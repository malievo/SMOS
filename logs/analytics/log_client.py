"""
log_client.py — отправка событий в систему логирования SMOS из самого
аналитического слоя (см. logs/PROTOCOL.md).

Копия эталонного клиента под module="analytics". Нужен только watch-режиму:
он шлёт обратно в шину найденные аномалии (module="analytics",
message="anomaly_*") и периодические сводки (message="window_summary") —
их видно в logs/raw/analytics/events.jsonl и через тот же отчёт.

Как и у всех: не бросает исключений, не блокирует. trace_id
необязателен (у аномалий его обычно нет — они про окно, не про реплику).
"""

import json
import socket

LOG_HOST = "127.0.0.1"
LOG_PORT = 47110

MODULE_NAME = "analytics"

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def send_log(level: str, message: str, data: dict | None = None,
             trace_id: str | None = None) -> None:
    payload = {"module": MODULE_NAME, "level": level, "message": message}
    if data:
        payload["data"] = data
    if trace_id:
        payload["trace_id"] = trace_id
    try:
        _sock.sendto(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), (LOG_HOST, LOG_PORT))
    except (OSError, TypeError, ValueError):
        pass
