"""
main.py — модуль "mod_playback": управление воспроизведением CNPS.

Модуль-компаньон CNPS (system/cnps/cnps_design.md, раздел «Управление
воспроизведением и модуль-компаньон»). Ядро вызывает его, когда SWL
распознал цель вида «останови / повтори / продолжи / тише». Модуль:
  1. шлёт CNPS управляющий UDP-фрейм {"op": ...} на control-порт;
  2. возвращает ядру результат с флагом silent — чтобы
     outputstructurizer НЕ проговорил ещё и текстовое подтверждение
     («останавливаю») поверх того, что только что оборвали
     (см. core/task_runner._emit_completion).

CNPS работает и без этого модуля — тогда командовать воспроизведением
просто нельзя (голосовой barge-in во время озвучки — вообще v2).

Протокол вызова — system/core/module_init/manifest_design.md:
    python3 main.py playback_stop '{}'
    -> {"status": "ok", "data": {"playback_stopped": {"silent": true, ...}}}

Порт CNPS: по умолчанию 47120 (совпадает с control_udp_port в
user/configs/cnps.json). Переопределяется переменной окружения
SMOS_CNPS_CONTROL_PORT.
"""

import json
import os
import socket
import sys

from log_client import send_log

CNPS_HOST = os.environ.get("SMOS_CNPS_CONTROL_HOST", "127.0.0.1")
CNPS_PORT = int(os.environ.get("SMOS_CNPS_CONTROL_PORT", "47120"))

# команда манифеста -> (op для CNPS, ключ produces)
COMMANDS = {
    "playback_stop":     ("stop",     "playback_stopped"),
    "playback_pause":    ("pause",    "playback_paused"),
    "playback_resume":   ("resume",   "playback_resumed"),
    "playback_skip":     ("skip",     "playback_skipped"),
    "playback_repeat":   ("repeat",   "playback_repeated"),
    "playback_continue": ("continue", "playback_continued"),
    "playback_louder":   ("louder",   "playback_louder_done"),
    "playback_quieter":  ("quieter",  "playback_quieter_done"),
}


def _send(op: str, amount) -> bool:
    """Один UDP-датаграмм с фреймом управления. Ошибка сети/сокета — не
    исключение наружу: возвращаем sent=False, ядро всё равно получит
    silent-ответ (переспрашивать пользователя не за что)."""
    frame = {"op": op, "source": "mod_playback"}
    if isinstance(amount, (int, float)):
        frame["amount"] = amount
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(json.dumps(frame, ensure_ascii=False).encode("utf-8"), (CNPS_HOST, CNPS_PORT))
        return True
    except OSError as e:
        send_log("WARNING", "cnps_control_send_failed", {"op": op, "error": str(e)})
        return False


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    except (json.JSONDecodeError, IndexError):
        params = {}
    if not isinstance(params, dict):
        params = {}

    if command not in COMMANDS:
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    op, produce_key = COMMANDS[command]
    sent = _send(op, params.get("amount"))
    send_log("INFO", "playback_command", {"op": op, "sent": sent})

    # silent: не проговаривать текстовое подтверждение поверх оборванного.
    print(json.dumps({
        "status": "ok",
        "data": {produce_key: {"silent": True, "op": op, "sent": sent}},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
