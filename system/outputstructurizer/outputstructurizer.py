"""
outputstructurizer.py — промежуточный этап между ядром и воспроизведением.

Место в системе (см. outputstructurizer_design.md, ../cnps/cnps_design.md,
../core/core_design.md раздел «Отдача результата дальше»):

    ядро/task_runner  ──► system/outputstructurizer/queue/   (файл на задачу)
                              │  этот процесс: result/error -> человеческая фраза
                              ▼
                        system/cnps/tasks/                    (заявка на воспроизведение)
                              │  CNPS: класс важности + синтез по предложениям
                              ▼
                          произнесено

Что делает демон (форма — как core.py / swl.py: поллинг папки-очереди):
1. На старте: файлы, уже лежащие в queue/, считаются протухшими
   (озвученный ответ полезен только свежим) — переезжают в queue/stale/
   и НЕ произносятся. Дальше реагируем только на файлы, появившиеся
   после старта.
2. В цикле поллит queue/. На каждый *.json-файл (task_runner пишет их
   атомарно, temp+rename — недописанного не поймаем):
     - разбирает: обязательны строковые task_id, goal, status;
     - phrasing.render() выбирает фразу (готовый шаблон -> GigaChat ->
       сырой результат, см. phrasing.py);
     - кладёт заявку <task_id>.json в system/cnps/tasks/ (атомарно);
     - если фразу сформулировал GigaChat — дописывает пример в
       output/dataset.jsonl (материал для будущей локальной модели);
     - удаляет разобранный файл очереди.
   Неразобранный файл не крутится вечно и не пропадает молча —
   переезжает в queue/rejected/ с предупреждением в лог.

Запуск:
    python outputstructurizer.py

Ядро может быть не запущено — тогда очередь просто пустая. Для проверки
без ядра можно положить в queue/ файл вида
{"task_id": "manual", "goal": "calc_result", "status": "done",
 "result": {"expression": "2+2", "value": 4}, "state": {}, "source_text": "посчитай"}
"""

import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
import phrasing  # noqa: E402
from log_client import send_log  # noqa: E402

CFG = config.load(SCRIPT_DIR)

QUEUE_DIR = SCRIPT_DIR / CFG["paths"]["queue_dir"]
REJECTED_DIR = QUEUE_DIR / CFG["paths"]["rejected_subdir"]
STALE_DIR = QUEUE_DIR / CFG["paths"]["stale_subdir"]
# Заявки на воспроизведение кладём в CNPS (system/cnps/tasks/); если в
# конфиге нет cnps_tasks_dir — откат на старую заглушку system/audio/.
PLAYBACK_TASKS_DIR = (SCRIPT_DIR / CFG["paths"].get("cnps_tasks_dir", CFG["paths"]["audio_tasks_dir"])).resolve()
PHRASES_FILE = SCRIPT_DIR / CFG["paths"]["phrases_file"]
OUTPUT_DIR = SCRIPT_DIR / CFG["paths"]["output_dir"]
DATASET_FILE = OUTPUT_DIR / CFG["paths"]["dataset_file"]

CHECK_INTERVAL_SEC = CFG["check_interval_sec"]
PRIVILEGE_LEVEL = CFG["privilege_level"]
PLAYBACK_VOICE = CFG.get("playback_voice", "gtts")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def load_phrases() -> dict:
    """Читает phrases.json. Нет файла или битый — пустая таблица (всё
    уйдёт в GigaChat / сырой fallback) + предупреждение, не падаем."""
    if not PHRASES_FILE.exists():
        print(f"[outputstructurizer] {PHRASES_FILE} не найден — работаю без готовых фраз")
        send_log("WARNING", "phrases_file_missing", {"path": str(PHRASES_FILE)})
        return {}
    try:
        data = json.loads(PHRASES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[outputstructurizer] не читается {PHRASES_FILE} ({e}) — работаю без готовых фраз")
        send_log("WARNING", "phrases_file_broken", {"path": str(PHRASES_FILE), "error": str(e)})
        return {}
    return data if isinstance(data, dict) else {}


def _move(src: Path, dest_dir: Path, tag: str) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{src.stem}_{uuid.uuid4().hex[:8]}.json"
    try:
        src.replace(dest)
    except OSError:
        pass
    print(f"[outputstructurizer] {tag}: {src.name} -> {dest.parent.name}/")


def queue_files() -> list[Path]:
    """*.json в очереди, старейшие первыми (имя = task_id, а он начинается
    с метки времени). Подпапки rejected/ и stale/ в glob не попадают,
    временные *.json.tmp — тоже."""
    if not QUEUE_DIR.exists():
        return []
    return sorted(p for p in QUEUE_DIR.glob("*.json") if p.is_file())


def discard_stale_on_start() -> None:
    """Всё, что уже лежит в очереди на момент старта, — устаревшие ответы
    (система была выключена/перезапущена). Не произносим их пачкой,
    убираем в stale/."""
    stale = queue_files()
    if not stale:
        return
    for f in stale:
        _move(f, STALE_DIR, "протухло на старте")
    send_log("INFO", "stale_results_discarded", {"count": len(stale)})
    print(f"[outputstructurizer] на старте отброшено устаревших результатов: {len(stale)}")


def emit_audio_task(record: dict, text: str, phrase_layer: str) -> str:
    """Кладёт заявку на воспроизведение в CNPS (system/cnps/tasks/,
    атомарно temp+rename). Имя файла = task_id (уникален и по времени —
    очередь читается по порядку). Возвращает имя файла.

    Манифест заявки CNPS — см. system/cnps/cnps_design.md, «Манифест
    заявки». Класс важности проставляет САМ CNPS по source; здесь мы
    только честно называем источник (outputstructurizer -> потолок
    RESULT) и передаём privilege_level=2 как подсказку (CNPS её
    зажмёт по потолку). phrase_layer (template|llm|fallback) — для
    отладки/статистики, на класс не влияет."""
    PLAYBACK_TASKS_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": record["task_id"],
        "task_id": record["task_id"],
        # Сквозной id реплики — последнее звено, где он ещё передаётся;
        # CNPS кладёт его в лог delivery (конец истории), см. logs/PROTOCOL.md.
        "trace_id": record.get("trace_id") or "",
        "kind": "speech",
        # Кто прислал — основной сигнал важности для CNPS.
        "source": "outputstructurizer",
        "goal": record["goal"],
        "status": record["status"],
        "text": text,
        # Подсказка класса: ответ модуля пользователю. CNPS зажмёт по
        # потолку источника (outputstructurizer -> RESULT).
        "privilege_level": PRIVILEGE_LEVEL,
        # Голос: ответы и болталку CNPS озвучивает женским gtts (голос
        # ассистента), отдельным от мужского piper системных сообщений.
        **({"voice": PLAYBACK_VOICE} if PLAYBACK_VOICE else {}),
        # Каким слоём формулировки получена фраза: template | llm | fallback.
        "phrase_layer": phrase_layer,
        # Исходная фраза пользователя — на будущее (тон/скорость озвучки).
        "source_text": record.get("source_text"),
        "ts": now_iso(),
    }
    name = f"{record['task_id']}.json"
    tmp = PLAYBACK_TASKS_DIR / (name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PLAYBACK_TASKS_DIR / name)
    return name


def append_to_dataset(record: dict, text: str) -> None:
    """Дописывает пример в output/dataset.jsonl — только для фраз,
    сформулированных GigaChat (это и есть «учительский» сигнал, как
    dataset.jsonl у классификатора и SWL). Материал для будущей
    локальной модели-формулировщика."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    row = {
        "goal": record.get("goal"),
        "status": record.get("status"),
        "result": record.get("result"),
        "error": record.get("error"),
        "source_text": record.get("source_text"),
        "text": text,
        "source": "llm_api",
    }
    with open(DATASET_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def process_file(f: Path, phrases: dict, seen: set) -> None:
    try:
        record = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _move(f, REJECTED_DIR, f"битый JSON ({e})")
        send_log("WARNING", "result_rejected", {"file": f.name, "reason": f"json: {e}"})
        return

    if not isinstance(record, dict):
        _move(f, REJECTED_DIR, "не JSON-объект")
        send_log("WARNING", "result_rejected", {"file": f.name, "reason": "не объект"})
        return

    task_id = record.get("task_id")
    goal = record.get("goal")
    status = record.get("status")
    if not (isinstance(task_id, str) and task_id) or not (isinstance(goal, str) and goal) \
            or not (isinstance(status, str) and status):
        _move(f, REJECTED_DIR, "нет task_id/goal/status")
        send_log("WARNING", "result_rejected", {"file": f.name, "reason": "нет обязательных полей"})
        return

    trace_id = record.get("trace_id") or ""

    if task_id in seen:
        # тот же task_id уже обработан в этом запуске — не произносим дважды
        f.unlink(missing_ok=True)
        send_log("DEBUG", "duplicate_result_skipped", {"task_id": task_id}, trace_id=trace_id)
        return

    text, source = phrasing.render(record, phrases, CFG)
    audio_file = emit_audio_task(record, text, source)
    if source == "llm":
        append_to_dataset(record, text)

    seen.add(task_id)
    f.unlink(missing_ok=True)

    send_log("INFO", "phrase_emitted", {
        "task_id": task_id, "goal": goal, "status": status,
        "source": source, "text": text, "audio_file": audio_file,
    }, trace_id=trace_id)
    print(f"[outputstructurizer] {goal!r} [{status}] ({source}) -> {text!r}")


def main() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    PLAYBACK_TASKS_DIR.mkdir(parents=True, exist_ok=True)

    phrases = load_phrases()
    discard_stale_on_start()

    send_log("INFO", "outputstructurizer_started", {"goals_with_phrases": sorted(
        k for k in phrases if not k.startswith("_"))})
    print(f"[outputstructurizer] запущен. Очередь результатов: {QUEUE_DIR}")
    print(f"[outputstructurizer] заявки на воспроизведение кладу в: {PLAYBACK_TASKS_DIR}")

    seen: set = set()
    while True:
        for f in queue_files():
            process_file(f, phrases, seen)
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        send_log("INFO", "outputstructurizer_stopped")
        print("\n[outputstructurizer] остановлен")
