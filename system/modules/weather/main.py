"""
main.py — тестовый модуль "weather" для проверки многошаговой цепочки
в планировщике (см. ../../core/planner/planner.py). Не настоящий
прогноз погоды — возвращает фиктивные данные по переданному городу.
"""

import json
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    if command != "get_weather":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    if "city" not in params:
        print(json.dumps({"status": "missing", "missing": ["city"]}, ensure_ascii=False))
        return

    forecast = {
        "city": params["city"],
        "temperature_c": 21,
        "condition": "облачно (фиктивные данные, для теста)",
    }
    print(json.dumps({"status": "ok", "data": {"weather_forecast": forecast}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
