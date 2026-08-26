"""
config.py — загрузка настроек SWL из config.json.

Та же схема, что и в system/fwl/rvs/config.py и
system/fwl/classifier/config.py: все настройки в одном config.json
рядом со скриптами, читается заново при каждом запуске (на лету не
подхватывается). Если файла нет или он битый — скрипты не падают,
работают на DEFAULTS и печатают предупреждение. Частично заполненный
config.json тоже валиден (рекурсивное слияние с DEFAULTS).

Использование в swl.py / intent_provider.py:
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["ai"]["model"]
    ...
"""

import copy
import json
from pathlib import Path

CONFIG_FILENAME = "config.json"

DEFAULTS = {
    # Пауза между проверками classified.json классификатора, сек.
    # (тот же смысл, что check_interval_sec у классификатора для
    # recognized.json — SWL стоит следующим звеном той же цепочки).
    "check_interval_sec": 0.5,

    "ai": {
        # Модель GigaChat для разбора фразы в цель (bootstrap-этап, см.
        # swl_design.md — позже дистилляция в локальную модель, как у
        # классификатора).
        "model": "GigaChat-2",
        # 0.0 — детерминированный разбор: нам нужен стабильный JSON с
        # целью и параметрами, а не разнообразие формулировок.
        "temperature": 0.0,
        # Ответ — небольшой JSON-объект {"goal": ..., "params": {...}}.
        # С запасом на список параметров, но не огромный.
        "max_tokens": 300,
        # GigaChat использует сертификат российского Минцифры — без
        # этого флага стандартные библиотеки его не узнают (та же
        # причина и то же значение, что у классификатора). Правильная
        # альтернатива на будущее — ca_bundle_file с реальным корневым
        # сертификатом вместо отключения проверки.
        "verify_ssl_certs": False,
    },

    # Имена файлов и папок. Пути к чужим папкам (классификатор, ядро) —
    # относительно папки со скриптами SWL.
    "paths": {
        # Вход: что пишет классификатор. SWL реагирует только на записи
        # с label == "command".
        "classifier_output_dir": "../fwl/classifier/output",
        "classifier_output_file": "classified.json",
        # Выход: папка-очередь целей демона ядра. SWL кладёт сюда по
        # одному goal.json на команду, ядро (core.py) их разбирает.
        "core_goals_dir": "../core/goals",
        # Накопительный датасет (фраза + разобранная цель + параметры) —
        # материал для будущего обучения локальной модели-разборщика,
        # по аналогии с dataset.jsonl классификатора. JSONL, дозапись.
        "output_dir": "output",
        "dataset_file": "dataset.jsonl",
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
    """Загружает config.json из папки base_dir (обычно SCRIPT_DIR) и
    накладывает его поверх DEFAULTS. Наружу не бросает исключений: если
    файла нет или он битый — печатает предупреждение и возвращает
    DEFAULTS, чтобы опечатка в конфиге не уронила скрипт."""
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
