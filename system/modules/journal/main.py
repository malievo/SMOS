"""
main.py — модуль "journal": снимок состояния системы в журнал на диске.

Ничего не собирает сам — принимает уже готовые current_datetime (от
mod_datetime) и system_stats (от mod_sysinfo) и склеивает их в одну
строку журнала journal.jsonl. Смысл модуля для тестов — проверить
многошаговый граф: цель "journal_written" тянет за собой ДВА других
настоящих модуля, и планировщик должен вызвать их сам.

Файл журнала лежит в личной папке данных модуля, путь к которой ядро
передаёт переменной SMOS_MODULE_DATA (`user/module_data/mod_journal/`,
отдельно от кода модуля — см. ../../core/module_init/manifest_design.md,
раздел "Хранение данных модуля").

  SMOS_MODULE_DATA=/tmp/j python3 main.py write_snapshot '{"current_datetime": {...}, "system_stats": {...}}'
  -> {"status": "ok", "data": {"journal_written": {...}}}
"""

import json
import os
import sys
from pathlib import Path

from log_client import send_log


def _journal_file() -> Path:
    """Путь к journal.jsonl в личной папке данных модуля (SMOS_MODULE_DATA)."""
    base = os.environ.get("SMOS_MODULE_DATA")
    if not base:
        raise KeyError("SMOS_MODULE_DATA")
    data_dir = Path(base)
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "journal.jsonl"


def write_snapshot(current_datetime: dict, system_stats: dict) -> dict:
    journal_file = _journal_file()

    mem = system_stats.get("memory", {})
    load = system_stats.get("loadavg_1_5_15") or [None]
    entry = {
        "at": current_datetime.get("iso"),
        "weekday": current_datetime.get("weekday"),
        "host": system_stats.get("hostname"),
        "load1": load[0],
        "mem_used_percent": mem.get("used_percent"),
        "disk_used_percent": system_stats.get("disk_root", {}).get("used_percent"),
        "uptime": system_stats.get("uptime", {}).get("human"),
    }
    with open(journal_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    total = sum(1 for line in journal_file.open(encoding="utf-8") if line.strip())
    return {"entry": entry, "total_entries": total, "file": str(journal_file)}


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    if command != "write_snapshot":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    if os.environ.get("SMOS_MODULE_DATA") is None:
        print(json.dumps({
            "status": "error",
            "error": "SMOS_MODULE_DATA не задан — модуль запускается ядром, "
                     "которое передаёт путь к папке данных (для ручного теста задай переменную сам)",
        }, ensure_ascii=False))
        return

    missing = [k for k in ("current_datetime", "system_stats") if k not in params]
    if missing:
        print(json.dumps({"status": "missing", "missing": missing}, ensure_ascii=False))
        return

    result = write_snapshot(params["current_datetime"], params["system_stats"])
    send_log("INFO", "journal_entry_written", {"total_entries": result["total_entries"]})
    print(json.dumps({"status": "ok", "data": {"journal_written": result}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
