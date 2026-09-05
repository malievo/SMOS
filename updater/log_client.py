"""
log_client.py — отправка событий в систему логирования SMOS (logs/listener).

Копия того же клиента, что и у остальных процессов (см.
logs/PROTOCOL.md) — одна функция send_log(...), UDP-пакет с JSON на
фиксированный адрес демона логов. Не бросает исключений и не блокирует
вызывающий код, если демон сейчас не запущен или недоступен —
логирование не должно быть причиной сбоя того, что логируется.
"""

import json
import socket

LOG_HOST = "127.0.0.1"
LOG_PORT = 47110

MODULE_NAME = "updater"

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send_log(level: str, message: str, data: dict | None = None) -> None:
    """Отправляет одно лог-событие. level — DEBUG/INFO/WARNING/ERROR/CRITICAL,
    message — короткий машинно-читаемый код события (например,
    "update_available"), data — необязательные подробности."""
    payload = {"module": MODULE_NAME, "level": level, "message": message}
    if data:
        payload["data"] = data
    try:
        _sock.sendto(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), (LOG_HOST, LOG_PORT))
    except (OSError, TypeError, ValueError):
        pass  # логирование не должно ронять модуль — ни из-за сети, ни из-за странных типов в data
