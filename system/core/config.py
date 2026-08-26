"""
config.py — загрузка настроек демона ядра SMOS из config.json.

Та же схема, что и в system/fwl/rvs/config.py и
system/fwl/classifier/config.py: все настройки в одном config.json рядом
с core.py, читается заново при каждом запуске (на лету не
подхватывается). Если файла нет или он битый — core.py не падает,
работает на DEFAULTS и печатает предупреждение. Частично заполненный
config.json тоже валиден — недостающие поля берутся из DEFAULTS
(рекурсивное слияние).

Использование в core.py:
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["check_interval_sec"]
    ...

Про сам module_init/ (сканирование модулей) конфига нет намеренно — там
нечего настраивать: пути к папкам модулей фиксированы структурой
проекта (system/modules/ и modules/ в корне), см. registry.py.
"""

import copy
import json
from pathlib import Path

CONFIG_FILENAME = "config.json"

DEFAULTS = {
    # Пауза между проверками папки-очереди целей (system/core/goals/), сек.
    # Очередь наполняется редко (одна фраза пользователя за раз) — частить
    # незачем, но и большой лаг между командой и реакцией не нужен.
    "check_interval_sec": 0.3,

    "paths": {
        # Папка-очередь: SWL кладёт сюда по одному goal.json на команду,
        # ядро их разбирает и удаляет. Относительно папки с core.py.
        # Сама папка — рантайм-данные, в .gitignore (как tasks/, output/).
        "goals_dir": "goals",
        # Подпапка внутри goals_dir, куда складываются файлы целей, которые
        # не удалось разобрать (нет поля goal, битый JSON и т.п.) — чтобы
        # они не крутились в очереди вечно, но и не пропадали молча, можно
        # посмотреть глазами, что пришло не так.
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


def load(base_dir: Path) -> dict:
    """Загружает config.json из папки base_dir (обычно SCRIPT_DIR самого
    core.py) и накладывает его поверх DEFAULTS. Наружу не бросает
    исключений: если файла нет или он битый — печатает предупреждение и
    возвращает DEFAULTS, чтобы опечатка в конфиге не уронила ядро."""
    config_file = Path(base_dir) / CONFIG_FILENAME

    if not config_file.exists():
        print(f"[config] {CONFIG_FILENAME} не найден рядом с core.py — использую значения по умолчанию.")
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
