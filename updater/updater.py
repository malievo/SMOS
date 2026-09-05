"""
updater.py — апдейтер SMOS.

Полный дизайн — updater_design.md рядом с этим файлом. Контракт со
smos.py:

    subprocess.run([python, "updater/updater.py", "check"], env=...)

- **stdout** — РОВНО одна строка, JSON `{"result": "..."}`. Ничего
  больше в stdout не печатается (по той же дисциплине, что и модули
  system/modules/* — один JSON-объект и всё), чтобы вызывающая сторона
  могла просто распарсить stdout процесса, не выцепляя нужную строку
  из потока отладочного вывода. Весь человекочитаемый прогресс — в
  stderr (см. _log) и в лог демона (send_log).
- **result**:
    "no_update" — версии совпадают / сеть недоступна / enabled: false
                  в конфиге / owner-repo не заполнены.
    "declined"  — было доступно обновление, пользователь сказал
                  нет/отмена, либо не ответил за отведённое время.
    "updating"  — пользователь подтвердил, apply уже запущен отдельным
                  отсоединённым процессом. smos.py в этом случае не
                  должен запускать остальные процессы — apply сам
                  поднимет smos.py заново по окончании.

Пути к wake.py/req.py/sysaudio.py и интерпретатору апдейтер не
хардкодит — получает их от smos.py через окружение (SMOS_WAKE_SCRIPT,
SMOS_REQ_SCRIPT, SMOS_SYSAUDIO_SCRIPT, SMOS_PYTHON). Единственное
место, которое обязано знать реальную раскладку system/, — таблица
PROCESSES в smos.py; апдейтер её не дублирует (см. updater_design.md,
раздел «Кто чем владеет»). Если переменные не заданы (ручной запуск
без smos.py, для отладки) — угадывает стандартные пути относительно
корня проекта.

Вопрос про обновление и объявления апдейтера озвучиваются готовыми
клипами `system/sysaudio/` (play_clip — отдельным процессом, той же
схемой, что и wake/req, см. play_clip() ниже), не синтезом на лету —
версии/notes остаются только в логе (см. sysaudio_design.md, «Одно
решение, которое надо принять» — вариант 1). Раньше апдейтер сам
поднимал audio.py и клал заявку в его tasks/ (_speak_and_wait) — с
sysaudio это не нужно вообще.

Ручной прогон без smos.py:
    python3 updater/updater.py check
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
from log_client import send_log  # noqa: E402

CFG = config.load(SCRIPT_DIR)

# updater/ по дизайну лежит прямо в корне проекта (см. updater_design.md,
# раздел «Зачем updater/ вне system/») — поэтому, в отличие от config.py,
# путь к корню не нужно искать вверх по маркеру, он всегда на уровень выше.
PROJECT_ROOT = SCRIPT_DIR.parent
if not (PROJECT_ROOT / "smos.root").exists():
    sys.exit("[updater] updater.py должен лежать в папке updater/ прямо в корне проекта (рядом с smos.root)")

INFO_FILE = PROJECT_ROOT / "project_info.json"
# Рантайм-состояние апдейтера (не настройка — правкой руками не
# занимаются), поэтому рядом с backup/, а не в user/configs/.
STATE_FILE = SCRIPT_DIR / "state.json"

# Журнал попыток обновления для человека — с какой версии на какую,
# когда, чем закончилось. Не то же самое, что logs/raw/updater/events.jsonl
# (тот — подробный технический след вперемешку с остальными модулями);
# здесь — только сами попытки apply, по одной строке, коротко и рядом
# с самим апдейтером. Формат .jsonl (не .json), потому что это лог
# на дозапись, а не документ, который целиком перезаписывают — та же
# причина, по которой logs/raw/*/events.jsonl устроены так же.
HISTORY_FILE = SCRIPT_DIR / "history.jsonl"
# Pid'ы wake.py/req.py, поднятых под текущий голосовой диалог — та же
# идея, что state.json у smos.py: если этот процесс убьют настолько
# резко, что try/finally в ask_user() не успеет отработать (SIGKILL,
# закрытие терминала), эти дети останутся сиротами (найдено на практике —
# см. updater_design.md, «Осиротевшие процессы диалога»). Следующий вызов
# ask_user() читает этот файл и гасит всё, что там ещё живо, ПЕРЕД тем как
# поднимать новые wake/req — иначе новая и старая запись накладываются.
DIALOG_PROCS_FILE = SCRIPT_DIR / "dialog_procs.json"

_YES_WORDS = ("да", "давай", "конечно", "обнови", "хорошо", "ок", "окей", "подтверждаю")
# "Отложить" сейчас технически то же самое, что явное "нет" (откладывать
# пока умеем только одним способом — cooldown до следующего запроса,
# см. STATE_FILE/decline_cooldown_hours), но слова держим отдельным
# списком синонимов, а не одним словом "нет" — распознаётся живая речь,
# не команда с фиксированным словарём.
_NO_WORDS = (
    "нет", "отмена", "отменить", "не надо", "не хочу", "стоп", "передумал",
    "позже", "попозже", "потом", "отложи", "отложить", "не сейчас", "не сегодня",
)


def _log(msg: str) -> None:
    """Человекочитаемый прогресс — в stderr, не в stdout (там только
    финальный JSON, см. докстринг модуля)."""
    print(f"[updater] {msg}", file=sys.stderr)


def play_clip(key: str, wait: bool = True) -> None:
    """Проигрывает системный клип sysaudio.py — отдельным процессом, той
    же схемой, что и все остальные компоненты SMOS (никакого in-process
    импорта: был ровно такой вариант, но он и архитектурно выбивался, и
    на практике сразу дал коллизию модулей config/log_client — sysaudio.py
    свои задумал именно как отдельный процесс с CLI, см.
    sysaudio_design.md). wait=True — дождаться конца проигрывания
    (`subprocess.run`); wait=False — не ждать (`subprocess.Popen`).
    Путь к sysaudio.py — из SMOS_SYSAUDIO_SCRIPT (тем же принципом, что
    SMOS_WAKE_SCRIPT/SMOS_REQ_SCRIPT), либо угадывается при ручном
    запуске. Любой сбой (нет скрипта, не запустился) — только в лог, не
    роняет вызывающего."""
    python = os.environ.get("SMOS_PYTHON") or sys.executable
    script = os.environ.get("SMOS_SYSAUDIO_SCRIPT") or str(PROJECT_ROOT / "system" / "sysaudio" / "sysaudio.py")
    if not Path(script).exists():
        _log(f"sysaudio недоступен ({script}) — {key!r} только в лог")
        return
    try:
        if wait:
            subprocess.run(
                [python, script, key], cwd=str(Path(script).parent), timeout=20,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            # НЕ subprocess.DEVNULL по недосмотру, а специально: если
            # вызывающий сам под subprocess.run(capture_output=True)
            # (как updater.py check под smos.py), непере-направленный
            # Popen молча наследует его stdout/stderr — те же пайпы,
            # которых ждёт capture_output. Найдено на практике на
            # apply/_restart_smos (см. ниже) — там это было гораздо
            # серьёзнее (apply живёт долго), но на всякий случай и здесь.
            subprocess.Popen(
                [python, script, key], cwd=str(Path(script).parent),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log(f"не удалось проиграть клип {key!r}: {e}")


def _append_history(event: str, **fields) -> None:
    """Дописывает одну строку в history.jsonl — журнал попыток
    обновления для человека (см. updater_design.md, раздел «Журнал
    истории обновлений»). Не должно ронять apply, даже если диск
    внезапно недоступен — история хуже, чем сорванное обновление."""
    entry = {"ts": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event, **fields}
    try:
        with HISTORY_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        _log(f"не удалось дописать history.jsonl: {e}")


# --------------------------------------------------------------------------
# Версии
# --------------------------------------------------------------------------

def load_local_info() -> dict:
    try:
        data = json.loads(INFO_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "version" in data:
            return data
    except (OSError, json.JSONDecodeError) as e:
        _log(f"не удалось прочитать {INFO_FILE}: {e}")
    send_log("WARNING", "local_project_info_unreadable")
    return {"version": "0.0.0", "notes": ""}


def fetch_remote_info(cfg: dict) -> dict | None:
    gh = cfg["github"]
    if not gh["owner"] or not gh["repo"]:
        _log("github.owner/github.repo не заполнены в user/configs/updater.json — проверка обновлений выключена")
        return None

    url = f"https://raw.githubusercontent.com/{gh['owner']}/{gh['repo']}/{gh['branch']}/project_info.json"
    try:
        with urllib.request.urlopen(url, timeout=cfg["check_timeout_sec"]) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        if isinstance(data, dict) and "version" in data:
            return data
        _log(f"{url} вернул не тот формат, что ожидался")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as e:
        _log(f"не удалось проверить версию на GitHub: {e}")
        send_log("WARNING", "update_check_network_failed", {"error": str(e)})
    return None


def parse_version(s: str) -> tuple[int, int, int]:
    """Простой разбор "major.minor.patch" без сторонних пакетов —
    в духе минимума зависимостей остального проекта. Не-числовые/
    отсутствующие компоненты считаются нулём, не бросает исключений."""
    parts = str(s).strip().split(".")
    nums = []
    for p in (parts + ["0", "0", "0"])[:3]:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    return tuple(nums)


def is_newer(remote: str, local: str) -> bool:
    return parse_version(remote) > parse_version(local)


def _load_decline_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_decline_state(version: str) -> None:
    try:
        STATE_FILE.write_text(
            json.dumps({"declined_version": version, "declined_at": time.time()}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as e:
        _log(f"не удалось сохранить состояние отказа: {e}")


def _clear_decline_state() -> None:
    STATE_FILE.unlink(missing_ok=True)


def _recently_declined(remote_version: str, cooldown_hours: float) -> bool:
    """True, если ИМЕННО ЭТУ версию уже отклоняли (да/нет/"попозже" —
    все ложатся в один и тот же cooldown, см. _NO_WORDS) не дольше
    cooldown_hours назад — не переспрашиваем про неё на каждом запуске
    smos.py. Версия новее той, что отклоняли, cooldown не касается —
    спросит сразу же."""
    state = _load_decline_state()
    if state.get("declined_version") != remote_version:
        return False
    declined_at = state.get("declined_at")
    if not isinstance(declined_at, (int, float)):
        return False
    return (time.time() - declined_at) < cooldown_hours * 3600


# --------------------------------------------------------------------------
# Голосовой диалог
# --------------------------------------------------------------------------

def _script_paths() -> dict[str, str] | None:
    """Пути к wake.py/req.py и интерпретатору — приходят от smos.py через
    окружение (единственный источник правды о раскладке system/, см.
    докстринг модуля). audio.py тут больше не нужен — вопрос и
    объявления теперь клипы system/sysaudio/, не динамическая TTS-фраза
    (см. sysaudio_design.md). Фолбэк на стандартные пути — только для
    ручного запуска updater.py check без smos.py."""
    python = os.environ.get("SMOS_PYTHON") or sys.executable
    wake = os.environ.get("SMOS_WAKE_SCRIPT")
    req = os.environ.get("SMOS_REQ_SCRIPT")

    if not (wake and req):
        wake = wake or str(PROJECT_ROOT / "system" / "fwl" / "rvs" / "wake.py")
        req = req or str(PROJECT_ROOT / "system" / "fwl" / "rvs" / "req.py")
        _log("SMOS_*_SCRIPT не заданы окружением — угадываю стандартные пути "
             "(нормально только при ручном запуске без smos.py)")

    for name, path in (("wake", wake), ("req", req)):
        if not Path(path).exists():
            _log(f"не найден скрипт {name}: {path}")
            return None
    return {"python": python, "wake": wake, "req": req}


def _wait_for_answer(req_script: str, timeout_sec: float, started_at: float) -> str | None:
    """Поллит output/recognized.json (то же самое, что req.py и так
    пишет), ждёт ОДНОЗНАЧНОГО да/нет/отмена — не первого непустого
    текста. wake.py --nowake реагирует на любую громкость, и случайное
    срабатывание от шума (без слов) иногда всё равно даёт от Google STT
    какую-то галлюцинацию на пустом месте — такой текст не парсится как
    да/нет и должен просто отбрасываться, а не досрочно завершать
    ожидание молчаливым "нет". Ждём до общего timeout_sec, реагируя
    только на файлы новее последнего уже рассмотренного (чтобы не
    разбирать один и тот же неоднозначный текст в цикле по кругу).
    Возвращает "yes" / "no", либо None, если за весь timeout_sec явного
    ответа так и не прозвучало."""
    output_file = Path(req_script).parent / "output" / "recognized.json"
    deadline = started_at + timeout_sec
    seen_mtime = started_at
    while time.time() < deadline:
        try:
            mtime = output_file.stat().st_mtime if output_file.exists() else 0.0
            if mtime > seen_mtime:
                seen_mtime = mtime
                data = json.loads(output_file.read_text(encoding="utf-8"))
                text = data.get("text", "")
                if text:
                    decision = _parse_yes_no(text)
                    if decision is not None:
                        _log(f"явный ответ: {text!r} -> {decision}")
                        return decision
                    _log(f"распознано, но неоднозначно ({text!r}) — жду дальше")
        except (OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    return None


def _stop_process(proc: subprocess.Popen) -> None:
    """Гасит один процесс по возрастающей — тот же приём, что и в
    smos.py (SIGINT -> SIGTERM -> SIGKILL)."""
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGINT, 3.0), (signal.SIGTERM, 2.0), (signal.SIGKILL, 1.0)):
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            if proc.poll() is not None:
                return
            time.sleep(0.1)


def _pid_alive(pid: int, cmdline_match: str) -> bool:
    """Тот же приём, что is_alive() в smos.py — не просто os.kill(pid, 0),
    а ещё сверка с /proc/<pid>/cmdline, чтобы не спутать с чужим
    процессом, случайно переиспользовавшим тот же pid после перезагрузки
    системы."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return True  # не Linux / нет /proc — доверяем os.kill
    return cmdline_match in cmdline


def _kill_pid(pid: int, cmdline_match: str) -> None:
    """Гасит один осиротевший pid по возрастающей (SIGINT -> SIGTERM ->
    SIGKILL) — вариант _stop_process для случая, когда есть только "голый"
    pid, а не объект Popen (процесс поднимал не этот, а прошлый,
    неудачно завершившийся вызов ask_user())."""
    if not _pid_alive(pid, cmdline_match):
        return
    for sig, wait in ((signal.SIGINT, 2.0), (signal.SIGTERM, 1.5), (signal.SIGKILL, 1.0)):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        deadline = time.time() + wait
        while time.time() < deadline:
            if not _pid_alive(pid, cmdline_match):
                return
            time.sleep(0.1)


def _cleanup_stale_dialog_procs() -> None:
    """Вызывается в начале КАЖДОГО ask_user(), до того как поднят хоть
    один новый процесс. Если предыдущий вызов был прерван настолько
    резко, что try/finally не отработали (SIGKILL, закрытие терминала,
    в котором жил updater.py) — его wake.py/req.py могли остаться
    висеть сиротами и до сих пор слушать. Без этой проверки новый
    диалог накладывается на старый — ровно то, что обнаружилось на
    практике (см. updater_design.md; тогда ещё поднимался и audio.py —
    сейчас его заменил sysaudio, но осиротеть могут и wake/req)."""
    try:
        entries = json.loads(DIALOG_PROCS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(entries, list):
        return
    for entry in entries:
        pid, match = entry.get("pid"), entry.get("match", "")
        if isinstance(pid, int) and _pid_alive(pid, match):
            _log(f"осиротевший процесс с прошлого диалога ({match}, pid {pid}) — гашу")
            send_log("WARNING", "stale_dialog_process_killed", {"pid": pid, "match": match})
            _kill_pid(pid, match)
    DIALOG_PROCS_FILE.unlink(missing_ok=True)


def _save_dialog_procs(procs: list[tuple[subprocess.Popen, str]]) -> None:
    entries = [{"pid": p.pid, "match": match} for p, match in procs]
    try:
        DIALOG_PROCS_FILE.write_text(json.dumps(entries), encoding="utf-8")
    except OSError as e:
        _log(f"не удалось сохранить dialog_procs.json: {e}")


def _clear_dialog_procs() -> None:
    DIALOG_PROCS_FILE.unlink(missing_ok=True)


def _cleanup_dialog_files(wake_script: str, req_script: str) -> None:
    """Подчищает flags/utterance.wav и output/recognized.json, чтобы не
    осталось хвостов к моменту, когда настоящие wake/req запустятся по
    обычному порядку в smos.py."""
    (Path(wake_script).parent / "flags" / "utterance.wav").unlink(missing_ok=True)
    (Path(req_script).parent / "output" / "recognized.json").unlink(missing_ok=True)


def _contains_word(text: str, phrase: str) -> bool:
    """phrase встречается в text по границам слова, НЕ как произвольная
    подстрока — иначе, например, "да" находится внутри "ерунда"/"надо"/
    "куда"/"когда", а такое совпадение ложное и почти гарантировано на
    живой речи."""
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def _parse_yes_no(text: str) -> str | None:
    """Простое сопоставление ключевых слов по границам слова (см.
    "Открыто" в updater_design.md — можно усложнить позже, но без LLM:
    система обновлений не должна зависеть от сети/модели там, где без
    неё можно обойтись). None — неоднозначный/нераспознанный текст (в
    т.ч. случайная галлюцинация STT на шумовом срабатывании wake.py
    --nowake) — это НЕ то же самое, что явное "нет": вызывающая сторона
    (_wait_for_answer) должна продолжать ждать, а не считать это ответом."""
    lowered = text.lower()
    if any(_contains_word(lowered, w) for w in _NO_WORDS):
        return "no"
    if any(_contains_word(lowered, w) for w in _YES_WORDS):
        return "yes"
    return None


def ask_user(cfg: dict) -> str:
    """Весь голосовой диалог да/нет/отмена. Вопрос — готовый клип
    sysaudio ("update_available"), не динамическая TTS-фраза с
    версиями/notes (см. sysaudio_design.md, «Одно решение, которое надо
    принять» — вариант 1: версии и notes остаются только в логе, на
    слух не нужны). Возвращает:
      "yes"       — пользователь подтвердил;
      "no"        — явный отказ (нет/отмена/...);
      "postponed" — не ответил за отведённое время, либо нужные скрипты
                    недоступны (в обоих случаях явного решения не было —
                    отдельно от "no", чтобы cmd_check() мог выбрать
                    между клипами update_cancelled/update_postponed)."""
    paths = _script_paths()
    if paths is None:
        send_log("ERROR", "dialog_unavailable_missing_scripts")
        return "postponed"

    # Прежде чем поднимать хоть один новый процесс — погасить осиротевшие
    # с прошлого, не до конца завершившегося диалога (см. докстринг
    # DIALOG_PROCS_FILE выше). Иначе новая запись накладывается на старую.
    _cleanup_stale_dialog_procs()

    play_clip("update_available")  # синхронно; вернулось — вопрос прозвучал

    dialog_started_at = time.time()
    # stdout/stderr — DEVNULL, не унаследованные: check запущен под
    # smos.py через subprocess.run(capture_output=True), и непере-
    # направленный Popen молча взял бы те же пайпы — тогда smos.py не
    # увидел бы конец check, пока живы wake/req (см. докстринг play_clip
    # и updater_design.md, «Гонка за порт logs» — тот же класс бага,
    # найден отдельно и позже, на apply/_restart_smos).
    wake_proc = subprocess.Popen(
        [paths["python"], paths["wake"], "--nowake"], cwd=str(Path(paths["wake"]).parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    req_proc = subprocess.Popen(
        [paths["python"], paths["req"]], cwd=str(Path(paths["req"]).parent),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _save_dialog_procs([(wake_proc, "wake.py"), (req_proc, "req.py")])
    try:
        decision = _wait_for_answer(paths["req"], cfg["dialog_timeout_sec"], dialog_started_at)
    finally:
        _stop_process(wake_proc)
        _stop_process(req_proc)
        _cleanup_dialog_files(paths["wake"], paths["req"])
        _clear_dialog_procs()

    if decision is None:
        _log(f"явного ответа не дождался за {cfg['dialog_timeout_sec']}с")
        send_log("INFO", "dialog_no_answer")
        return "postponed"

    send_log("INFO", "dialog_answer_received", {"decision": decision})
    return decision


# --------------------------------------------------------------------------
# apply: скачивание, проверка версии, бэкап, подмена system/ + smos.py
# --------------------------------------------------------------------------
#
# smos.py обновляется вместе с system/ (не только она) — если меняется
# состав/раскладка system/, с большой вероятностью меняется и таблица
# PROCESSES в smos.py, которая про эту раскладку знает. updater.py и
# структура папок верхнего уровня по-прежнему НЕ обновляются (см.
# updater_design.md, «Зачем updater/ вне system/» и «Вне рамок v1») —
# апдейтер не может безопасно заменить самого себя, пока выполняется.

def _rmtree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _create_backup(system_dir: Path, smos_py: Path, dest_zip: Path) -> None:
    """Зипует system/ и smos.py вместе, атомарно (temp + rename). Пути
    внутри архива — относительно PROJECT_ROOT ("system/..." и
    "smos.py"), чтобы восстановление сводилось к распаковке архива прямо
    в корень проекта."""
    tmp = dest_zip.with_suffix(dest_zip.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in system_dir.rglob("*"):
            if file.is_file():
                zf.write(file, file.relative_to(PROJECT_ROOT))
        if smos_py.is_file():
            zf.write(smos_py, smos_py.relative_to(PROJECT_ROOT))
    tmp.replace(dest_zip)


def _restore_backup(backup_zip: Path) -> bool:
    """Последний рубеж отката — распаковать зип-бэкап прямо поверх
    корня проекта, восстанавливая и system/, и smos.py разом.
    Используется, только если восстановление из *_old_tmp (первая линия
    отката при сбое подмены) не сработало — на практике не должно
    случаться, но лучше явный откат, чем молчаливая дыра."""
    if not backup_zip.exists():
        return False
    try:
        with zipfile.ZipFile(backup_zip) as zf:
            zf.extractall(PROJECT_ROOT)
        return True
    except (OSError, zipfile.BadZipFile):
        return False


def _download(url: str, dest: Path, timeout: float) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "SMOS-updater"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        dest.write_bytes(resp.read())


def _extract_repo_zip(zip_path: Path, extract_to: Path) -> Path:
    """Распаковывает архив ветки в extract_to, возвращает путь к
    корневой папке репозитория внутри — GitHub кладёт содержимое в
    подпапку вида <repo>-<branch>/, а не прямо в корень архива."""
    extract_to.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_to)
    entries = [p for p in extract_to.iterdir() if p.is_dir()]
    if len(entries) != 1:
        raise FileNotFoundError(f"неожиданное содержимое архива: {[p.name for p in extract_to.iterdir()]}")
    return entries[0]


def _restart_smos() -> subprocess.Popen | None:
    """Запускает smos.py с --no-update, возвращает Popen (или None при
    сбое запуска). apply зовёт это при любом исходе — и при успехе, и
    при откате после сбоя (система должна снова работать в любом
    случае, см. updater_design.md); там, где Popen дальше не нужен (все
    пути кроме успешной подмены), вызывающий код просто игнорирует
    возврат.

    --no-update ОБЯЗАТЕЛЕН здесь: без него перезапущенный smos.py сам
    тут же снова идёт в свою собственную проверку обновления (ведь
    project_info.json ещё не обновлён на этот момент — см. шаг 7 в
    updater_design.md, версия пишется только ПОСЛЕ health-check) —
    поднимает свой logs, снова спрашивает голосом, и здоровье
    настоящего apply не успевает проверить логи вовремя (обнаружено на
    практике: health-check стабильно проваливался с
    health_check_no_started_log, потому что restart тратил всё окно на
    вложенный повторный диалог вместо start_merged()).

    stdout/stderr перезапущенного smos.py — В ФАЙЛ (restart_output.log,
    перезаписывается на каждый перезапуск), не унаследованные и не
    DEVNULL. Не унаследованные — потому что apply сам может быть
    потомком check, который под smos.py идёт через
    subprocess.run(capture_output=True); непере-направленный Popen
    молча взял бы те же пайпы, и smos.py навсегда завис бы на 180с,
    ожидая закрытия пайпа, который держит открытым живой,
    перезапущенный smos.py (найдено на практике: apply успешно
    применял обновление и новая система реально работала, а
    ОРИГИНАЛЬНЫЙ smos.py всё ещё ждал ответа check и не мог перейти
    дальше — по истечении таймаута решал, что обновления не было, и
    поднимал ВТОРОЙ полный комплект процессов поверх уже работающего,
    отсюда ошибки ALSA "unable to open slave" — два wake.py на один
    микрофон). В файл, не в DEVNULL — если новая версия рухнет ДО того,
    как успеет хоть что-то залогировать (как в случае с argparse и
    несовместимым --no-update, который тоже был найден на практике) —
    трейсбек будет виден в файле, а не потерян."""
    log_path = SCRIPT_DIR / "restart_output.log"
    try:
        log_file = open(log_path, "wb")
    except OSError as e:
        _log(f"не удалось открыть {log_path}: {e} — вывод перезапуска будет потерян")
        log_file = subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "smos.py"), "start", "--no-update"], cwd=str(PROJECT_ROOT),
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},  # иначе вывод в файл копится блоками, не по строкам
        )
        send_log("INFO", "smos_restarted")
        return proc
    except OSError as e:
        _log(f"не удалось перезапустить smos.py: {e}")
        send_log("CRITICAL", "smos_restart_failed", {"error": str(e)})
        return None
    finally:
        if log_file not in (None, subprocess.DEVNULL):
            log_file.close()  # Popen уже продублировал дескриптор — закрывать здесь безопасно


# --------------------------------------------------------------------------
# Здоровье после обновления: слежение за логами (см. updater_design.md,
# раздел «Здоровье после обновления»). Идея автора: система логов сама
# обновляется исключительно редко, поэтому формат logs/raw/*/events.jsonl
# — надёжный, не завязанный на конкретную версию system/ индикатор того,
# поднялось ли всё после подмены.
# --------------------------------------------------------------------------

def _log_files() -> list[Path]:
    logs_dir = PROJECT_ROOT / "logs" / "raw"
    if not logs_dir.is_dir():
        return []
    return sorted(logs_dir.glob("*/events.jsonl"))


def _snapshot_log_offsets() -> dict[Path, int]:
    """Текущий размер каждого logs/raw/<module>/events.jsonl — точка
    отсчёта, откуда потом искать НОВЫЕ записи (после перезапуска)."""
    offsets = {}
    for f in _log_files():
        try:
            offsets[f] = f.stat().st_size
        except OSError:
            offsets[f] = 0
    return offsets


def _read_new_log_entries(offsets: dict[Path, int]) -> list[dict]:
    """Читает то, что дописалось в logs/raw/*/events.jsonl с прошлого
    вызова, и СРАЗУ продвигает offsets на новые размеры (чтобы повторный
    вызов не возвращал уже виденное). Новые файлы модулей (появились уже
    после снимка) читаются с начала."""
    entries = []
    for f in _log_files():
        try:
            size = f.stat().st_size
        except OSError:
            continue
        start = offsets.get(f, 0)
        if size <= start:
            offsets[f] = size
            continue
        try:
            with f.open("r", encoding="utf-8") as fh:
                fh.seek(start)
                chunk = fh.read(size - start)
        except OSError:
            continue
        offsets[f] = size
        for line in chunk.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _health_check(smos_proc: subprocess.Popen, cfg: dict) -> bool:
    """Следит за логами (и за самим процессом smos.py) после перезапуска
    на новой версии. "Здорово": в логах появилось smos/all_system_started
    и НИ ОДНОЙ новой ошибки (ERROR/CRITICAL, в любом модуле) за
    health_check_timeout_sec. "Плохо", с немедленным прекращением
    ожидания: сам smos.py неожиданно завершился, или нашлась хоть одна
    новая ошибка. Не появилось ни того, ни другого за весь таймаут —
    тоже "плохо" (система, похоже, не поднялась до конца).

    Не блокирует пользователя — smos.py к этому моменту уже реально
    запущен и работает, это просто фоновое наблюдение apply."""
    offsets = _snapshot_log_offsets()
    deadline = time.time() + cfg["health_check_timeout_sec"]
    started_seen = False

    while time.time() < deadline:
        if smos_proc.poll() is not None:
            _log(f"smos.py неожиданно завершился (код {smos_proc.returncode}) во время проверки здоровья")
            send_log("ERROR", "health_check_smos_exited", {"returncode": smos_proc.returncode})
            return False

        for entry in _read_new_log_entries(offsets):
            if entry.get("module") == "smos" and entry.get("message") == "all_system_started":
                started_seen = True
            if entry.get("level") in ("ERROR", "CRITICAL"):
                _log(f"новая ошибка после обновления: {entry.get('module')}/{entry.get('message')}")
                send_log("WARNING", "health_check_new_error", {"source": entry})
                return False

        time.sleep(0.5)

    if not started_seen:
        _log("не дождался лога all_system_started — считаю обновление неудачным")
        send_log("WARNING", "health_check_no_started_log")
    return started_seen


def _rollback_to_old(
    system_dir: Path, smos_py: Path, old_system_tmp: Path, old_smos_tmp: Path, backup_zip: Path,
) -> bool:
    """Возвращает на место старые system/ и smos.py, отложенные в
    *_old_tmp во время подмены (используется, когда новая версия уже
    встала на место, но провалила health-check). Если *_old_tmp почему-то
    нет — последний рубеж: backup.zip целиком поверх корня проекта."""
    try:
        if old_system_tmp.is_dir():
            _rmtree(system_dir)
            old_system_tmp.rename(system_dir)
            old_system_ok = True
        else:
            old_system_ok = False

        if old_smos_tmp.is_file():
            smos_py.unlink(missing_ok=True)
            old_smos_tmp.rename(smos_py)
        # если smos_old_tmp нет — архив не содержал smos.py, его и не
        # трогали при подмене, откатывать нечего, это не сбой отката.
    except OSError:
        old_system_ok = False

    if old_system_ok:
        _log("откат: старые system/ и smos.py возвращены из *_old_tmp")
        send_log("WARNING", "rolled_back_from_tmp")
        return True
    if _restore_backup(backup_zip):
        _log("откат: восстановлено из backup")
        send_log("WARNING", "rolled_back_from_backup")
        return True
    _log("ОТКАТ НЕ УДАЛСЯ — система может быть в неполном состоянии, нужно вмешательство вручную")
    send_log("CRITICAL", "rollback_failed")
    return False


# --------------------------------------------------------------------------
# check / apply
# --------------------------------------------------------------------------

def spawn_apply() -> bool:
    """Запускает `updater.py apply` отсоединённым процессом
    (start_new_session — переживает завершение и этого check-вызова, и
    самого smos.py, см. updater_design.md).

    stdout/stderr — В ФАЙЛ (apply_output.log, перезаписывается на каждый
    запуск), не унаследованные от check. КРИТИЧНО, не просто гигиена:
    check запущен из smos.py через subprocess.run(capture_output=True),
    и apply живёт долго (скачивание, health-check, а после успеха — вся
    жизнь перезапущенного smos.py и его 8 детей, у которых те же
    unnaследованные пайпы транзитивно). Непере-направленный Popen молча
    взял бы эти пайпы, они никогда не закрылись бы сами — и
    subprocess.run в smos.py висел бы до жёсткого таймаута 180с, даже
    когда apply давно всё сделал и новая система уже реально работает
    (найдено на практике: именно так и произошло — по истечении
    таймаута smos.py решал, что обновления не было, и поднимал ВТОРОЙ
    полный комплект процессов поверх уже работающего)."""
    log_path = SCRIPT_DIR / "apply_output.log"
    try:
        log_file = open(log_path, "wb")
    except OSError as e:
        _log(f"не удалось открыть {log_path}: {e} — вывод apply будет потерян")
        log_file = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "updater.py"), "apply"],
            cwd=str(SCRIPT_DIR),
            start_new_session=True,
            stdout=log_file, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},  # иначе вывод в файл копится блоками, не по строкам
        )
        return True
    except OSError as e:
        _log(f"не удалось запустить apply: {e}")
        return False
    finally:
        if log_file not in (None, subprocess.DEVNULL):
            log_file.close()  # Popen уже продублировал дескриптор — закрывать здесь безопасно


def cmd_check() -> dict:
    if not CFG["enabled"]:
        return {"result": "no_update"}

    local_info = load_local_info()
    remote_info = fetch_remote_info(CFG)
    if remote_info is None:
        return {"result": "no_update"}

    local_version = local_info.get("version", "0.0.0")
    remote_version = remote_info.get("version", "0.0.0")
    if not is_newer(remote_version, local_version):
        return {"result": "no_update"}

    if _recently_declined(remote_version, CFG["decline_cooldown_hours"]):
        _log(f"{remote_version} уже отклоняли недавно — не переспрашиваю (см. updater/state.json)")
        return {"result": "no_update"}

    _log(f"доступно обновление: {local_version} -> {remote_version}")
    send_log("INFO", "update_available", {"local": local_version, "remote": remote_version})

    answer = ask_user(CFG)

    if answer == "yes":
        send_log("INFO", "update_confirmed")
        _clear_decline_state()
        if not spawn_apply():
            send_log("ERROR", "apply_spawn_failed")
            return {"result": "declined"}
        return {"result": "updating"}

    # "no" (явный отказ) и "postponed" (не ответил / диалог недоступен) —
    # разные клипы (update_cancelled / update_postponed), но одинаковое
    # дальнейшее поведение: не обновляемся, включаем cooldown на эту версию.
    play_clip("update_cancelled" if answer == "no" else "update_postponed", wait=False)
    send_log("INFO", "update_declined", {"reason": answer})
    _save_decline_state(remote_version)
    return {"result": "declined"}


def cmd_apply() -> dict:
    """Скачивание zip -> проверка версии -> бэкап -> подмена system/ +
    smos.py -> перезапуск + health-check по логам -> обновление
    project_info.json (только если health-check прошёл). См.
    updater_design.md, разделы «Внутри updater.py apply» и «Здоровье
    после обновления».

    Порядок специально такой: пока версия не сверена, локально ничего
    не тронуто — ни бэкап не создаётся, ни тем более подмена не
    начинается. Версия сверяется здесь ЗАНОВО (не полагаясь на то, что
    её уже проверил cmd_check) — на случай ручного запуска `apply`
    в обход `check`, или если репозиторий на GitHub откатился на более
    старую версию между check и apply: apply не должен позволить
    откатить рабочую систему на версию старее или равную текущей.

    После подмены новая версия не считается принятой автоматически —
    apply перезапускает smos.py и какое-то время следит за логами
    (см. _health_check); не поднялось, или появилась новая ошибка —
    откатывает обратно на старую версию и перезапускает уже её.

    Запущен отсоединённым процессом (см. spawn_apply) — ничей больше
    ребёнок, переживает и check-вызов, и сам smos.py. ЗАКАНЧИВАЕТСЯ
    ВСЕГДА перезапуском smos.py, при любом исходе, включая полный
    провал с откатом — система должна снова заработать, а не остаться
    выключенной посреди обновления."""
    send_log("INFO", "apply_started")
    _log("apply: начинаю обновление")
    play_clip("update_applying", wait=False)
    current_version = load_local_info().get("version", "0.0.0")

    gh = CFG["github"]
    if not gh["owner"] or not gh["repo"]:
        send_log("ERROR", "apply_no_github_config")
        _append_history("no_github_config", **{"from": current_version})
        _restart_smos()
        return {"result": "error", "error": "no_github_config"}

    system_dir = PROJECT_ROOT / "system"
    smos_py = PROJECT_ROOT / "smos.py"
    backup_dir = SCRIPT_DIR / "backup"
    backup_dir.mkdir(exist_ok=True)
    backup_zip = backup_dir / "backup.zip"
    tmp_zip = SCRIPT_DIR / "tmp_download.zip"
    tmp_extract = SCRIPT_DIR / "tmp_extract"

    # 1. Скачивание — во временный файл, рабочие файлы ещё не тронуты.
    download_url = f"https://github.com/{gh['owner']}/{gh['repo']}/archive/refs/heads/{gh['branch']}.zip"
    try:
        _download(download_url, tmp_zip, CFG["download_timeout_sec"])
        _log("проект скачан")
    except Exception as e:  # noqa: BLE001 — сеть/HTTP/диск, любой сбой -> просто не обновляемся сейчас
        _log(f"скачивание не удалось: {e}")
        send_log("ERROR", "download_failed", {"error": str(e)})
        _append_history("download_failed", **{"from": current_version})
        tmp_zip.unlink(missing_ok=True)
        _restart_smos()
        return {"result": "error", "error": "download_failed"}

    # 2. Распаковка во временную папку — рабочие файлы всё ещё не тронуты.
    try:
        repo_root = _extract_repo_zip(tmp_zip, tmp_extract)
        new_system_dir = repo_root / "system"
        new_smos_py = repo_root / "smos.py"
        new_info_file = repo_root / "project_info.json"
        if not new_system_dir.is_dir():
            raise FileNotFoundError("в скачанном архиве нет папки system/")
        _log("архив распакован")
    except Exception as e:  # noqa: BLE001 — битый zip/неожиданная структура архива
        _log(f"распаковка не удалась: {e}")
        send_log("ERROR", "extract_failed", {"error": str(e)})
        _append_history("extract_failed", **{"from": current_version})
        tmp_zip.unlink(missing_ok=True)
        _rmtree(tmp_extract)
        _restart_smos()
        return {"result": "error", "error": "extract_failed"}

    # 3. Версия — заново, сейчас, а не то, что видел cmd_check (см. докстринг).
    downloaded_version = "0.0.0"
    if new_info_file.exists():
        try:
            data = json.loads(new_info_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "version" in data:
                downloaded_version = data["version"]
        except (OSError, json.JSONDecodeError):
            pass
    if not is_newer(downloaded_version, current_version):
        _log(f"скачанная версия ({downloaded_version}) не новее текущей ({current_version}) — не обновляюсь")
        send_log("WARNING", "downgrade_prevented", {"current": current_version, "downloaded": downloaded_version})
        _append_history("downgrade_prevented", **{"from": current_version, "to": downloaded_version})
        tmp_zip.unlink(missing_ok=True)
        _rmtree(tmp_extract)
        _restart_smos()
        return {"result": "downgrade_prevented"}

    # 4. Бэкап — только теперь, когда точно решили обновляться.
    try:
        _create_backup(system_dir, smos_py, backup_zip)
        _log(f"бэкап сохранён: {backup_zip}")
        send_log("INFO", "backup_created")
    except OSError as e:
        _log(f"бэкап не удался: {e}")
        send_log("ERROR", "backup_failed", {"error": str(e)})
        _append_history("backup_failed", **{"from": current_version, "to": downloaded_version})
        tmp_zip.unlink(missing_ok=True)
        _rmtree(tmp_extract)
        _restart_smos()
        return {"result": "error", "error": "backup_failed"}

    # 5. Подмена — system/ и smos.py вместе. *_old_tmp НЕ удаляется сразу
    # (в отличие от предыдущей версии этого шага) — она нужна ещё живой
    # до конца health-check (шаг 6), на случай отката.
    old_system_tmp = PROJECT_ROOT / "system_old_tmp"
    old_smos_tmp = PROJECT_ROOT / "smos_old_tmp.py"
    try:
        _rmtree(old_system_tmp)
        system_dir.rename(old_system_tmp)
        new_system_dir.rename(system_dir)

        if new_smos_py.is_file():
            old_smos_tmp.unlink(missing_ok=True)
            smos_py.rename(old_smos_tmp)
            new_smos_py.rename(smos_py)

        _log("system/ и smos.py заменены, старые копии оставлены на время проверки здоровья")
    except OSError as e:
        _log(f"подмена не удалась: {e}")
        send_log("ERROR", "swap_failed", {"error": str(e)})

        restored_from_tmp = True
        if not system_dir.is_dir():
            if old_system_tmp.exists():
                old_system_tmp.rename(system_dir)
            else:
                restored_from_tmp = False
        if not smos_py.exists():
            if old_smos_tmp.exists():
                old_smos_tmp.rename(smos_py)
            else:
                restored_from_tmp = False

        if restored_from_tmp:
            _log("откат: system/ и smos.py восстановлены из *_old_tmp")
            send_log("WARNING", "rolled_back_from_tmp")
        elif _restore_backup(backup_zip):
            _log("откат: восстановлено из backup")
            send_log("WARNING", "rolled_back_from_backup")
        else:
            _log("ОТКАТ НЕ УДАЛСЯ — система может быть в неполном состоянии, нужно вмешательство вручную")
            send_log("CRITICAL", "rollback_failed")

        _append_history("swap_failed", **{"from": current_version, "to": downloaded_version})
        tmp_zip.unlink(missing_ok=True)
        _rmtree(tmp_extract)
        _restart_smos()
        return {"result": "error", "error": "swap_failed"}

    # 6. Перезапуск + проверка здоровья по логам. Новая версия уже стоит
    # на месте физически — если она не поднимется как надо, откатываем
    # её тем же способом, каким подменяли, и запускаем старую заново.
    smos_proc = _restart_smos()
    healthy = smos_proc is not None and _health_check(smos_proc, CFG)

    if not healthy:
        if smos_proc is not None:
            _stop_process(smos_proc)
        # _restart_smos() ниже перезапишет restart_output.log ПОВЕРХ
        # этого же файла (второй перезапуск в рамках одного apply — уже
        # на старой версии) — если не забрать содержимое сейчас, трейс-
        # бек краша (если он был) будет потерян прямо в момент, когда
        # он больше всего нужен (найдено на практике при тестировании
        # этого же фикса).
        crash_output = ""
        try:
            crash_output = (SCRIPT_DIR / "restart_output.log").read_text(encoding="utf-8", errors="replace")[-2000:]
        except OSError:
            pass
        _rollback_to_old(system_dir, smos_py, old_system_tmp, old_smos_tmp, backup_zip)
        tmp_zip.unlink(missing_ok=True)
        _rmtree(tmp_extract)
        _log("обновление отклонено проверкой здоровья, возвращаюсь на старую версию")
        send_log("WARNING", "update_rolled_back_after_health_check", {"crash_output": crash_output})
        _append_history("rolled_back", **{"from": current_version, "to": downloaded_version})
        _restart_smos()
        return {"result": "error", "error": "health_check_failed"}

    _rmtree(old_system_tmp)
    old_smos_tmp.unlink(missing_ok=True)

    # 7. Версия — только теперь, когда и подмена, и health-check прошли.
    try:
        (PROJECT_ROOT / "project_info.json").write_text(
            new_info_file.read_text(encoding="utf-8"), encoding="utf-8",
        )
    except OSError as e:
        _log(f"не удалось обновить project_info.json: {e} (не критично, файлы уже обновлены)")
        send_log("WARNING", "project_info_update_failed", {"error": str(e)})

    tmp_zip.unlink(missing_ok=True)
    _rmtree(tmp_extract)

    _log(f"обновление завершено успешно: {current_version} -> {downloaded_version}")
    send_log("INFO", "update_applied", {"from": current_version, "to": downloaded_version})
    _append_history("applied", **{"from": current_version, "to": downloaded_version})
    return {"result": "applied"}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "apply"):
        sys.exit("Использование: updater.py check | apply")

    result = cmd_check() if sys.argv[1] == "check" else cmd_apply()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
