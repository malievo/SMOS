"""
main.py — тестовый модуль "location" для проверки многошаговой цепочки
в планировщике (см. ../../core/planner/planner.py). Не настоящее
определение местоположения — всегда возвращает "Москва" хардкодом.
"""

import json
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None

    if command != "get_city":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    print(json.dumps({"status": "ok", "data": {"city": "Москва"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
