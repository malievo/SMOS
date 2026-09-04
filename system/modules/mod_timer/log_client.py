"""
log_client.py — отправка событий в систему логирования SMOS (logs/listener).

Своя копия у модуля, без общих зависимостей — так задумано в
logs/PROTOCOL.md. Одна функция send_log(): один UDP-пакет с JSON на
адрес демона логов. Демон недоступен — событие молча теряется, модуль
не падает и не блокируется.
"""

import json
import socket

LOG_HOST = "127.0.0.1"
LOG_PORT = 47110

MODULE_NAME = "mod_timer"

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send_log(level: str, message: str, data: dict | None = None) -> None:
    """level — DEBUG/INFO/WARNING/ERROR/CRITICAL, message — короткий
    машинный код события в snake_case, data — необязательные подробности
    (только JSON-совместимые типы)."""
    payload = {"module": MODULE_NAME, "level": level, "message": message}
    if data:
        payload["data"] = data
    try:
        _sock.sendto(
            json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"),
            (LOG_HOST, LOG_PORT),
        )
    except (OSError, TypeError, ValueError):
        pass  # логирование не должно ронять модуль
