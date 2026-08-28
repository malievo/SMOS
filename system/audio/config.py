"""
config.py — загрузка настроек аудио-демона SMOS (v1).

Та же схема, что и в system/swl/config.py и system/core/config.py: все
настройки в одном JSON-файле — user/configs/audio.json, в общей папке
пользовательских настроек в корне проекта (рядом с файлом-маркером
smos.root). Читается заново при каждом запуске. Если файла нет, корень
не найден или JSON битый — audio.py не падает, работает на DEFAULTS и
печатает предупреждение. Частично заполненный файл валиден (рекурсивное
слияние с DEFAULTS).

v1 — это только «сказать текст». Полный аудио-демон (приоритетная
очередь, приглушение музыки, будильник поверх всего) — см.
audio_design.md, здесь не реализован.

Движки синтеза (tts.engine):
- "gtts"    — Google Text-to-Speech (gtts): синтез в MP3 через интернет,
              проигрывание внешним плеером (tts.gtts.player). Нужен
              `pip install gtts` и плеер MP3 (по умолчанию gst-play-1.0).
- "spd-say" — speech-dispatcher, локально/оффлайн, без зависимостей питона.
Первичный движок упал/недоступен → пробуется tts.fallback_engine →
печать текста. Сбой озвучки никогда не роняет демон и не стопорит очередь.

Использование в audio.py:
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["tts"]["engine"]
    ...
"""

import copy
import json
from pathlib import Path

CONFIG_NAME = "audio.json"
ROOT_MARKER = "smos.root"

DEFAULTS = {
    # Пауза между проверками папки заявок на озвучку (tasks/), сек.
    "check_interval_sec": 0.3,

    "tts": {
        # Включён ли синтез речи. false — заявки только печатаются в
        # консоль (полезно на машине без звука / при отладке конвейера).
        "enabled": True,

        # Первичный движок: "gtts" | "spd-say".
        "engine": "gtts",
        # Запасной движок, если первичный не сработал (нет пакета/сети/
        # плеера/команды). "" — не пробовать запасной, сразу печать.
        "fallback_engine": "spd-say",

        "gtts": {
            # Язык синтеза и «домен» голоса Google (tld: com, ru, co.uk…).
            "lang": "ru",
            "tld": "com",
            # true — медленная, «диктующая» речь.
            "slow": False,
            # Чем проиграть полученный MP3. Путь к временному файлу
            # добавляется последним аргументом. gst-play-1.0 (из
            # gstreamer1.0-tools) играет MP3 и сам завершается.
            # Альтернативы: ["mpg123","-q"], ["ffplay","-nodisp","-autoexit","-loglevel","quiet"].
            "player": ["gst-play-1.0", "--quiet"],
            # Потолок на синтез+проигрывание одной фразы, сек.
            "timeout_sec": 30,
        },

        "spd_say": {
            # Команда speech-dispatcher. Текст добавляется последним
            # аргументом. -w — ждать окончания фразы, -l ru — язык.
            "command": ["spd-say", "-w", "-l", "ru"],
            # Потолок на одно произнесение, сек.
            "timeout_sec": 30,
        },
    },

    "paths": {
        # Папка-очередь заявок на озвучку: outputstructurizer кладёт сюда
        # по одному <task_id>.json, этот демон их произносит и удаляет.
        # Относительно папки с audio.py. Рантайм-данные, в .gitignore.
        "tasks_dir": "tasks",
        # Куда убирать заявки, которые не удалось разобрать (битый JSON,
        # нет поля text). Подпапка tasks_dir.
        "rejected_subdir": "rejected",
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
    DEFAULTS. base_dir — папка вызывающего скрипта (SCRIPT_DIR): от неё
    ищется корень проекта. Наружу не бросает исключений: корень не
    найден, файла нет, битый JSON или не JSON-объект — печатает
    предупреждение и возвращает DEFAULTS."""
    root = _project_root(base_dir)
    if root is None:
        print(f"[config] не найден корень проекта (файл {ROOT_MARKER}) — использую значения по умолчанию.")
        return copy.deepcopy(DEFAULTS)

    config_file = root / "user" / "configs" / CONFIG_NAME

    if not config_file.exists():
        print(f"[config] {config_file} не найден — использую значения по умолчанию.")
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
