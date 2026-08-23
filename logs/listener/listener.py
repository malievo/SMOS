"""
listener.py — демон-приёмник логов SMOS.

Идея: логирование не должно быть тем, от чего зависят остальные модули
системы. Поэтому транспорт — UDP на localhost: любой модуль (не важно,
на чём написан) просто отправляет один JSON-пакет на фиксированный
адрес и продолжает работать дальше, не дожидаясь ответа. Если этот
демон сейчас не запущен/перезапускается — отправка модулю ничего не
стоит и не блокирует и не роняет его, пакет просто теряется.

Что отправляет модуль (три обязательных поля + необязательные данные):
    {"module": "rvs", "level": "INFO", "message": "wake_word_detected", "data": {...}}
Время (ts) модуль НЕ указывает — его проставляет этот демон в момент
приёма, чтобы не зависеть от того, насколько точно настроены часы у
конкретного модуля, и чтобы формат времени был одинаковым везде.

Что делает демон:
- Слушает listener.host:listener.port (UDP, см. config.json).
- На каждый пакет: парсит JSON, проверяет обязательные поля
  (module, message), проставляет ts и level по умолчанию (INFO, если
  level отсутствует или не из стандартного набора).
- Раскладывает событие по папкам модулей: logs/raw/<module>/events.jsonl
  (создаёт подпапку модуля при первом событии от него). Файл —
  append-only (одна JSON-запись на строку, JSONL) — демон никогда не
  читает и не удаляет уже записанное, это сырой архив событий, который
  позже будет читать отдельный анализатор.
- Битые/неполные пакеты не роняют демон — печатается предупреждение,
  пакет отбрасывается, приём продолжается.

Запуск:
    python listener.py
"""

import json
import socket
import sys
from datetime import datetime
from pathlib import Path

# --- Пути (скрипт сам находит себя, не зависит от места запуска) ---
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))  # чтобы гарантированно найти config.py рядом с собой

import config  # noqa: E402  (импорт после правки sys.path — так и задумано)

CFG = config.load(SCRIPT_DIR)

LOGS_DIR = SCRIPT_DIR.parent  # папка logs/, на уровень выше listener/
RAW_DIR = LOGS_DIR / CFG["paths"]["raw_dir"]
EVENTS_FILENAME = CFG["paths"]["events_filename"]

HOST = CFG["listener"]["host"]
PORT = CFG["listener"]["port"]

# Стандартные уровни логирования (как в logging самого Python) — общая,
# заранее известная любому разработчику модуля конвенция.
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
DEFAULT_LEVEL = "INFO"

MAX_PACKET_SIZE = 65536  # с запасом хватает на любое разумное лог-событие с data


def handle_packet(raw_bytes: bytes, addr) -> None:
    """Разбирает один принятый UDP-пакет и, если он валиден, дописывает
    событие в файл соответствующего модуля. Любая проблема с пакетом —
    это просто предупреждение в консоль, не исключение: один плохой
    отправитель не должен останавливать приём от остальных."""
    try:
        payload = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[logs] Некорректный пакет от {addr}: {e}")
        return

    if not isinstance(payload, dict):
        print(f"[logs] Пакет от {addr} — не JSON-объект, пропущен.")
        return

    module = payload.get("module")
    message = payload.get("message")
    if not module or not message:
        print(f"[logs] Пакет от {addr} без обязательных полей module/message, пропущен: {payload}")
        return

    level = payload.get("level", DEFAULT_LEVEL)
    if level not in VALID_LEVELS:
        level = DEFAULT_LEVEL

    event = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "module": module,
        "level": level,
        "message": message,
        "data": payload.get("data"),
    }

    module_dir = RAW_DIR / str(module)
    module_dir.mkdir(parents=True, exist_ok=True)
    events_file = module_dir / EVENTS_FILENAME
    with open(events_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

    print(f"[logs] {module} [{level}] {message}")


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((HOST, PORT))
    print(f"[logs] Запущен. Слушаю {HOST}:{PORT}...")

    try:
        while True:
            raw_bytes, addr = sock.recvfrom(MAX_PACKET_SIZE)
            handle_packet(raw_bytes, addr)
    except KeyboardInterrupt:
        print("[logs] Остановлен.")
    finally:
        sock.close()


if __name__ == "__main__":
    main()
