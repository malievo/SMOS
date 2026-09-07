#!/usr/bin/env python3
"""
smos.py — единая точка входа SMOS: preflight-проверка, запуск и остановка
всех системных процессов одной командой.

Зачем: раньше для полного прогона приходилось вручную открывать консоль
на каждый процесс (logs, core, swl, classifier, req, wake), заходить в
его папку и запускать скрипт — минут семь только на старт. Этот скрипт
поднимает всех сразу, в нужном порядке, и гасит одной командой — в том
числе из ДРУГОЙ консоли и даже если сам launcher уже закрыт: состояние
лежит в launcher/run/state.json (НЕ в system/ — эта папка целиком
заменяется при обновлении апдейтером, см. updater/updater_design.md;
живи это состояние внутри system/, updater/apply стирал бы или
откатывал его при каждом обновлении, и smos.py "терял" бы из виду уже
запущенные процессы), так что осиротевший wake.py на микрофоне всегда
убирается через `python smos.py stop`.

Процессы SMOS (см. system/core/how_core_works.md, system/swl/swl_design.md):
  logs       logs/listener/listener.py            демон логов (UDP) — первым
  core       system/core/core.py                  ядро: очередь целей -> задачи
  outputstructurizer  system/outputstructurizer/outputstructurizer.py  результат задачи -> человеческая фраза
  cnps       system/cnps/cnps.py                  фраза -> класс важности + синтез + воспроизведение (заменил audio)
  swl        system/swl/swl.py                    команда -> структурированная цель
  classifier system/fwl/classifier/classifier.py  команда / разговор
  req        system/fwl/rvs/req.py                запись фразы -> текст (Google STT)
  wake       system/fwl/rvs/wake.py              wake-word + запись, владелец микрофона

Перед стартом остальных процессов (см. updater/updater_design.md):
  1. Поднимается logs (нужен апдейтеру для логирования себя, как и всем).
  2. Блокирующий вызов updater/updater.py check — сверяет версию с GitHub,
     при необходимости сам ведёт голосовое подтверждение: вопрос и
     объявления — готовые клипы system/sysaudio/ (play_clip, отдельным
     процессом), да/нет/отмена слушает через wake --nowake + req. Пути
     ко всем этим скриптам передаются апдейтеру через окружение
     (SMOS_*_SCRIPT) — smos.py остаётся единственным источником правды
     о раскладке system/, апдейтер её не дублирует.
  3. Результат: "updating" -> smos.py ничего больше не запускает и
     завершается (apply сам поднимет smos.py заново); "no_update" /
     "declined" -> обычный запуск всех процессов продолжается.
  --no-update пропускает эту проверку целиком.

Команды:
  python smos.py                      preflight + проверка обновления + запуск всех; общий вывод, Ctrl+C гасит всех
  python smos.py start --debugview    то же, но каждый процесс в своей панели tmux
  python smos.py start --only swl,core   поднять только перечисленные
  python smos.py start --skip wake,req   поднять все, кроме перечисленных
  python smos.py start --restart      поднимать упавший процесс заново (в merged — с backoff)
  python smos.py start --no-update    пропустить проверку обновления
  python smos.py stop                 погасить всё, что запускал launcher
  python smos.py stop --timer 2       подождать 2с и погасить (для остановки изнутри — модуль mod_shutdown)
  python smos.py status              кто жив, pid, uptime, режим классификатора
  python smos.py restart [флаги start]   (тоже принимает --timer SEC — задержка перед фазой стопа)
  python smos.py check              только preflight, ничего не запускать

Запускать из .venv, чтобы дети унаследовали интерпретатор:
    .venv/bin/python smos.py
--debugview требует tmux:  sudo apt install tmux
"""

import argparse
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if not (PROJECT_ROOT / "smos.root").exists():
    sys.exit("[smos] smos.py должен лежать в корне проекта (рядом с файлом-маркером smos.root)")

PYCHILD = sys.executable  # тем же интерпретатором запускаем всех детей — venv наследуется

# "Голос системы" (см. system/sysaudio/sysaudio_design.md) — вызывается
# отдельным процессом, той же схемой, что и все остальные компоненты
# SMOS (не in-process импортом — тот вариант архитектурно выбивался и
# на практике сразу дал коллизию модулей config/log_client между
# updater.py и sysaudio.py, см. updater_design.md).
SYSAUDIO_SCRIPT = PROJECT_ROOT / "system" / "sysaudio" / "sysaudio.py"


def play_clip(key: str, wait: bool = True) -> None:
    """Проигрывает системный клип sysaudio.py. wait=True — дождаться
    конца проигрывания; wait=False — не ждать. Любой сбой (нет скрипта,
    не запустился) — только в консоль, не роняет smos.py."""
    if not SYSAUDIO_SCRIPT.exists():
        print(f"[smos] sysaudio недоступен ({SYSAUDIO_SCRIPT}) — {key!r} только в лог")
        return
    try:
        if wait:
            subprocess.run(
                [PYCHILD, str(SYSAUDIO_SCRIPT), key], cwd=str(SYSAUDIO_SCRIPT.parent), timeout=20,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            # DEVNULL, не унаследованные — та же гигиена, что и у
            # updater.py play_clip/apply/_restart_smos (см.
            # updater_design.md, «Гонка за stdout/stderr при
            # capture_output»): здесь smos.py сам верхнеуровневый, так
            # что зависания не будет, но пусть привычка не заводит хвост.
            subprocess.Popen(
                [PYCHILD, str(SYSAUDIO_SCRIPT), key], cwd=str(SYSAUDIO_SCRIPT.parent),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[smos] не удалось проиграть клип {key!r}: {e}")


# ВАЖНО: вне system/ (см. докстринг модуля) — updater/apply целиком
# заменяет system/ при обновлении, и живи это состояние внутри неё,
# каждое обновление стирало бы/откатывало bookkeeping о том, что уже
# запущено (обнаружено на практике при разработке апдейтера).
RUN_DIR = PROJECT_ROOT / "launcher" / "run"
STATE_FILE = RUN_DIR / "state.json"
TMUX_SESSION = "smos"

PRINT_LOCK = threading.Lock()

LOG_HOST = "127.0.0.1"
LOG_PORT = 47110


def send_log(level: str, message: str, data: dict | None = None) -> None:
    """Тот же протокол, что и у log_client.py в каждом модуле (см.
    logs/PROTOCOL.md) — здесь встроено прямо в smos.py, а не отдельным
    файлом, потому что smos.py не модуль в system/, а сам процесс
    запуска. Единственный, кому это сейчас нужно от smos.py, —
    all_system_started ниже: апдейтер следит за ним при health-check
    после обновления (см. updater/updater_design.md)."""
    payload = {"module": "smos", "level": level, "message": message}
    if data:
        payload["data"] = data
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.sendto(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"), (LOG_HOST, LOG_PORT))
    except (OSError, TypeError, ValueError):
        pass  # логирование не должно ронять launcher — ни из-за сети, ни из-за странных типов в data


@dataclass
class Proc:
    name: str
    script: str            # путь относительно корня проекта
    requires: list[str] = field(default_factory=list)  # import-имена пакетов, без которых процесс не поднимется

    @property
    def abspath(self) -> Path:
        return PROJECT_ROOT / self.script

    @property
    def match(self) -> str:
        return os.path.basename(self.script)  # для сверки pid <-> /proc/<pid>/cmdline


# Порядок важен: демон логов первым (он биндит UDP-сокет; поднимется
# позже — стартовые логи остальных потеряются). Между прочими порядок по
# дизайну не критичен (файловые очереди, слежение по mtime).
PROCESSES = [
    Proc("logs", "logs/listener/listener.py"),
    Proc("core", "system/core/core.py"),
    Proc("outputstructurizer", "system/outputstructurizer/outputstructurizer.py", ["gigachat", "dotenv"]),
    # cnps заменил audio: приём заявок + класс важности + синтез по
    # предложениям + одна дорожка с вытеснением + TTL/Recap + управление
    # по UDP + пульс mute.flag. Питон-зависимостей для СТАРТА нет
    # (piper опционален -> откат на spd-say -> печать; numpy не нужен;
    # aplay/paplay — системные). См. system/cnps/cnps_design.md.
    Proc("cnps", "system/cnps/cnps.py"),
    Proc("swl", "system/swl/swl.py", ["gigachat", "dotenv"]),
    Proc("classifier", "system/fwl/classifier/classifier.py",
         ["gigachat", "dotenv", "sentence_transformers", "sklearn", "joblib", "numpy"]),
    Proc("req", "system/fwl/rvs/req.py", ["speech_recognition"]),
    Proc("wake", "system/fwl/rvs/wake.py", ["openwakeword", "pyaudio", "numpy"]),
]
BY_NAME = {p.name: p for p in PROCESSES}


# --------------------------------------------------------------------------
# Вспомогательное: время, /proc, сигналы
# --------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def uptime(started_at: str) -> str:
    try:
        delta = datetime.now().astimezone() - datetime.fromisoformat(started_at)
    except ValueError:
        return "?"
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


def is_alive(pid: int, match: str = "") -> bool:
    """Жив ли процесс pid и (если задан match) действительно ли это он —
    сверяем с /proc/<pid>/cmdline, чтобы не принять за него чужой процесс
    с переиспользованным номером."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    if not match:
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return True  # не Linux или нет /proc — доверяем os.kill
    return match in cmdline


def stop_pid(pid: int, match: str, label: str) -> str:
    """Гасит один pid по возрастающей: SIGINT (все демоны, кроме мелочей,
    ловят KeyboardInterrupt и логируют *_stopped) -> SIGTERM -> SIGKILL."""
    if not is_alive(pid, match):
        return "уже не запущен"
    for sig, wait in ((signal.SIGINT, 3.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return f"остановлен ({sig.name})"
        deadline = time.time() + wait
        while time.time() < deadline:
            if not is_alive(pid, match):
                return f"остановлен ({sig.name})"
            time.sleep(0.1)
    return "НЕ УМЕР даже после SIGKILL"


# --------------------------------------------------------------------------
# Состояние на диске
# --------------------------------------------------------------------------

def save_state(mode: str, procs: dict, tmux_session: str | None = None) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "launcher_pid": os.getpid(),
        "mode": mode,
        "tmux_session": tmux_session,
        "started_at": now_iso(),
        "python": PYCHILD,
        "procs": procs,
    }
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)  # атомарно — тот же приём, что и везде в проекте


def read_state() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_state() -> None:
    STATE_FILE.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# tmux
# --------------------------------------------------------------------------

def have_tmux() -> bool:
    return shutil.which("tmux") is not None


def tmux(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, check=check)


def tmux_has_session() -> bool:
    if not have_tmux():
        return False
    return subprocess.run(["tmux", "has-session", "-t", TMUX_SESSION],
                          capture_output=True).returncode == 0


def tmux_shell_cmd(script: str) -> str:
    # exec — чтобы pid панели стал pid питона (а не промежуточного sh)
    return f'cd "{PROJECT_ROOT}" && exec "{PYCHILD}" -u "{script}"'


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

def classifier_mode() -> str:
    base = PROJECT_ROOT / "system" / "fwl" / "classifier"
    if (base / "flags" / "offline_only.flag").exists():
        return "OFFLINE"
    if (base / "flags" / "promoted.flag").exists():
        return "VALIDATING"
    if (base / "output" / "local_model.joblib").exists():
        return "SHADOW"
    return "BOOTSTRAP"


def _has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def preflight(selected: list[Proc]) -> bool:
    print("— preflight —")
    ok = True

    in_venv = sys.prefix != sys.base_prefix
    print(f"  python     : {PYCHILD}  ({'venv' if in_venv else 'НЕ venv'})")
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_py.exists() and venv_py.resolve() != Path(PYCHILD).resolve():
        print(f"  ⚠ запущен не из .venv — лучше:  {venv_py} smos.py")

    cfg_dir = PROJECT_ROOT / "user" / "configs"
    for cfg in sorted(cfg_dir.glob("*.json")):
        try:
            json.loads(cfg.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"  ⚠ {cfg.relative_to(PROJECT_ROOT)}: битый JSON ({e}) — скрипт откатится на DEFAULTS")

    mode = classifier_mode()
    env_file = PROJECT_ROOT / "user" / ".env"
    has_key = env_file.exists() and "GIGACHAT_CREDENTIALS" in env_file.read_text(encoding="utf-8", errors="replace")
    sel_names = {p.name for p in selected}
    needs_key = "swl" in sel_names or ("classifier" in sel_names and mode != "OFFLINE")
    if needs_key and not has_key:
        print("  ⚠ нет GIGACHAT_CREDENTIALS в user/.env — swl и bootstrap-классификатор не разберут фразу")

    for p in selected:
        missing = [m for m in p.requires if not _has_module(m)]
        if missing:
            ok = False
            print(f"  ✗ {p.name:<10}: не хватает пакетов: {', '.join(missing)}")
        else:
            print(f"  ✓ {p.name}")

    print(f"  классификатор: режим {mode}")
    if shutil.which("tmux") is None:
        print("  (tmux не найден — --debugview недоступен:  sudo apt install tmux)")
    print("—" * 12)
    return ok


# --------------------------------------------------------------------------
# Выбор подмножества процессов
# --------------------------------------------------------------------------

def select_processes(only: str, skip: str) -> list[Proc]:
    sel = list(PROCESSES)
    if only:
        want = [n.strip() for n in only.split(",") if n.strip()]
        bad = [n for n in want if n not in BY_NAME]
        if bad:
            sys.exit(f"[smos] неизвестные процессы в --only: {', '.join(bad)}  (есть: {', '.join(BY_NAME)})")
        sel = [p for p in PROCESSES if p.name in want]
    if skip:
        drop = [n.strip() for n in skip.split(",") if n.strip()]
        bad = [n for n in drop if n not in BY_NAME]
        if bad:
            sys.exit(f"[smos] неизвестные процессы в --skip: {', '.join(bad)}")
        sel = [p for p in sel if p.name not in drop]
    if not sel:
        sys.exit("[smos] после --only/--skip не осталось ни одного процесса")
    return sel


# --------------------------------------------------------------------------
# Проверка обновления (updater/updater.py check) — до запуска остальных
# процессов. См. updater/updater_design.md.
# --------------------------------------------------------------------------

UPDATER_SCRIPT = PROJECT_ROOT / "updater" / "updater.py"
# Щедрый потолок на весь вызов check (включая возможный голосовой диалог) —
# предохранитель на случай, если что-то внутри апдейтера зависло; сам
# апдейтер должен укладываться в свои внутренние таймауты намного раньше.
UPDATE_CHECK_TIMEOUT_SEC = 180


def run_update_check(selected: list[Proc]) -> str:
    """Блокирующий вызов updater.py check. Возвращает "no_update" /
    "declined" / "updating" — при любой накладке (апдейтера нет, упал,
    вернул не то, завис) безопасный дефолт "no_update", чтобы сбой
    проверки обновления никогда не мешал обычному запуску системы.

    Поднимает logs на время проверки (апдейтеру нужно кому-то
    логировать себя), если logs вообще есть в selected — временный
    процесс, не входит в state.json, гасится перед возвратом; ниже по
    списку PROCESSES его поднимет обычный start_merged/start_tmux."""
    if not UPDATER_SCRIPT.exists():
        return "no_update"

    logs_def = BY_NAME.get("logs")
    logs_child = None
    if logs_def is not None and any(p.name == "logs" for p in selected):
        print("[smos] запускаю logs (нужен апдейтеру для логирования)...")
        logs_child = _spawn_merged(logs_def)
        time.sleep(0.7)

    env = {
        **os.environ,
        "SMOS_PYTHON": PYCHILD,
        "SMOS_SYSAUDIO_SCRIPT": str(SYSAUDIO_SCRIPT),
    }
    for env_key, proc_name in (
        ("SMOS_WAKE_SCRIPT", "wake"), ("SMOS_REQ_SCRIPT", "req"),
    ):
        proc_def = BY_NAME.get(proc_name)
        if proc_def is not None:
            env[env_key] = str(proc_def.abspath)

    print("[smos] проверяю обновления...")
    outcome = "no_update"
    try:
        result = subprocess.run(
            [PYCHILD, str(UPDATER_SCRIPT), "check"],
            cwd=str(UPDATER_SCRIPT.parent),
            env=env, capture_output=True, text=True, timeout=UPDATE_CHECK_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(f"[smos] проверка обновления не уложилась в {UPDATE_CHECK_TIMEOUT_SEC}с — пропускаю")
    else:
        for line in result.stderr.splitlines():
            print(f"updater    │ {line}")
        try:
            outcome = json.loads(result.stdout.strip())["result"]
        except (json.JSONDecodeError, KeyError, AttributeError):
            print(f"[smos] апдейтер вернул непонятный ответ (stdout={result.stdout!r}) — пропускаю")

    if logs_child is not None:
        _shutdown_merged({"logs": logs_child})

    return outcome


# --------------------------------------------------------------------------
# start: merged (общий вывод в одну консоль)
# --------------------------------------------------------------------------

def _spawn_merged(p: Proc) -> subprocess.Popen:
    proc = subprocess.Popen(
        [PYCHILD, "-u", str(p.abspath)],
        cwd=str(p.abspath.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    threading.Thread(target=_pump, args=(p.name, proc.stdout), daemon=True).start()
    return proc


def _pump(name: str, stream) -> None:
    for line in stream:
        with PRINT_LOCK:
            sys.stdout.write(f"{name:<10}│ {line.rstrip()}\n")
            sys.stdout.flush()


def start_merged(selected: list[Proc], auto_restart: bool) -> None:
    children: dict[str, subprocess.Popen] = {}
    procs_state: dict[str, dict] = {}
    stop_event = threading.Event()

    def snapshot() -> None:
        for name, cp in children.items():
            procs_state.setdefault(name, {})
            procs_state[name].update(
                pid=cp.pid, script=BY_NAME[name].script,
                cmdline_match=BY_NAME[name].match,
            )
            procs_state[name].setdefault("started_at", now_iso())
        save_state("merged", procs_state)

    def handle_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print(f"[smos] запускаю: {', '.join(p.name for p in selected)}")
    for p in selected:
        children[p.name] = _spawn_merged(p)
        procs_state[p.name] = {"pid": children[p.name].pid, "script": p.script,
                               "cmdline_match": p.match, "started_at": now_iso()}
        save_state("merged", procs_state)
        time.sleep(0.7 if p.name == "logs" else 0.3)

    print("[smos] все запущены. Ctrl+C — остановить всех.\n")

    # Живы ли все сразу после старта? Не то же самое, что "готовы" — просто
    # "хотя бы не упали немедленно". Апдейтер следит именно за этим логом
    # при health-check после обновления (см. updater/updater_design.md) —
    # поэтому шлём его один раз, здесь, а не полагаемся на то, что кто-то
    # другой досмотрит своё собственное состояние за всех. Секунда — и
    # для этой проверки, и чтобы "система запущена" не звучало раньше,
    # чем реально всё поднялось (см. system/sysaudio/sysaudio_design.md).
    time.sleep(1.0)
    alive_names = [p.name for p in selected if children.get(p.name) is not None and children[p.name].poll() is None]
    if len(alive_names) == len(selected):
        send_log("INFO", "all_system_started", {"processes": alive_names})
        play_clip("system_started", wait=False)
    else:
        missing = [p.name for p in selected if p.name not in alive_names]
        send_log("WARNING", "all_system_started_incomplete", {"missing": missing})

    restart_hist: dict[str, list[float]] = {p.name: [] for p in selected}

    while not stop_event.is_set():
        time.sleep(1.0)
        for p in selected:
            cp = children.get(p.name)
            if cp is None or cp.poll() is None:
                continue
            rc = cp.returncode
            with PRINT_LOCK:
                print(f"\n{'!' * 4} {p.name} завершился (код {rc}) {'!' * 4}\n")
            children.pop(p.name, None)
            if auto_restart and not stop_event.is_set():
                hist = restart_hist[p.name]
                nowt = time.time()
                hist[:] = [t for t in hist if nowt - t < 60]
                if len(hist) >= 5:
                    print(f"{'!' * 4} {p.name}: 5 падений за минуту — больше не поднимаю {'!' * 4}")
                    continue
                delay = min(2 ** len(hist), 30)
                print(f"[smos] перезапуск {p.name} через {delay}s")
                if stop_event.wait(delay):
                    break
                hist.append(time.time())
                children[p.name] = _spawn_merged(p)
                snapshot()
        if not children:
            print("[smos] все процессы завершились — выходим")
            break

    _shutdown_merged(children)
    clear_state()


def _shutdown_merged(children: dict[str, subprocess.Popen]) -> None:
    alive = {n: c for n, c in children.items() if c.poll() is None}
    if not alive:
        return
    print(f"\n[smos] останавливаю: {', '.join(alive)}")
    for c in alive.values():
        c.send_signal(signal.SIGINT)
    for sig, wait in ((None, 3.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
        deadline = time.time() + wait
        while time.time() < deadline and any(c.poll() is None for c in alive.values()):
            time.sleep(0.1)
        remaining = [c for c in alive.values() if c.poll() is None]
        if not remaining:
            break
        if sig is not None:
            for c in remaining:
                c.send_signal(sig)
    for n, c in alive.items():
        print(f"  {n}: код {c.poll()}")


# --------------------------------------------------------------------------
# start: debugview (tmux, панель на процесс)
# --------------------------------------------------------------------------

def start_tmux(selected: list[Proc], auto_restart: bool, attach: bool, force: bool) -> None:
    if shutil.which("tmux") is None:
        sys.exit("[smos] --debugview требует tmux:  sudo apt install tmux")
    if tmux_has_session():
        if not force:
            sys.exit(f"[smos] сессия tmux '{TMUX_SESSION}' уже запущена. "
                     f"`python smos.py stop`  или  `tmux attach -t {TMUX_SESSION}`  (или --force)")
        tmux("kill-session", "-t", TMUX_SESSION)

    first, rest = selected[0], selected[1:]
    tmux("new-session", "-d", "-s", TMUX_SESSION, "-x", "250", "-y", "60",
         "-n", "smos", tmux_shell_cmd(first.script))
    tmux("set-option", "-t", TMUX_SESSION, "remain-on-exit", "on")  # упавший процесс оставляет вывод на экране
    tmux("set-option", "-t", TMUX_SESSION, "mouse", "on")
    tmux("select-pane", "-t", f"{TMUX_SESSION}:0.0", "-T", first.name)
    time.sleep(0.7)

    for p in rest:
        tmux("split-window", "-t", TMUX_SESSION, tmux_shell_cmd(p.script))
        tmux("select-layout", "-t", TMUX_SESSION, "tiled")
        tmux("select-pane", "-t", TMUX_SESSION, "-T", p.name)
        time.sleep(0.3)

    tmux("set-option", "-t", TMUX_SESSION, "pane-border-status", "top")
    if auto_restart:
        # tmux сам поднимет умершую панель. Без backoff — жёстко
        # крашащийся процесс будет крутиться (осознанно, это --restart).
        tmux("set-hook", "-t", TMUX_SESSION, "pane-died", "respawn-pane")

    panes = tmux("list-panes", "-t", TMUX_SESSION, "-F", "#{pane_index} #{pane_pid}").stdout.splitlines()
    pid_by_index = {int(x.split()[0]): int(x.split()[1]) for x in panes if x.strip()}
    procs_state = {
        p.name: {
            "pid": pid_by_index.get(i, 0),
            "script": p.script,
            "cmdline_match": p.match,
            "started_at": now_iso(),
        }
        for i, p in enumerate(selected)
    }
    save_state("tmux", procs_state, tmux_session=TMUX_SESSION)

    # В отличие от start_merged, тут нет прямых Popen на каждый процесс,
    # чтобы дёшево проверить .poll() — best-effort, без проверки живости
    # (apply никогда не перезапускает smos.py в этом режиме, так что для
    # health-check апдейтера это не критично, см. updater_design.md).
    time.sleep(1.0)
    send_log("INFO", "all_system_started", {"processes": [p.name for p in selected]})
    play_clip("system_started", wait=False)

    print(f"[smos] сессия tmux '{TMUX_SESSION}' поднята — {len(selected)} панелей "
          f"({', '.join(p.name for p in selected)})")
    print(f"[smos] остановить:  python smos.py stop")
    if attach:
        os.execvp("tmux", ["tmux", "attach", "-t", TMUX_SESSION])
    print(f"[smos] подключиться: tmux attach -t {TMUX_SESSION}")


def stop_tmux_session() -> None:
    if not tmux_has_session():
        print("  сессия tmux уже не запущена")
        return
    panes = tmux("list-panes", "-t", TMUX_SESSION, "-F", "#{pane_id}").stdout.split()
    for pane in panes:
        subprocess.run(["tmux", "send-keys", "-t", pane, "C-c"], capture_output=True)
    time.sleep(2.0)
    tmux("kill-session", "-t", TMUX_SESSION)
    print(f"  сессия tmux '{TMUX_SESSION}' убита")


# --------------------------------------------------------------------------
# stop / status / restart
# --------------------------------------------------------------------------

def cmd_stop(delay: float = 0.0) -> None:
    # delay > 0: подождать перед остановкой. Нужно, когда stop запускает
    # сам SMOS изнутри (модуль mod_shutdown) — модулю-инициатору и его
    # ответу по цепочке (core -> outputstructurizer) надо успеть
    # завершиться раньше, чем мы дойдём до их процессов. Задержку задаёт
    # вызывающий (`smos.py stop --timer N`), тут просто ждём.
    if delay > 0:
        print(f"[smos] остановка через {delay:g}с...")
        send_log("INFO", "stop_delayed", {"delay_sec": delay})
        time.sleep(delay)

    state = read_state()
    if state is None:
        print("[smos] state.json нет — launcher ничего не запускал")
        if tmux_has_session():
            print(f"[smos] но сессия tmux '{TMUX_SESSION}' жива — гашу её")
            stop_tmux_session()
        return

    print(f"[smos] останавливаю (режим {state['mode']}, старт {state.get('started_at', '?')})")
    if state["mode"] == "tmux":
        stop_tmux_session()
    else:
        for name, info in state["procs"].items():
            res = stop_pid(info["pid"], info.get("cmdline_match", ""), name)
            print(f"  {name:<10} pid {info['pid']:<8} {res}")
        lp = state.get("launcher_pid")
        if lp and is_alive(lp, "smos.py"):
            try:
                os.kill(lp, signal.SIGTERM)
            except ProcessLookupError:
                pass
    clear_state()
    print("[smos] готово")


def cmd_status() -> None:
    print(f"классификатор: режим {classifier_mode()}")
    state = read_state()
    if state is None:
        print("launcher: ничего не запущено")
        if tmux_has_session():
            print(f"(но сессия tmux '{TMUX_SESSION}' существует — `tmux attach -t {TMUX_SESSION}`)")
        return
    print(f"режим запуска: {state['mode']}   старт: {state.get('started_at', '?')}   "
          f"launcher pid: {state.get('launcher_pid', '?')}")
    for name, info in state["procs"].items():
        live = is_alive(info["pid"], info.get("cmdline_match", ""))
        print(f"  {name:<10} pid {info['pid']:<8} "
              f"{'РАБОТАЕТ' if live else 'мёртв   '}  {uptime(info.get('started_at', ''))}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def add_start_flags(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--debugview", action="store_true", help="каждый процесс в своей панели tmux")
    sp.add_argument("--only", default="", metavar="A,B", help="поднять только эти процессы")
    sp.add_argument("--skip", default="", metavar="A,B", help="поднять все, кроме этих")
    sp.add_argument("--restart", action="store_true", dest="auto_restart",
                    help="поднимать упавший процесс заново")
    sp.add_argument("--no-attach", action="store_true", help="--debugview: не подключаться к tmux сразу")
    sp.add_argument("--force", action="store_true", help="игнорировать уже запущенную сессию/состояние")
    sp.add_argument("--no-update", action="store_true", help="пропустить проверку обновления")


def run_start(args: argparse.Namespace) -> None:
    selected = select_processes(args.only, args.skip)
    ok = preflight(selected)
    if not ok:
        sys.exit("[smos] preflight не пройден — поставьте недостающие пакеты и повторите "
                 "(или --skip проблемный процесс)")

    existing = read_state()
    if existing and not args.force:
        live = [n for n, i in existing["procs"].items() if is_alive(i["pid"], i.get("cmdline_match", ""))]
        if live or (existing["mode"] == "tmux" and tmux_has_session()):
            sys.exit(f"[smos] похоже, уже запущено ({', '.join(live) or 'tmux'}). "
                     f"`python smos.py stop` или `python smos.py start --force`")

    if not args.no_update:
        outcome = run_update_check(selected)
        if outcome == "updating":
            print("[smos] апдейтер применяет обновление — завершаюсь, дождитесь автоматического перезапуска")
            return

    if args.debugview:
        start_tmux(selected, args.auto_restart, attach=not args.no_attach, force=args.force)
    else:
        start_merged(selected, args.auto_restart)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] not in ("-h", "--help") and argv[0].startswith("-"):
        argv = ["start", *argv]  # флаги без подкоманды -> start ... флаги
    elif not argv:
        argv = ["start"]         # голый вызов -> start

    ap = argparse.ArgumentParser(prog="smos.py", description="Единая точка входа SMOS.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    add_start_flags(sub.add_parser("start", help="preflight + запуск всех процессов"))
    restart_p = sub.add_parser("restart", help="stop, затем start с теми же флагами")
    add_start_flags(restart_p)
    restart_p.add_argument("--timer", type=float, default=0.0, metavar="SEC",
                           help="подождать SEC секунд перед фазой остановки")
    stop_p = sub.add_parser("stop", help="погасить всё, что запускал launcher")
    stop_p.add_argument("--timer", type=float, default=0.0, metavar="SEC",
                        help="подождать SEC секунд перед остановкой "
                             "(инициатор-модуль успевает корректно завершиться)")
    sub.add_parser("status", help="кто жив, pid, uptime")
    sub.add_parser("check", help="только preflight")

    args = ap.parse_args(argv)

    if args.cmd == "check":
        sys.exit(0 if preflight(select_processes("", "")) else 1)
    if args.cmd == "status":
        cmd_status()
        return
    if args.cmd == "stop":
        cmd_stop(args.timer)
        return
    if args.cmd == "restart":
        cmd_stop(args.timer)
        time.sleep(1.0)
        run_start(args)
        return
    run_start(args)


if __name__ == "__main__":
    main()
