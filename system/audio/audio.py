"""
audio.py — аудио-демон SMOS, v1: «сказать текст».

Место в системе (см. audio_design.md, ../outputstructurizer/outputstructurizer_design.md):

    outputstructurizer  ──► system/audio/tasks/   (заявка <task_id>.json)
                                 │  этот демон: text -> синтез речи
                                 ▼
                             произнесено

Это НЕ полный аудио-демон из audio_design.md — здесь нет приоритетной
очереди, приглушения музыки, будильника поверх всего. Только: взять
заявку, произнести её text, удалить заявку. Поля privilege_level и kind
в заявке уже есть (их проставляет outputstructurizer), но v1 их не
использует — они на будущее, под приоритетную очередь.

Синтез (см. config.py):
- engine "gtts"    — Google TTS: gtts синтезирует MP3 (через интернет),
                     внешний плеер (tts.gtts.player) его проигрывает.
- engine "spd-say" — speech-dispatcher, локально/оффлайн.
Первичный движок не сработал (нет пакета / сети / плеера / команды) →
пробуется tts.fallback_engine → печать текста. Сбой озвучки никогда не
роняет демон и не стопорит очередь.

Что делает демон (форма — как core.py / outputstructurizer.py):
- Поллит tasks/. На каждый *.json (пишется атомарно, temp+rename):
    разбирает (обязательно непустое строковое поле text), произносит,
    удаляет заявку.
- Неразобранная заявка -> tasks/rejected/ с предупреждением.
- На старте очередь НЕ чистит (в отличие от outputstructurizer): фильтр
  «протухших» ответов стоит выше, сюда попадает уже одобренное.

Глушение микрофона на время воспроизведения (mute.flag, 2026-09-06):
- Перед тем как произнести фразу, демон кладёт файл mute.flag в папку
  системы распознавания (mute.flag_path, по умолчанию
  system/fwl/rvs/flags/mute.flag), а после — убирает. Пока файл есть,
  wake.py перестаёт слушать микрофон — иначе на высокой громкости
  система слышит собственную озвучку, это уходит в распознавание и
  зацикливается (ответ -> услышали -> команда -> снова ответ).
- Пишется в режиме "until": в файле лежит момент, до которого глушить
  (сейчас + предельная длительность фразы + запас). Даже если демон
  упадёт, не сняв флаг, микрофон оглохнет ненадолго и сам отпустит —
  чинить вручную не надо. Контракт файла — см. wake.py, mic_muted.
- Ставит его только при реальной попытке синтеза (tts.enabled=true).
  Пока писатель один; когда появится централизованная озвучка, флаг
  станет её заботой.

Запуск:
    python audio.py

Установка для движка по умолчанию (gtts):
    pip install gtts
    # + MP3-плеер: gst-play-1.0 (пакет gstreamer1.0-tools) или mpg123/ffplay

Проверка вручную:
    echo '{"task_id":"t","text":"проверка связи","status":"done"}' > system/audio/tasks/t.json
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
from log_client import send_log  # noqa: E402

CFG = config.load(SCRIPT_DIR)

TASKS_DIR = SCRIPT_DIR / CFG["paths"]["tasks_dir"]
REJECTED_DIR = TASKS_DIR / CFG["paths"]["rejected_subdir"]

CHECK_INTERVAL_SEC = CFG["check_interval_sec"]
TTS = CFG["tts"]
TTS_ENABLED = TTS["enabled"]
TTS_ENGINE = TTS["engine"]
TTS_FALLBACK_ENGINE = TTS.get("fallback_engine", "")

# Флаг глушения микрофона на время воспроизведения (см. докстринг модуля
# и wake.py -> mic_muted). Путь — относительно корня проекта; если корень
# не найден, глушение просто не ставится (демон из-за этого не падает).
MUTE = CFG["mute"]
_PROJECT_ROOT = config.project_root(SCRIPT_DIR)
MUTE_FLAG_FILE = (_PROJECT_ROOT / MUTE["flag_path"]) if _PROJECT_ROOT else None


# --------------------------------------------------------------------------
# Движки синтеза. Каждый либо произносит text, либо бросает исключение —
# решение, что делать при сбое (пробовать запасной, печатать), принимает
# speak() ниже.
# --------------------------------------------------------------------------

def _speak_gtts(text: str) -> None:
    """Google TTS: gtts синтезирует MP3 (сетевой вызов), внешний плеер
    его проигрывает. Временный файл удаляется в любом случае."""
    from gtts import gTTS  # локальный импорт: без пакета работает spd-say/печать

    cfg = TTS["gtts"]
    player = cfg["player"]
    if not player or shutil.which(player[0]) is None:
        raise RuntimeError(f"MP3-плеер {player[0] if player else '(пусто)'!r} не найден")

    tts = gTTS(text=text, lang=cfg["lang"], tld=cfg["tld"], slow=cfg["slow"])
    fd, path = tempfile.mkstemp(prefix="smos_tts_", suffix=".mp3")
    os.close(fd)
    try:
        tts.save(path)  # тут и происходит обращение к Google
        result = subprocess.run(
            [*player, path],
            capture_output=True, text=True, timeout=cfg["timeout_sec"],
        )
        if result.returncode != 0:
            raise RuntimeError(f"плеер вернул код {result.returncode}: {result.stderr.strip()[:200]}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    print(f"[audio] сказал (gtts): {text}")


def _speak_spd_say(text: str) -> None:
    """speech-dispatcher: локально, оффлайн, без зависимостей питона."""
    cfg = TTS["spd_say"]
    command = cfg["command"]
    if not command or shutil.which(command[0]) is None:
        raise RuntimeError(f"команда {command[0] if command else '(пусто)'!r} не найдена")

    result = subprocess.run(
        [*command, text],
        capture_output=True, text=True, timeout=cfg["timeout_sec"],
    )
    if result.returncode != 0:
        raise RuntimeError(f"вернул код {result.returncode}: {result.stderr.strip()[:200]}")
    print(f"[audio] сказал (spd-say): {text}")


_ENGINES = {
    "gtts": _speak_gtts,
    "spd-say": _speak_spd_say,
}


def speak(text: str) -> None:
    """Произносит text первичным движком; не вышло — запасным; не вышло —
    печатает. Ни один сбой синтеза не должен стопорить очередь и ронять
    демон."""
    if not TTS_ENABLED:
        print(f"[audio] (tts выкл) {text}")
        return

    chain = [TTS_ENGINE]
    if TTS_FALLBACK_ENGINE and TTS_FALLBACK_ENGINE != TTS_ENGINE:
        chain.append(TTS_FALLBACK_ENGINE)

    for i, engine in enumerate(chain):
        fn = _ENGINES.get(engine)
        if fn is None:
            print(f"[audio] неизвестный движок {engine!r} в конфиге — пропускаю")
            send_log("WARNING", "tts_unknown_engine", {"engine": engine})
            continue
        try:
            fn(text)
            if i > 0:
                send_log("INFO", "tts_fallback_used", {"engine": engine, "primary": TTS_ENGINE})
            return
        except subprocess.TimeoutExpired:
            role = "первичный" if i == 0 else "запасной"
            print(f"[audio] ({role} движок {engine}: таймаут) {text}")
            send_log("WARNING", "tts_engine_failed", {"engine": engine, "error": "timeout"})
        except Exception as e:  # noqa: BLE001 — любой сбой движка роняем на следующий/печать
            role = "первичный" if i == 0 else "запасной"
            print(f"[audio] ({role} движок {engine} не сработал: {e}) {text}")
            send_log("WARNING", "tts_engine_failed", {"engine": engine, "error": str(e)})

    print(f"[audio] (озвучка недоступна) {text}")


# --------------------------------------------------------------------------
# Глушение микрофона на время воспроизведения
# --------------------------------------------------------------------------

def _playback_cap_sec() -> float:
    """Верхняя граница длительности одной фразы, сек: предельный timeout
    любого из движков синтеза (мог сработать запасной) плюс запас. До
    "сейчас + это" ставится момент снятия глушения в mute.flag."""
    timeouts = [TTS["gtts"]["timeout_sec"], TTS["spd_say"]["timeout_sec"]]
    return max(timeouts) + MUTE["expiry_margin_sec"]


def _set_mute_flag(task_id) -> None:
    """Кладёт mute.flag (режим "until"): пока файл существует и момент в
    нём не прошёл, wake.py не слушает микрофон. Пишем атомарно
    (temp+rename), как всё в SMOS, — чтобы wake не поймал файл на
    середине записи. Любой сбой — только в лог, воспроизведение важнее."""
    if MUTE_FLAG_FILE is None:
        return
    payload = {"until": time.time() + _playback_cap_sec(), "by": "audio", "task_id": task_id}
    try:
        MUTE_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = MUTE_FLAG_FILE.with_name(MUTE_FLAG_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(MUTE_FLAG_FILE)
    except OSError as e:
        send_log("WARNING", "mute_flag_set_failed", {"error": str(e)})


def _clear_mute_flag() -> None:
    """Снимает mute.flag после воспроизведения. Нет файла — ничего
    страшного (мог протухнуть по "until" сам). Сбой — только в лог."""
    if MUTE_FLAG_FILE is None:
        return
    try:
        MUTE_FLAG_FILE.unlink(missing_ok=True)
    except OSError as e:
        send_log("WARNING", "mute_flag_clear_failed", {"error": str(e)})


# --------------------------------------------------------------------------
# Очередь заявок
# --------------------------------------------------------------------------

def _reject(f: Path, reason: str) -> None:
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    dest = REJECTED_DIR / f"{f.stem}_{uuid.uuid4().hex[:8]}.json"
    try:
        f.replace(dest)
    except OSError:
        pass
    send_log("WARNING", "audio_task_rejected", {"file": f.name, "reason": reason})
    print(f"[audio] отклонил {f.name}: {reason}")


def task_files() -> list[Path]:
    if not TASKS_DIR.exists():
        return []
    return sorted(p for p in TASKS_DIR.glob("*.json") if p.is_file())


def process_file(f: Path) -> None:
    try:
        task = json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        _reject(f, f"битый JSON ({e})")
        return

    if not isinstance(task, dict):
        _reject(f, "не JSON-объект")
        return

    text = task.get("text")
    if not (isinstance(text, str) and text.strip()):
        _reject(f, "нет непустого поля text")
        return

    # На время произнесения глушим микрофон (см. докстринг модуля):
    # ставим только при реальной попытке синтеза, снимаем всегда, даже
    # если speak() бросит — иначе микрофон останется глухим до "until".
    mute_here = TTS_ENABLED and MUTE_FLAG_FILE is not None
    if mute_here:
        _set_mute_flag(task.get("task_id"))
    try:
        speak(text.strip())
    finally:
        if mute_here:
            _clear_mute_flag()

    f.unlink(missing_ok=True)
    send_log("INFO", "spoken", {
        "task_id": task.get("task_id"),
        "goal": task.get("goal"),
        "chars": len(text),
        "engine": TTS_ENGINE if TTS_ENABLED else "off",
        "privilege_level": task.get("privilege_level"),
    })


def _engine_ready(engine: str) -> str:
    """Строка-диагностика для стартового вывода: готов ли движок прямо
    сейчас (пакет + внешняя команда). Ничего не синтезирует."""
    if engine == "gtts":
        try:
            import gtts  # noqa: F401
        except ImportError:
            return "нет пакета gtts (pip install gtts)"
        player = TTS["gtts"]["player"]
        if not player or shutil.which(player[0]) is None:
            return f"нет плеера {player[0] if player else '(пусто)'}"
        return "готов"
    if engine == "spd-say":
        command = TTS["spd_say"]["command"]
        if not command or shutil.which(command[0]) is None:
            return f"нет команды {command[0] if command else '(пусто)'}"
        return "готов"
    return "неизвестный движок"


def main() -> None:
    TASKS_DIR.mkdir(parents=True, exist_ok=True)

    send_log("INFO", "audio_started", {
        "tts_enabled": TTS_ENABLED, "engine": TTS_ENGINE, "fallback_engine": TTS_FALLBACK_ENGINE,
        "mute_flag": str(MUTE_FLAG_FILE) if MUTE_FLAG_FILE else None,
    })
    print(f"[audio] запущен. Очередь заявок на озвучку: {TASKS_DIR}")
    if MUTE_FLAG_FILE is not None:
        print(f"[audio] на время озвучки глушу микрофон через: {MUTE_FLAG_FILE}")
    else:
        print("[audio] корень проекта не найден — глушение микрофона на время озвучки ОТКЛЮЧЕНО")
    if TTS_ENABLED:
        print(f"[audio] движок: {TTS_ENGINE} ({_engine_ready(TTS_ENGINE)})")
        if TTS_FALLBACK_ENGINE and TTS_FALLBACK_ENGINE != TTS_ENGINE:
            print(f"[audio] запасной: {TTS_FALLBACK_ENGINE} ({_engine_ready(TTS_FALLBACK_ENGINE)})")

    while True:
        for f in task_files():
            process_file(f)
        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        send_log("INFO", "audio_stopped")
        print("\n[audio] остановлен")
