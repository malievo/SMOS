"""
config.py — загрузка настроек демона логов (logs/listener).

Та же идея, что в system/fwl/rvs/config.py: все настройки лежат в одном
JSON-файле — user/configs/logs.json, в общей папке пользовательских
настроек в корне проекта (рядом с файлом-маркером smos.root). Меняешь
значение — перезапускаешь listener.py. Если файла нет, корень не найден
или JSON битый — listener.py не падает, работает на значениях по
умолчанию (DEFAULTS) и печатает предупреждение. DEFAULTS — и
поставляемый baseline, и страховка. Если заполнены не все поля —
недостающие берутся из DEFAULTS (рекурсивное слияние).
"""

import copy
import json
from pathlib import Path

# Имя этого конфига внутри user/configs/. ROOT_MARKER — пустой файл в
# корне проекта, по нему находится папка user/ независимо от того,
# откуда запущен скрипт.
CONFIG_NAME = "logs.json"
ROOT_MARKER = "smos.root"

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
    ищется корень проекта. Не бросает исключений наружу: корень не
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
