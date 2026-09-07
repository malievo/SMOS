"""
main.py — модуль "mod_shutdown": остановка и перезапуск самой SMOS.

Это НЕ про питание хоста — только про процессы SMOS. Что делает модуль:
  1. находит корень проекта по файлу-маркеру smos.root;
  2. запускает ОТВЯЗАННО `python3 smos.py stop|restart --timer N` —
     smos.py ждёт N секунд и гасит всё, что поднял launcher
     (SIGINT -> SIGTERM -> SIGKILL, см. smos.py::stop_pid; режимы
     merged и tmux оба покрыты). Задержка нужна, чтобы этот модуль
     успел вернуть ответ ядру и завершиться раньше, чем smos.py дойдёт
     до core — родителя модуля;
  3. возвращает ядру silent-результат — поверх гаснущей системы не надо
     проговаривать текстовое подтверждение (см.
     ../../core/task_runner/task_runner.py::_emit_completion, флаг
     silent, тот же приём у mod_playback) — и сразу выходит.

Почему отвязка обязательна. Ядро зовёт модуль через
`subprocess.run(timeout=5, capture_output=True)` (см.
../../core/planner/planner.py::call_module). Дочерний smos.py поэтому:
  - start_new_session=True — переживает смерть core;
  - stdin/stdout/stderr = DEVNULL — не держит пайпы ядра, иначе
    subprocess.run в planner зависнет на чтении дольше таймаута (та же
    «гонка за stdout/stderr», что описана в updater/updater_design.md).

Задержку по умолчанию (2 c) можно переопределить переменной окружения
SMOS_SHUTDOWN_DELAY_SEC.

Протокол вызова — ../../core/module_init/manifest_design.md:
    python3 main.py smos_stop '{}'
    -> {"status": "ok", "data": {"smos_stopped": {"silent": true, ...}}}
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from log_client import send_log

DEFAULT_DELAY_SEC = 2.0

# команда манифеста -> (подкоманда smos.py, ключ produces)
COMMANDS = {
    "smos_stop":    ("stop",    "smos_stopped"),
    "smos_restart": ("restart", "smos_restarted"),
}


def _project_root() -> Path:
    """Вверх от этого файла до папки с маркером smos.root (тот же
    маркер, по которому себя проверяет сам smos.py)."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "smos.root").exists():
            return parent
    raise RuntimeError("не найден корень проекта (файл-маркер smos.root)")


def _delay_sec() -> float:
    raw = os.environ.get("SMOS_SHUTDOWN_DELAY_SEC")
    if raw is None:
        return DEFAULT_DELAY_SEC
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_DELAY_SEC
    return value if value >= 0 else DEFAULT_DELAY_SEC


def _spawn_smos(root: Path, subcommand: str, delay: float) -> int:
    """Отвязанный `python3 smos.py <subcommand> --timer <delay>`.
    Возвращает pid. Ошибку запуска пробрасываем наружу — пусть ядро
    увидит error, а не тихий silent."""
    proc = subprocess.Popen(
        [sys.executable, str(root / "smos.py"), subcommand, "--timer", f"{delay:g}"],
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None

    if command not in COMMANDS:
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    subcommand, produce_key = COMMANDS[command]
    delay = _delay_sec()

    try:
        root = _project_root()
        pid = _spawn_smos(root, subcommand, delay)
    except (RuntimeError, OSError) as e:
        send_log("ERROR", "smos_shutdown_spawn_failed", {"command": command, "error": str(e)})
        print(json.dumps({"status": "error", "error": f"не удалось запустить smos.py: {e}"}, ensure_ascii=False))
        return

    send_log("INFO", "smos_shutdown_requested", {
        "command": command, "subcommand": subcommand, "delay_sec": delay, "smos_pid": pid,
    })

    # silent: система через delay секунд погаснет целиком, озвучивать
    # подтверждение поверх этого незачем.
    print(json.dumps({
        "status": "ok",
        "data": {produce_key: {"silent": True, "subcommand": subcommand, "delay_sec": delay}},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
