"""
log_client.py — отправка событий в систему логирования SMOS (logs/listener).

Копия под ядро (MODULE_NAME="core") — так и задумано в logs/PROTOCOL.md:
клиент маленький, каждая независимая часть системы носит свою копию, а
не тянет общую зависимость. В отличие от rvs/classifier — этот файл
общий для всех подпапок system/core/ (module_init/, в будущем
tasks/ и т.п.), потому что они не независимые модули, а внутренние
части одного системного процесса ("ядро") — для внешнего наблюдателя
(логов) это одна система, не несколько.

Никакой сложной библиотеки: одна функция send_log(...), которая кидает
UDP-пакет с JSON на фиксированный адрес демона логов. Если демон логов
сейчас не запущен или недоступен — send_log() не бросает исключение и
не блокирует вызывающий код: событие просто теряется.
"""

import json
import socket

LOG_HOST = "127.0.0.1"
LOG_PORT = 47110

MODULE_NAME = "core"

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
        pass  # логирование не должно ронять ядро — ни из-за сети, ни из-за странных типов в data
