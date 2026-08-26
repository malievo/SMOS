"""
core.py — демон ядра SMOS: постоянно работающий системный процесс,
который принимает цели и раздаёт их в задачи, не блокируясь.

Место в системе (см. core_design.md, how_core_works.md, ../swl/swl_design.md):
ядро — НЕ звено линейного пайплайна на одну фразу (этим заняты FWL и
SWL), а служба того же типа, что демон логов: в неё прилетают цели от
многих разных команд. SWL превращает распознанную команду в
структурированную цель и кладёт её в папку-очередь; ядро эту очередь
разбирает.

Что делает демон:
1. ОДИН РАЗ на старте сканирует папки модулей (system/modules/ + modules/
   в корне) и строит граф действий — registry.scan() + build_registry().
   Без hot-reload: новый модуль виден только после перезапуска ядра
   (осознанное решение, см. swl_design.md). Снимок графа обновляется на
   диске (output/graph.json) — для чтения глазами и другими частями
   системы.
2. В цикле поллит папку-очередь goals/ (см. config.json). На каждый
   *.json-файл:
     - разбирает его: обязателен строковый ключ "goal"; необязательный
       объект "state" — то, что SWL уже вытащил из фразы (город и т.п.);
     - зовёт task_runner.create_task(goal, state, graph) — тот СРАЗУ
       возвращает task_id, ведёт задачу в отдельном потоке, ядро не ждёт;
     - удаляет файл цели (он разобран).
   Файл, который не удалось разобрать, не крутится в очереди вечно и не
   пропадает молча — переезжает в goals/rejected/ с предупреждением в лог.
3. Никогда не блокируется на выполнении задачи: create_task() —
   неблокирующий по построению (поток на задачу, см. task_runner.py),
   главный цикл только принимает и раздаёт.

Как узнать результат задачи: не через этот процесс, а через файл
system/core/tasks/<task_id>/state.json (task_runner.read_state) — общение
файловое, как и везде в SMOS.

Запуск:
    python core.py

SWL пока может не быть запущен — тогда очередь просто пустая. Для
проверки самого ядра можно вручную положить в goals/ файл вида
{"goal": "weather_forecast", "state": {}} — ядро его подхватит.
"""

import json
import sys
import time
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_INIT_DIR = SCRIPT_DIR / "module_init"
TASK_RUNNER_DIR = SCRIPT_DIR / "task_runner"
PLANNER_DIR = SCRIPT_DIR / "planner"
for path in (SCRIPT_DIR, MODULE_INIT_DIR, TASK_RUNNER_DIR, PLANNER_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import config  # noqa: E402
import registry as module_registry  # noqa: E402
import task_runner  # noqa: E402
from log_client import send_log  # noqa: E402

CFG = config.load(SCRIPT_DIR)

GOALS_DIR = SCRIPT_DIR / CFG["paths"]["goals_dir"]
REJECTED_DIR = GOALS_DIR / CFG["paths"]["rejected_subdir"]
CHECK_INTERVAL_SEC = CFG["check_interval_sec"]


def build_graph() -> dict:
    """Сканирует модули и строит граф действий планировщика. Заодно
    обновляет снимок на диске (output/modules.json, output/graph.json) —
    не источник истины, просто копия для чтения без запуска Python."""
    modules = module_registry.scan()
    graph = module_registry.build_registry(modules)
    module_registry.save(modules, graph)
    return graph


def _reject(goal_file: Path, reason: str) -> None:
    """Переносит неразобранный файл цели в goals/rejected/ (не удаляет —
    чтобы можно было посмотреть глазами, что пришло не так) и логирует."""
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    dest = REJECTED_DIR / f"{goal_file.stem}_{uuid.uuid4().hex[:8]}.json"
    try:
        goal_file.replace(dest)
    except OSError:
        pass
    send_log("WARNING", "goal_rejected", {"file": goal_file.name, "reason": reason})
    print(f"[core] отклонил {goal_file.name}: {reason}")


def process_goal_file(goal_file: Path, graph: dict) -> None:
    """Разбирает один файл цели из очереди и раздаёт его в задачу.

    SWL пишет файлы цели атомарно (temp + rename), так что здесь файл
    всегда прочитается целиком — битый JSON означает действительно
    испорченную цель, а не гонку с записью, поэтому такой файл сразу
    уходит в rejected, а не перечитывается на следующем цикле."""
    try:
        spec = json.loads(goal_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _reject(goal_file, f"не удалось прочитать JSON: {e}")
        return

    if not isinstance(spec, dict):
        _reject(goal_file, "цель должна быть JSON-объектом")
        return

    goal = spec.get("goal")
    if not isinstance(goal, str) or not goal:
        _reject(goal_file, "нет строкового поля 'goal'")
        return

    state = spec.get("state", {})
    if state is None:
        state = {}
    if not isinstance(state, dict):
        _reject(goal_file, "'state' должно быть JSON-объектом")
        return

    source_text = spec.get("source_text")
    task_id = task_runner.create_task(goal, state, graph)
    goal_file.unlink(missing_ok=True)

    send_log("INFO", "goal_dispatched", {
        "goal": goal, "task_id": task_id, "source_text": source_text,
        "known_keys": sorted(state),
    })
    print(f"[core] цель {goal!r} -> задача {task_id} (известно заранее: {sorted(state) or '—'})")


def pending_goal_files() -> list[Path]:
    """Файлы целей в очереди, старейшие первыми. Имена от SWL начинаются
    с метки времени (см. swl.py), поэтому сортировка по имени = по
    порядку поступления. rejected/ — подпапка, в glob('*.json') не
    попадает; временные файлы записи (*.json.tmp) — тоже."""
    if not GOALS_DIR.exists():
        return []
    return sorted(p for p in GOALS_DIR.glob("*.json") if p.is_file())


def main() -> None:
    GOALS_DIR.mkdir(parents=True, exist_ok=True)

    graph = build_graph()
    send_log("INFO", "core_started", {"actions": sorted(graph)})
    print(f"[core] запущен. В графе {len(graph)} действий: {list(graph)}")
    print(f"[core] слежу за очередью целей: {GOALS_DIR}")

    while True:
        for goal_file in pending_goal_files():
            process_goal_file(goal_file, graph)
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        send_log("INFO", "core_stopped")
        print("\n[core] остановлен")
