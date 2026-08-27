"""
main.py — модуль "sysinfo": настоящие показатели хост-машины.

Читает реальные метрики Linux через /proc и стандартную библиотеку
(psutil не тянем — модули без внешних зависимостей). Отдаёт снимок:
загрузка, память, диск, аптайм, ядра, платформа.

  python3 main.py get_system_stats '{}'
  -> {"status": "ok", "data": {"system_stats": {...}}}
"""

import json
import os
import platform
import shutil
import socket
import sys
import time

from log_client import send_log


def _read_loadavg() -> list[float]:
    try:
        one, five, fifteen = os.getloadavg()
        return [round(one, 2), round(five, 2), round(fifteen, 2)]
    except OSError:
        return []


def _read_meminfo() -> dict:
    """/proc/meminfo -> байты. MemAvailable — то, что ядро реально
    считает доступным без свопа, точнее чем free+buffers+cache."""
    info = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key.strip()] = int(rest.strip().split()[0]) * 1024
    except (OSError, ValueError):
        return {}

    total = info.get("MemTotal", 0)
    available = info.get("MemAvailable", 0)
    used = total - available if total else 0
    return {
        "total_mb": round(total / 1024 / 1024, 1),
        "available_mb": round(available / 1024 / 1024, 1),
        "used_mb": round(used / 1024 / 1024, 1),
        "used_percent": round(used / total * 100, 1) if total else None,
    }


def _read_uptime() -> dict:
    try:
        with open("/proc/uptime", encoding="utf-8") as f:
            seconds = float(f.read().split()[0])
    except (OSError, ValueError):
        return {}
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    return {"seconds": int(seconds), "human": f"{days}d {hours}h {minutes}m"}


def _read_disk(path: str = "/") -> dict:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return {}
    return {
        "path": path,
        "total_gb": round(usage.total / 1024 ** 3, 1),
        "used_gb": round(usage.used / 1024 ** 3, 1),
        "free_gb": round(usage.free / 1024 ** 3, 1),
        "used_percent": round(usage.used / usage.total * 100, 1) if usage.total else None,
    }


def get_system_stats() -> dict:
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "loadavg_1_5_15": _read_loadavg(),
        "memory": _read_meminfo(),
        "disk_root": _read_disk("/"),
        "uptime": _read_uptime(),
        "sampled_unix": int(time.time()),
    }


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None

    if command != "get_system_stats":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    stats = get_system_stats()
    send_log("INFO", "system_stats_sampled", {
        "load1": stats["loadavg_1_5_15"][0] if stats["loadavg_1_5_15"] else None,
        "mem_used_percent": stats["memory"].get("used_percent"),
        "disk_used_percent": stats["disk_root"].get("used_percent"),
    })
    print(json.dumps({"status": "ok", "data": {"system_stats": stats}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
