"""
main.py — модуль "notes": настоящие заметки на диске.

Реальный побочный эффект: add_note дописывает строку в notes.jsonl
(JSONL, по заметке на строку), list_notes читает файл обратно.

Файл лежит в ЛИЧНОЙ папке данных модуля, путь к которой ядро передаёт
переменной окружения SMOS_MODULE_DATA (`user/module_data/mod_notes/`,
отдельно от кода модуля — переживает его удаление/переустановку, см.
../../core/module_init/manifest_design.md, раздел "Хранение данных
модуля"). Модуль этот путь не вычисляет сам, а только читает из env.

  SMOS_MODULE_DATA=/tmp/notes python3 main.py add_note   '{"note_text": "купить хлеб"}'
  SMOS_MODULE_DATA=/tmp/notes python3 main.py list_notes '{}'
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

from log_client import send_log


def _notes_file() -> Path:
    """Путь к notes.jsonl в личной папке данных модуля (SMOS_MODULE_DATA).
    Папку создаём здесь же — в норме ядро её уже создало перед вызовом,
    но при ручном запуске переменная может указывать на несуществующий
    путь."""
    base = os.environ.get("SMOS_MODULE_DATA")
    if not base:
        raise KeyError("SMOS_MODULE_DATA")
    data_dir = Path(base)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "notes.jsonl"


def add_note(text: str) -> dict:
    notes_file = _notes_file()
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text,
    }
    with open(notes_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    total = _count_lines(notes_file)
    return {"saved": entry, "total_notes": total, "file": str(notes_file)}


def list_notes() -> dict:
    notes_file = _notes_file()
    if not notes_file.exists():
        return {"notes": [], "count": 0, "file": str(notes_file)}

    notes = []
    with open(notes_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                notes.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # битую строку молча пропускаем, не роняем чтение
    return {"notes": notes, "count": len(notes), "file": str(notes_file)}


def _count_lines(notes_file: Path) -> int:
    if not notes_file.exists():
        return 0
    with open(notes_file, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    if os.environ.get("SMOS_MODULE_DATA") is None:
        print(json.dumps({
            "status": "error",
            "error": "SMOS_MODULE_DATA не задан — модуль запускается ядром, "
                     "которое передаёт путь к папке данных (для ручного теста задай переменную сам)",
        }, ensure_ascii=False))
        return

    if command == "add_note":
        text = params.get("note_text")
        if not text or not str(text).strip():
            print(json.dumps({"status": "missing", "missing": ["note_text"]}, ensure_ascii=False))
            return
        result = add_note(str(text).strip())
        send_log("INFO", "note_added", {"total_notes": result["total_notes"]})
        print(json.dumps({"status": "ok", "data": {"note_saved": result}}, ensure_ascii=False))
        return

    if command == "list_notes":
        result = list_notes()
        send_log("INFO", "notes_listed", {"count": result["count"]})
        print(json.dumps({"status": "ok", "data": {"notes_list": result}}, ensure_ascii=False))
        return

    print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
