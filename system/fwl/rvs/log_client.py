"""
log_client.py — отправка событий в систему логирования SMOS (logs/listener).

Никакой сложной библиотеки: одна функция send_log(...), которая кидает
UDP-пакет с JSON на фиксированный адрес демона логов (см.
logs/listener/config.json — здесь адрес продублирован намеренно, чтобы
rvs не зависел от файлов другой системы, а просто знал общий,
заранее согласованный адрес).

Модуль НЕ проставляет время — это делает демон при получении (см.
listener.py). Если демон логов сейчас не запущен или недоступен —
send_log() не бросает исключение и не блокирует вызывающий код: событие
просто теряется. Логирование не должно быть причиной сбоя того, что
логируется.
"""

import json
import socket

LOG_HOST = "127.0.0.1"
LOG_PORT = 47110

MODULE_NAME = "rvs"

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send_log(level: str, message: str, data: dict | None = None) -> None:
    """Отправляет одно лог-событие. level — DEBUG/INFO/WARNING/ERROR/CRITICAL,
    message — короткий машинно-читаемый код события (например,
    "wake_word_detected"), data — необязательные подробности."""
    payload = {"module": MODULE_NAME, "level": level, "message": message}
    if data:
        payload["data"] = data
    try:
        _sock.sendto(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), (LOG_HOST, LOG_PORT))
    except (OSError, TypeError, ValueError):
        pass  # логирование не должно ронять модуль — ни из-за сети, ни из-за странных типов в data
