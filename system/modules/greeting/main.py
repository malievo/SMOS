"""main.py — тестовый модуль "greeting", два действия в одном манифесте."""

import json
import sys

RESPONSES = {
    "say_hello": ("greeting_text", "Привет! Чем могу помочь?"),
    "say_bye": ("farewell_text", "Пока! До связи."),
}


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    if command not in RESPONSES:
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return
    key, text = RESPONSES[command]
    print(json.dumps({"status": "ok", "data": {key: text}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
