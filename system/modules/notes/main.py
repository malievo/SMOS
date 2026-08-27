"""
main.py — модуль "notes": настоящие заметки на диске.

Реальный побочный эффект: add_note дописывает строку в data/notes.txt
(JSONL, по заметке на строку), list_notes читает файл обратно. Файл
лежит рядом с модулем, в data/ (в .gitignore — это рантайм-данные).

  python3 main.py add_note   '{"note_text": "купить хлеб"}'
  python3 main.py list_notes '{}'
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from log_client import send_log

DATA_DIR = Path(__file__).resolve().parent / "data"
NOTES_FILE = DATA_DIR / "notes.jsonl"


def add_note(text: str) -> dict:
    DATA_DIR.mkdir(exist_ok=True)
    entry = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "text": text,
    }
    with open(NOTES_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    total = _count_lines()
    return {"saved": entry, "total_notes": total, "file": str(NOTES_FILE)}


def list_notes() -> dict:
    if not NOTES_FILE.exists():
        return {"notes": [], "count": 0, "file": str(NOTES_FILE)}

    notes = []
    with open(NOTES_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                notes.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # битую строку молча пропускаем, не роняем чтение
    return {"notes": notes, "count": len(notes), "file": str(NOTES_FILE)}


def _count_lines() -> int:
    if not NOTES_FILE.exists():
        return 0
    with open(NOTES_FILE, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

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
