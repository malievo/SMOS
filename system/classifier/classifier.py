"""
classifier.py — основной скрипт классификатора «команда / разговор» SMOS.

Следит за output/recognized.json подсистемы rvs (см. system/rvs/req.py),
на каждую новую распознанную фразу вызывает ai_classify() (сейчас —
GigaChat, см. ai_provider.py) и складывает результат в свой собственный
output/classified.json.

Это bootstrap-этап схемы из model_combination_design.md: классификация
идёт через облачную LLM напрямую, без локальной модели и без ручной
проверки пользователем — см. этот файл за подробностями всей схемы.

Скрипт сам находит свою папку, не зависит от того, откуда его запустили.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))  # чтобы гарантированно найти ai_provider.py/log_client.py рядом с собой

import config  # noqa: E402
from ai_provider import ai_classify  # noqa: E402
from log_client import send_log  # noqa: E402

CFG = config.load(SCRIPT_DIR)

# --- Пути (аналогично wake.py/req.py — та же rvs, соседняя папка) ---
RECOGNIZED_FILE = SCRIPT_DIR / CFG["paths"]["rvs_output_dir"] / CFG["paths"]["rvs_output_file"]
OUTPUT_DIR = SCRIPT_DIR / CFG["paths"]["output_dir"]
OUTPUT_FILE = OUTPUT_DIR / CFG["paths"]["output_file"]

CHECK_INTERVAL_SEC = CFG["check_interval_sec"]


def load_recognized() -> dict:
    return json.loads(RECOGNIZED_FILE.read_text(encoding="utf-8"))


def save_result(text: str, label: str) -> None:
    """Пишет classified.json атомарно (temp-файл + rename), чтобы будущие
    читатели (пока их нет) никогда не увидели недописанный файл — тот же
    приём, что и в wake.py для utterance.wav."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    payload = {
        "text": text,
        "label": label,
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    tmp_file = OUTPUT_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(OUTPUT_FILE)


def main() -> None:
    send_log("INFO", "classifier_started")
    print(f"[classifier] слежу за {RECOGNIZED_FILE}")

    last_mtime = None

    while True:
        if RECOGNIZED_FILE.exists():
            mtime = RECOGNIZED_FILE.stat().st_mtime

            if mtime != last_mtime:
                last_mtime = mtime

                try:
                    recognized = load_recognized()
                except (json.JSONDecodeError, OSError):
                    # req.py пишет recognized.json не атомарно — есть шанс
                    # поймать файл в момент записи. Просто пробуем ещё раз
                    # на следующем цикле, а не падаем.
                    time.sleep(CHECK_INTERVAL_SEC)
                    continue

                text = recognized.get("text", "").strip()
                if not text:
                    time.sleep(CHECK_INTERVAL_SEC)
                    continue

                try:
                    label = ai_classify(text)
                except Exception as e:
                    send_log("ERROR", "classification_failed", {"text": text, "error": str(e)})
                    print(f"[classifier] ошибка классификации: {e}")
                    time.sleep(CHECK_INTERVAL_SEC)
                    continue

                save_result(text, label)
                send_log("INFO", "phrase_classified", {"text": text, "label": label})
                print(f"[classifier] {label}: {text}")

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        send_log("INFO", "classifier_stopped")
        print("\n[classifier] остановлен")
