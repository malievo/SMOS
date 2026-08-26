"""
config.py — загрузка настроек демона ядра SMOS.

Та же схема, что и в system/fwl/rvs/config.py и
system/fwl/classifier/config.py: все настройки в одном JSON-файле —
user/configs/core.json, в общей папке пользовательских настроек в корне
проекта (рядом с файлом-маркером smos.root). Читается заново при каждом
запуске (на лету не подхватывается). Если файла нет, корень не найден
или JSON битый — core.py не падает, работает на DEFAULTS и печатает
предупреждение. DEFAULTS — и поставляемый baseline, и страховка.
Частично заполненный файл валиден — недостающие поля берутся из
DEFAULTS (рекурсивное слияние).

Использование в core.py (без изменений):
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["check_interval_sec"]
    ...

Про сам module_init/ (сканирование модулей) конфига нет намеренно — там
нечего настраивать: пути к папкам модулей фиксированы структурой
проекта (system/modules/ и user/modules/), см. registry.py.
"""

import copy
import json
from pathlib import Path

CONFIG_NAME = "core.json"
ROOT_MARKER = "smos.root"

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
    предупреждение и возвращает DEFAULTS, чтобы опечатка в конфиге не
    уронила ядро."""
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
