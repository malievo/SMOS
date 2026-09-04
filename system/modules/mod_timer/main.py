"""
main.py — модуль "mod_timer": точка входа (одноразовый диспетчер).

Сессионный модуль (см. ../../core/module_init/manifest_design.md и
../../core/core_design.md, раздел «Долгоживущие модули: сессии»). Ядро
вызывает эту точку входа как обычный одноразовый модуль:

  python3 main.py set_timer '{"timer_duration": "10 минут"}'
  -> {"status": "ok", "data": {"timer_set": {...}}}

Но вместо того чтобы что-то посчитать и выйти, диспетчер ПОРОЖДАЕТ
отдельный долгоживущий процесс timer_daemon.py (его нет в манифесте —
это частная деталь модуля), регистрирует сессию в
system/core/sessions/<id>/state.json и сразу выходит. Ядро при этом
не держит долгоживущих детей — механизм вызова для него обычный.

По истечении времени демон сам кладёт файл-напоминание в очередь
outputstructurizer (system/outputstructurizer/queue/), и ответ
озвучивается тем же путём, что подтверждения команд.

Разбор длительности ("10 минут" -> 600) делает сам модуль (timerlib),
чтобы не зависеть от того, вернул ли SWL число или строку.
"""

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import timerlib  # noqa: E402
from log_client import send_log  # noqa: E402


def _new_session_id() -> str:
    return f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def set_timer(raw_duration) -> dict:
    seconds = timerlib.parse_duration(raw_duration)
    if seconds is None:
        return {"status": "error", "error": f"не понял длительность: {raw_duration!r}"}

    session_id = _new_session_id()
    sdir = timerlib.sessions_dir(HERE) / session_id
    started_at = datetime.now().astimezone()
    fire_at = started_at + timedelta(seconds=seconds)
    human = timerlib.human_duration(seconds)

    # Демон порождаем ДО записи state.json, чтобы сразу знать pid.
    # start_new_session=True — процесс отвязан от диспетчера (переживёт
    # его выход, не поймает сигналы группы). Вывод в никуда: демон
    # общается через файлы (state.json, очередь) и логи по UDP.
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "timer_daemon.py"), session_id],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ},
    )

    state = {
        "session_id": session_id,
        "module": "mod_timer",
        "kind": "timer",
        "status": "running",
        "pid": proc.pid,
        "started_at": started_at.isoformat(timespec="seconds"),
        "fire_at": fire_at.isoformat(timespec="seconds"),
        "seconds": seconds,
        "human": human,
        "control_inbox": str(sdir),  # сюда демон смотрит на файл "cancel" (задел на отмену)
    }
    timerlib.atomic_write_json(sdir / "state.json", state)

    send_log("INFO", "timer_set", {
        "session_id": session_id, "seconds": seconds, "fire_at": state["fire_at"], "pid": proc.pid,
    })

    return {"status": "ok", "data": {"timer_set": {
        "seconds": seconds,
        "human": human,
        "session_id": session_id,
        "fire_at": state["fire_at"],
    }}}


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error", "error": f"параметры не JSON: {e}"}, ensure_ascii=False))
        return

    if command != "set_timer":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    raw = params.get("timer_duration")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        print(json.dumps({"status": "missing", "missing": ["timer_duration"]}, ensure_ascii=False))
        return

    try:
        result = set_timer(raw)
    except Exception as e:  # noqa: BLE001 — модуль обязан вернуть JSON, а не упасть с трейсбеком
        send_log("ERROR", "timer_set_failed", {"error": str(e), "raw": str(raw)})
        print(json.dumps({"status": "error", "error": f"не удалось поставить таймер: {e}"}, ensure_ascii=False))
        return

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
