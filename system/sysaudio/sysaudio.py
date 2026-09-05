#!/usr/bin/env python3
"""
sysaudio.py — «голос системы» SMOS: проигрывание заранее собранных клипов
системных событий.

Место в системе (см. sysaudio_design.md): это НЕ демон и НЕ звено
пайплайна. Библиотечная функция play_clip(key) + тонкий CLI. Работает в
процессе того, кто позвал: in-process импортом из smos.py / updater.py по
событию инфраструктуры, либо подпроцессом из любого скрипта.

    python3 system/sysaudio/sysaudio.py system_started
    python3 system/sysaudio/sysaudio.py --list
    python3 system/sysaudio/sysaudio.py --generate "любой текст"

Клип <key> — готовый файл clips/<lang>/<key>.wav, собранный generate.py.
Набор ключей = файлы в папке (их и создаёт generate.py из своего PHRASES).
Динамику (числа, версии, даты) сюда не носить — это system/audio/ (TTS).

--generate <текст>: разовый синтез произвольной фразы ТЕМ ЖЕ голосом
Piper (ru_RU-dmitri-medium), играет и удаляет. НЕ сохраняет в clips/ —
там живут только фразы с ключом (см. generate.py PHRASES). Нужен
piper-tts и модель (качается при первом вызове).

Любой сбой (нет клипа, нет проигрывателя, piper не установлен) — в лог и
на stderr, без исключения: голос системы не должен ронять того, кто его
позвал (тот же принцип, что у log_client).

Зависимость только от проигрывателя WAV (aplay/paplay) — для проигрывания
клипов. piper нужен ТОЛЬКО для --generate.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
from log_client import send_log  # noqa: E402

CFG = config.load(SCRIPT_DIR)

ENABLED = CFG["enabled"]
LANG = CFG["lang"]
CLIPS_DIR = SCRIPT_DIR / CFG["paths"]["clips_dir"] / LANG
LEAD_TONE_FILE = CLIPS_DIR / "lead_tone.wav"
PLAYERS = [cmd for cmd in CFG["players"] if cmd]
PLAY_TIMEOUT_SEC = CFG["play_timeout_sec"]
LEAD_TONE = CFG["lead_tone"]


def _log(msg: str) -> None:
    """Человекочитаемо — на stderr, чтобы не мешать stdout вызывающего."""
    print(f"[sysaudio] {msg}", file=sys.stderr)


def _find_player() -> list[str] | None:
    """Первая команда из CFG['players'], чья программа есть в PATH."""
    for cmd in PLAYERS:
        if shutil.which(cmd[0]):
            return cmd
    return None


def _play_file(path: Path, wait: bool) -> None:
    """Проигрывает один WAV. wait=False — не ждать конца (fire-and-forget).
    Любой сбой — в лог, без исключения."""
    player = _find_player()
    if player is None:
        tried = [c[0] for c in PLAYERS]
        _log(f"нет проигрывателя WAV (пробовал: {tried})")
        send_log("WARNING", "no_player", {"tried": tried})
        return
    cmd = [*player, str(path)]
    try:
        if wait:
            subprocess.run(cmd, capture_output=True, timeout=PLAY_TIMEOUT_SEC)
        else:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        _log(f"проигрывание {path.name} не уложилось в {PLAY_TIMEOUT_SEC}с")
        send_log("WARNING", "play_timeout", {"file": path.name})
    except OSError as e:
        _log(f"не удалось запустить проигрыватель: {e}")
        send_log("WARNING", "play_failed", {"file": path.name, "error": str(e)})


def play_clip(key: str, wait: bool = True) -> None:
    """Проиграть системный клип по ключу — clips/<lang>/<key>.wav.

    Это и есть точка, которую in-process зовут smos.py / updater.py.
    Любой сбой (нет клипа, нет проигрывателя) — тихо в лог + stderr, не
    бросает: голос системы не роняет вызывающего. wait=False —
    не дожидаться конца проигрывания."""
    if not ENABLED:
        _log(f"(выключено) {key}")
        return
    clip = CLIPS_DIR / f"{key}.wav"
    if not clip.is_file():
        _log(f"нет клипа {key!r} ({clip})")
        send_log("WARNING", "clip_missing", {"key": key})
        return
    if LEAD_TONE and LEAD_TONE_FILE.is_file():
        _play_file(LEAD_TONE_FILE, wait=True)  # тон всегда синхронно — он короткий
    _play_file(clip, wait=wait)
    send_log("INFO", "clip_played", {"key": key})


def generate_and_play(text: str) -> None:
    """Разовый синтез произвольной фразы голосом Piper + проигрывание.
    НЕ сохраняет: в clips/ только фразы с ключом (generate.py PHRASES).
    Временный файл удаляется в любом случае. piper/модель недоступны —
    в лог, без исключения."""
    if not ENABLED:
        _log(f"(выключено) --generate {text!r}")
        return

    import generate  # локальный импорт: build-time модуль, в обычном пути клипов не нужен

    fd, tmp = tempfile.mkstemp(prefix="sysaudio_adhoc_", suffix=".wav")
    os.close(fd)
    tmp_path = Path(tmp)
    try:
        generate.synthesize(text, tmp_path)
    except generate.GenerateError as e:
        _log(f"--generate не удалось: {e}")
        send_log("WARNING", "adhoc_synth_failed", {"error": str(e)})
        tmp_path.unlink(missing_ok=True)
        return

    _play_file(tmp_path, wait=True)
    tmp_path.unlink(missing_ok=True)
    send_log("INFO", "adhoc_played", {"chars": len(text)})


def _available_keys() -> list[str]:
    if not CLIPS_DIR.exists():
        return []
    return sorted(p.stem for p in CLIPS_DIR.glob("*.wav") if p.stem != "lead_tone")


def _list() -> None:
    keys = _available_keys()
    if not keys:
        _log(f"в {CLIPS_DIR} нет клипов — собери их: python3 {SCRIPT_DIR / 'generate.py'}")
        return
    texts = {}
    try:
        import generate
        texts = generate.PHRASES
    except Exception:  # noqa: BLE001 — generate не обязателен для показа списка
        pass
    print(f"клипы ({LANG}):")
    width = max(len(k) for k in keys)
    for key in keys:
        print(f"  {key.ljust(width)}  {texts.get(key, '')}")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return
    if args[0] == "--list":
        _list()
        return
    if args[0] == "--generate":
        text = " ".join(args[1:]).strip()
        if not text:
            sys.exit("[sysaudio] --generate требует текст: sysaudio.py --generate \"...\"")
        generate_and_play(text)
        return

    key = args[0]
    if key not in _available_keys():
        _log(f"неизвестный ключ {key!r}. Есть: {_available_keys() or '—'}")
        sys.exit(1)
    play_clip(key)


if __name__ == "__main__":
    main()
