"""main.py — тестовый модуль "clock", фиктивное время, для проверки."""

import json
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command != "get_time":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return
    print(json.dumps({"status": "ok", "data": {"current_time": "15:00 (фиктивное время, для теста)"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
