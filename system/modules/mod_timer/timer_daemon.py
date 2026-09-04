"""
timer_daemon.py — долгоживущий процесс одного таймера.

Не в манифесте mod_timer — частная деталь модуля. Порождается из
main.py (диспетчера) через Popen(start_new_session=True) и живёт сам,
пока не истечёт время или пока в папке сессии не появится файл "cancel"
(задел на отмену — в v1 его никто не создаёт).

  python3 timer_daemon.py <session_id>

Общение с остальной системой — только через файлы:
- читает  system/core/sessions/<id>/state.json  (fire_at, human);
- по истечении кладёт файл-напоминание в
  system/outputstructurizer/queue/  (та же форма, что пишет
  task_runner._emit_completion) — дальше озвучка идёт обычным путём;
- обновляет свой state.json: running -> ended / cancelled.

Это осознанный shortcut v1: демон пишет прямо в очередь
outputstructurizer, а не через ядро. Ядро таймер не ведёт (в реестре
сессий он есть только как файл). См. core_design.md.
"""

import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import timerlib  # noqa: E402
from log_client import send_log  # noqa: E402

STATE_WAIT_SEC = 5.0     # сколько ждём появления state.json от диспетчера
POLL_SEC = 1.0


def _load_state(state_file: Path) -> dict | None:
    deadline = time.time() + STATE_WAIT_SEC
    while time.time() < deadline:
        try:
            return json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            time.sleep(0.1)
    return None


def _emit_reminder(session_id: str, seconds: int, human: str) -> None:
    queue_dir = timerlib.outputstructurizer_queue(HERE)
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    record = {
        "task_id": f"timerfire_{session_id}",
        "goal": "timer_elapsed",
        "status": "done",
        "result": {"seconds": seconds, "human": human},
        "error": None,
        "state": {},
        "source_text": None,
        "ts": ts,
    }
    # session_id уже начинается с метки времени -> очередь читается по
    # порядку; суффикс отделяет напоминание от возможных будущих файлов
    # той же сессии.
    timerlib.atomic_write_json(queue_dir / f"{session_id}_fire.json", record)


def main() -> None:
    if len(sys.argv) < 2:
        send_log("ERROR", "timer_daemon_no_session_id")
        sys.exit(2)
    session_id = sys.argv[1]

    sdir = timerlib.sessions_dir(HERE) / session_id
    state_file = sdir / "state.json"
    cancel_file = sdir / "cancel"

    state = _load_state(state_file)
    if state is None:
        send_log("ERROR", "timer_daemon_no_state", {"session_id": session_id})
        sys.exit(1)

    try:
        fire_at = datetime.fromisoformat(state["fire_at"])
    except (KeyError, ValueError) as e:
        send_log("ERROR", "timer_daemon_bad_state", {"session_id": session_id, "error": str(e)})
        sys.exit(1)

    seconds = int(state.get("seconds", 0))
    human = state.get("human") or timerlib.human_duration(seconds)

    # Гашение снаружи (smos.py stop, выключение системы) шлёт SIGINT ->
    # SIGTERM. Оставляем в реестре честную запись, а не висящий "running"
    # (детект осиротевших сессий по признаку жизни — отдельная задача,
    # см. core_design.md).
    def _on_signal(signum, _frame):
        state["status"] = "stopped"
        state["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            timerlib.atomic_write_json(state_file, state)
        finally:
            send_log("INFO", "timer_stopped", {"session_id": session_id, "signal": signum})
            sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    send_log("INFO", "timer_daemon_started", {
        "session_id": session_id, "fire_at": state["fire_at"], "seconds": seconds,
    })

    while datetime.now().astimezone() < fire_at:
        if cancel_file.exists():
            state["status"] = "cancelled"
            state["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            timerlib.atomic_write_json(state_file, state)
            send_log("INFO", "timer_cancelled", {"session_id": session_id})
            return
        remaining = (fire_at - datetime.now().astimezone()).total_seconds()
        time.sleep(max(0.0, min(POLL_SEC, remaining)))

    _emit_reminder(session_id, seconds, human)

    state["status"] = "ended"
    state["ended_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    timerlib.atomic_write_json(state_file, state)
    send_log("INFO", "timer_elapsed", {"session_id": session_id, "seconds": seconds})


if __name__ == "__main__":
    main()
