"""
main.py — тестовый модуль "echo" для проверки сканера/реестра и
протокола вызова (см. ../../core/module_init/manifest_design.md).
Не настоящая возможность SMOS, просто фикстура: возвращает то, что
получил, без побочных эффектов.
"""

import json
import sys


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    if command != "echo":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    if "text" not in params:
        print(json.dumps({"status": "missing", "missing": ["text"]}, ensure_ascii=False))
        return

    print(json.dumps({"status": "ok", "data": {"echoed_text": params["text"]}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
