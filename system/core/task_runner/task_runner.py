"""
task_runner.py — модель "поток на задачу": ядро создаёт отдельное
пространство (папку) под каждую цель от SWL и не блокируется, пока
задача выполняется. Дизайн и все решения — см. ../core_design.md.

SWL ещё не существует — цели пока задаются вручную (см. __main__), как
и в planner.py: create_task() не знает и не обязан знать, откуда
взялась цель — это забота вызывающего кода.

Общение между потоком задачи и остальной системой — через файл
state.json, не через shared memory (см. core_design.md, раздел "Память
задачи — файл, не shared memory").
"""

import json
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_DIR = SCRIPT_DIR.parent
PLANNER_DIR = CORE_DIR / "planner"
MODULE_INIT_DIR = CORE_DIR / "module_init"
for path in (SCRIPT_DIR, CORE_DIR, PLANNER_DIR, MODULE_INIT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import planner  # noqa: E402
import registry as module_registry  # noqa: E402
from log_client import send_log  # noqa: E402

# Папки конкретных задач — чистые рантайм-данные, не исходный код,
# поэтому не рядом с этим файлом, а на уровень выше, в system/core/tasks/
# (в .gitignore).
TASKS_DIR = CORE_DIR / "tasks"

# Очередь результатов для следующей стадии (приведение ответа в
# человеческий вид → синтез речи). Ядро сюда ПИШЕТ по завершении задачи —
# так же, как SWL пишет в core/goals/: вход принадлежит потребителю,
# производитель в него кладёт. tasks/<id>/state.json остаётся как
# долговременная запись; этот файл — компактное уведомление «задача
# готова, вот что сказать». Потребителя (outputstructurizer) пока нет —
# файлы просто копятся, как копилась бы core/goals/ без запущенного ядра.
OUTPUTSTRUCTURIZER_QUEUE_DIR = CORE_DIR.parent / "outputstructurizer" / "queue"


def _new_task_id() -> str:
    """Человекочитаемый и гарантированно уникальный id: время создания —
    чтобы задачи было видно по порядку глазами в списке папок, плюс
    короткий случайный хвост на случай двух задач в одну секунду."""
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _save_state(task_dir: Path, state: dict) -> None:
    """Атомарная запись (temp-файл + rename) — тот же приём, что и
    везде в проекте (rvs/classifier/module_init)."""
    task_dir.mkdir(parents=True, exist_ok=True)
    tmp_file = task_dir / "state.json.tmp"
    tmp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(task_dir / "state.json")


def _emit_completion(record: dict, source_text: str | None) -> None:
    """Кладёт компактный файл-результат в очередь следующей стадии
    (OUTPUTSTRUCTURIZER_QUEUE_DIR). Атомарно (temp + rename), имя файла =
    task_id (он уже уникален и по времени, значит очередь читается по
    порядку поступления). Пишется и на done, и на error — ошибку тоже
    надо проговорить пользователю ("не смог ...").

    Место под поле speech (дословная формулировка от модуля, который
    произвёл цель) оставлено на будущее — сейчас его никто не заполняет
    и не читает, см. system/audio/audio_design.md."""
    OUTPUTSTRUCTURIZER_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": record["task_id"],
        "goal": record["goal"],
        "status": record["status"],
        "result": record["result"],
        "error": record["error"],
        "state": record["state"],
        "source_text": source_text,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    name = f"{record['task_id']}.json"
    tmp_file = OUTPUTSTRUCTURIZER_QUEUE_DIR / (name + ".tmp")
    tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(OUTPUTSTRUCTURIZER_QUEUE_DIR / name)


def _run_task(task_id: str, goal: str, initial_state: dict, graph: dict,
              source_text: str | None = None) -> None:
    """Код одного потока задачи. Обычный последовательный/блокирующий
    код — блокируется сам на себя (внутри achieve(), на вызовах
    модулей), но это не блокирует ядро в целом, см. create_task().

    source_text — исходная фраза пользователя (от SWL через ядро), едет
    дальше в очередь результатов: формулировка ответа зависит от того,
    как спросили ("сколько времени" vs "который час")."""
    task_dir = TASKS_DIR / task_id
    record = {
        "task_id": task_id,
        "goal": goal,
        "state": dict(initial_state),
        "status": "running",
        "result": None,
        "error": None,
    }
    _save_state(task_dir, record)
    send_log("INFO", "task_started", {"task_id": task_id, "goal": goal})

    try:
        result = planner.achieve(goal, record["state"], graph)
        record["status"] = "done"
        record["result"] = result
        send_log("INFO", "task_done", {"task_id": task_id, "goal": goal})
    except planner.PlanningError as e:
        record["status"] = "error"
        record["error"] = str(e)
        send_log("ERROR", "task_failed", {"task_id": task_id, "goal": goal, "error": str(e)})

    _save_state(task_dir, record)
    _emit_completion(record, source_text)


def create_task(goal: str, initial_state: dict, graph: dict,
                source_text: str | None = None) -> str:
    """Создаёт задачу и сразу возвращает её id, не дожидаясь
    выполнения — сама задача выполняется в отдельном потоке. Вызывающий
    код (главный цикл ядра) тут же свободен принимать следующую цель —
    это и есть требование "ядро асинхронно" из core_design.md.

    source_text — необязательная исходная фраза, пробрасывается в
    файл-результат для стадии формулировки ответа."""
    task_id = _new_task_id()
    thread = threading.Thread(
        target=_run_task, args=(task_id, goal, initial_state, graph, source_text), daemon=True
    )
    thread.start()
    return task_id


def read_state(task_id: str) -> dict:
    """Читает текущее состояние задачи из её state.json. Так, а не
    спрашивая поток напрямую — общение через файл, не shared memory."""
    return json.loads((TASKS_DIR / task_id / "state.json").read_text(encoding="utf-8"))


if __name__ == "__main__":
    modules = module_registry.scan()
    graph = module_registry.build_registry(modules)
    print(f"[tasks] реестр: {list(graph)}")

    # SWL ещё не существует — цели задаются вручную. Специально создаём
    # две задачи подряд, чтобы показать: создание задачи не ждёт
    # завершения предыдущей.
    t0 = time.time()
    print("\n[tasks] создаю задачу 1 (weather_forecast, многошаговая)...")
    task1 = create_task("weather_forecast", {}, graph)
    print(f"[tasks] создание вернуло управление за {time.time() - t0:.3f} сек, задача {task1} работает в фоне")

    print("\n[tasks] создаю задачу 2 (echoed_text) сразу следом, не дожидаясь первой...")
    t0 = time.time()
    task2 = create_task("echoed_text", {"text": "проверка потоков"}, graph)
    print(f"[tasks] создание вернуло управление за {time.time() - t0:.3f} сек, задача {task2} работает в фоне")

    # Ждём обе, только чтобы в демонстрации показать финальный результат —
    # в реальном ядре главный цикл так ждать не будет.
    time.sleep(2)

    print()
    for task_id in (task1, task2):
        state = read_state(task_id)
        print(f"[tasks] {task_id}: status={state['status']} result={state['result']} error={state['error']}")
