"""
config.py — загрузка настроек системного аудио SMOS.

Та же схема, что и в system/audio/config.py, system/swl/config.py и др.:
все настройки в одном JSON — user/configs/sysaudio.json, в общей папке
пользовательских настроек рядом с файлом-маркером smos.root. Читается
заново при каждом запуске. Файла нет / корень не найден / битый JSON —
sysaudio не падает, работает на DEFAULTS. Частично заполненный файл
валиден (рекурсивное слияние с DEFAULTS).

sysaudio проигрывает заранее собранные клипы системных событий
(clips/<lang>/<ключ>.wav, собирает generate.py). Не демон, не TTS в
рантайме — см. sysaudio_design.md.

Использование в sysaudio.py:
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["players"]
    ...
"""

import copy
import json
from pathlib import Path

CONFIG_NAME = "sysaudio.json"
ROOT_MARKER = "smos.root"

DEFAULTS = {
    # false — play_clip() ничего не проигрывает, только пишет в лог/stderr
    # (машина без звука, отладка).
    "enabled": True,

    # Какая подпапка clips/<lang>/ используется.
    "lang": "ru",

    # Проигрыватель WAV: список команд-кандидатов, берётся первая, чья
    # программа есть в PATH. Путь к файлу добавляется последним аргументом.
    # aplay — из alsa-utils, paplay — из PulseAudio; одна из них есть
    # почти на любой Linux-машине. WAV играется без декодера.
    "players": [["aplay", "-q"], ["paplay"]],

    # Потолок на проигрывание одного файла, сек (предохранитель от зависшего
    # проигрывателя).
    "play_timeout_sec": 15,

    # Играть ли короткий тон-префикс (clips/<lang>/lead_tone.wav) перед
    # каждым клипом — сигнал «говорит система», а не ассистент.
    "lead_tone": True,

    "paths": {
        # Папка с клипами, относительно sysaudio.py. Внутри — подпапки по
        # языку (clips/ru/, clips/en/, ...). Клипы — ассеты, под гитом.
        "clips_dir": "clips",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно накладывает override поверх base. Ключи, отсутствующие
    в override, остаются от base — config.json можно заполнять частично."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _project_root(start: Path) -> Path | None:
    """Поднимается от start вверх до папки с файлом-маркером ROOT_MARKER
    (корень проекта SMOS). None — если маркер не найден нигде выше."""
    start = Path(start).resolve()
    for folder in (start, *start.parents):
        if (folder / ROOT_MARKER).exists():
            return folder
    return None


def load(base_dir: Path) -> dict:
    """Загружает user/configs/<CONFIG_NAME> и накладывает его поверх
    DEFAULTS. base_dir — папка вызывающего скрипта (SCRIPT_DIR). Наружу не
    бросает исключений: корень не найден, файла нет, битый JSON или не
    JSON-объект — печатает предупреждение и возвращает DEFAULTS."""
    root = _project_root(base_dir)
    if root is None:
        print(f"[config] не найден корень проекта (файл {ROOT_MARKER}) — использую значения по умолчанию.")
        return copy.deepcopy(DEFAULTS)

    config_file = root / "user" / "configs" / CONFIG_NAME

    if not config_file.exists():
        return copy.deepcopy(DEFAULTS)

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            user_config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] Не удалось прочитать {config_file} ({e}) — использую значения по умолчанию.")
        return copy.deepcopy(DEFAULTS)

    if not isinstance(user_config, dict):
        print(f"[config] {config_file} должен содержать JSON-объект — использую значения по умолчанию.")
        return copy.deepcopy(DEFAULTS)

    return _deep_merge(DEFAULTS, user_config)
