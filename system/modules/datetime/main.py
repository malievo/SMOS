"""
main.py — модуль "datetime": настоящие текущие дата и время машины.

Не фикстура (в отличие от system/modules/clock) — читает реальные часы
через стандартную библиотеку и отдаёт разложенное представление, которым
удобно пользоваться другим модулям (например journal).

Протокол вызова — см. system/core/module_init/manifest_design.md:
  python3 main.py get_datetime '{}'
  -> {"status": "ok", "data": {"current_datetime": {...}}}
"""

import json
import sys
import time
from datetime import datetime

from log_client import send_log

WEEKDAYS_RU = [
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
]


def get_datetime() -> dict:
    now = datetime.now().astimezone()
    return {
        "iso": now.isoformat(timespec="seconds"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": WEEKDAYS_RU[now.weekday()],
        "weekday_num": now.weekday() + 1,
        "timezone": now.tzname(),
        "utc_offset_sec": int(now.utcoffset().total_seconds()) if now.utcoffset() else 0,
        "unix": int(time.time()),
    }


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None

    if command != "get_datetime":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    payload = get_datetime()
    send_log("INFO", "datetime_reported", {"iso": payload["iso"]})
    print(json.dumps({"status": "ok", "data": {"current_datetime": payload}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
