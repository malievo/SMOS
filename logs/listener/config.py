"""
config.py — загрузка настроек демона логов (logs/listener) из config.json.

Та же идея, что в system/fwl/rvs/config.py: все настройки лежат в config.json
рядом со скриптом, чтобы менять поведение без правки кода. Если
config.json отсутствует, не найден или содержит битый JSON — listener.py
не падает, а работает на значениях по умолчанию (DEFAULTS) и печатает
предупреждение. Если в config.json заполнены не все поля — недостающие
берутся из DEFAULTS (рекурсивное слияние).
"""

import copy
import json
from pathlib import Path

CONFIG_FILENAME = "config.json"

DEFAULTS = {
    "listener": {
        # Адрес (UDP), который слушает демон. Все модули системы шлют
        # события сюда — фиксированный, общеизвестный адрес.
        "host": "127.0.0.1",
        "port": 47110,
    },
    "paths": {
        # Папка с сырыми событиями, относительно папки logs/ (на уровень
        # выше папки listener/). Внутри неё демон сам создаёт подпапку на
        # каждый модуль и пишет туда events_filename.
        "raw_dir": "raw",
        "events_filename": "events.jsonl",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно накладывает override поверх base. Ключи, отсутствующие
    в override, остаются от base — то есть config.json можно заполнять
    частично."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load(base_dir: Path) -> dict:
    """Загружает config.json из папки base_dir (обычно — папка самого
    скрипта) и накладывает его поверх DEFAULTS. Не бросает исключений
    наружу: если файла нет или он битый — печатает предупреждение и
    возвращает DEFAULTS."""
    config_file = Path(base_dir) / CONFIG_FILENAME

    if not config_file.exists():
        print(f"[config] {CONFIG_FILENAME} не найден рядом со скриптом — использую значения по умолчанию.")
        return copy.deepcopy(DEFAULTS)

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            user_config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] Не удалось прочитать {CONFIG_FILENAME} ({e}) — использую значения по умолчанию.")
        return copy.deepcopy(DEFAULTS)

    if not isinstance(user_config, dict):
        print(f"[config] {CONFIG_FILENAME} должен содержать JSON-объект — использую значения по умолчанию.")
        return copy.deepcopy(DEFAULTS)

    return _deep_merge(DEFAULTS, user_config)
