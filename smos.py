#!/usr/bin/env python3
"""
smos.py — единая точка входа SMOS: preflight-проверка, запуск и остановка
всех системных процессов одной командой.

Зачем: раньше для полного прогона приходилось вручную открывать консоль
на каждый процесс (logs, core, swl, classifier, req, wake), заходить в
его папку и запускать скрипт — минут семь только на старт. Этот скрипт
поднимает всех сразу, в нужном порядке, и гасит одной командой — в том
числе из ДРУГОЙ консоли и даже если сам launcher уже закрыт: состояние
лежит в system/launcher/run/state.json, так что осиротевший wake.py на
микрофоне всегда убирается через `python smos.py stop`.

Процессы SMOS (см. system/core/how_core_works.md, system/swl/swl_design.md):
  logs       logs/listener/listener.py            демон логов (UDP) — первым
  core       system/core/core.py                  ядро: очередь целей -> задачи
  outputstructurizer  system/outputstructurizer/outputstructurizer.py  результат задачи -> человеческая фраза
  audio      system/audio/audio.py                фраза -> озвучка (spd-say), v1
  swl        system/swl/swl.py                    команда -> структурированная цель
  classifier system/fwl/classifier/classifier.py  команда / разговор
  req        system/fwl/rvs/req.py                запись фразы -> текст (Google STT)
  wake       system/fwl/rvs/wake.py              wake-word + запись, владелец микрофона

Команды:
  python smos.py                      preflight + запуск всех; общий вывод, Ctrl+C гасит всех
  python smos.py start --debugview    то же, но каждый процесс в своей панели tmux
  python smos.py start --only swl,core   поднять только перечисленные
  python smos.py start --skip wake,req   поднять все, кроме перечисленных
  python smos.py start --restart      поднимать упавший процесс заново (в merged — с backoff)
  python smos.py stop                 погасить всё, что запускал launcher
  python smos.py status              кто жив, pid, uptime, режим классификатора
  python smos.py restart [флаги start]
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
RUN_DIR = PROJECT_ROOT / "system" / "launcher" / "run"
STATE_FILE = RUN_DIR / "state.json"
TMUX_SESSION = "smos"

PRINT_LOCK = threading.Lock()


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
    Proc("audio", "system/audio/audio.py"),
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
# Сессии (долгоживущие модули: таймер, музыка, ...)
# --------------------------------------------------------------------------

SESSIONS_DIR = PROJECT_ROOT / "system" / "core" / "sessions"


def read_sessions() -> list[dict]:
    """Живые записи сессий из system/core/sessions/<id>/state.json.
    Сессию порождает точка входа сессионного модуля (см.
    system/core/core_design.md, раздел «Долгоживущие модули»); ядро её
    не держит в памяти — только файл. Пока это скан папки, а не чтение
    реестра: registry поле `session` в v1 не пробрасывает."""
    out = []
    if not SESSIONS_DIR.exists():
        return out
    for st in sorted(SESSIONS_DIR.glob("*/state.json")):
        try:
            info = json.loads(st.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        info["_dir"] = st.parent.name
        out.append(info)
    return out


def stop_sessions() -> None:
    """Гасит все живые сессии — тем же путём, что и обычные процессы
    (SIGINT -> SIGTERM -> SIGKILL), pid берётся из их state.json."""
    live = [s for s in read_sessions()
            if s.get("status") == "running" and s.get("pid") and is_alive(s["pid"])]
    if not live:
        return
    print(f"[smos] гашу сессии: {', '.join(s.get('module', s['_dir']) for s in live)}")
    for s in live:
        res = stop_pid(s["pid"], "", s.get("module", s["_dir"]))
        print(f"  сессия {s['_dir']:<24} pid {s['pid']:<8} {res}")


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
    stop_sessions()
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

def cmd_stop() -> None:
    state = read_state()
    if state is None:
        print("[smos] state.json нет — launcher ничего не запускал")
        if tmux_has_session():
            print(f"[smos] но сессия tmux '{TMUX_SESSION}' жива — гашу её")
            stop_tmux_session()
        stop_sessions()  # осиротевший таймер мог пережить исчезновение state.json
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
    stop_sessions()
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

    sessions = read_sessions()
    if sessions:
        print("сессии:")
        for s in sessions:
            live = s.get("pid") and is_alive(s["pid"])
            mark = "РАБОТАЕТ" if (live and s.get("status") == "running") else s.get("status", "?")
            extra = f"  сработает {s['fire_at']}" if s.get("fire_at") and s.get("status") == "running" else ""
            print(f"  {s.get('module', s['_dir']):<12} {s['_dir']:<24} pid {s.get('pid', '?'):<8} {mark}{extra}")


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
    add_start_flags(sub.add_parser("restart", help="stop, затем start с теми же флагами"))
    sub.add_parser("stop", help="погасить всё, что запускал launcher")
    sub.add_parser("status", help="кто жив, pid, uptime")
    sub.add_parser("check", help="только preflight")

    args = ap.parse_args(argv)

    if args.cmd == "check":
        sys.exit(0 if preflight(select_processes("", "")) else 1)
    if args.cmd == "status":
        cmd_status()
        return
    if args.cmd == "stop":
        cmd_stop()
        return
    if args.cmd == "restart":
        cmd_stop()
        time.sleep(1.0)
        run_start(args)
        return
    run_start(args)


if __name__ == "__main__":
    main()
