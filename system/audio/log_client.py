"""
log_client.py — отправка событий в систему логирования SMOS (logs/listener).

Копия system/swl/log_client.py под этот процесс (MODULE_NAME другой) —
так и задумано в logs/PROTOCOL.md: клиент маленький, каждая независимая
часть системы носит свою копию, а не тянет общую зависимость.

Одна функция send_log(...), которая кидает UDP-пакет с JSON на
фиксированный адрес демона логов. Демон не запущен/недоступен —
send_log() не бросает исключение и не блокирует вызывающий код: событие
просто теряется. Логирование не должно быть причиной сбоя того, что
логируется.
"""

import json
import socket

LOG_HOST = "127.0.0.1"
LOG_PORT = 47110

MODULE_NAME = "audio"

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send_log(level: str, message: str, data: dict | None = None) -> None:
    """Отправляет одно лог-событие. level — DEBUG/INFO/WARNING/ERROR/CRITICAL,
    message — короткий машинно-читаемый код события, data — необязательные
    подробности."""
    payload = {"module": MODULE_NAME, "level": level, "message": message}
    if data:
        payload["data"] = data
    try:
        _sock.sendto(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), (LOG_HOST, LOG_PORT))
    except (OSError, TypeError, ValueError):
        pass  # логирование не должно ронять процесс
