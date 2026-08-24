"""
config.py — загрузка настроек классификатора SMOS из config.json.

Та же схема, что и в system/rvs/config.py: все настройки в одном
config.json рядом со скриптами, читается заново при каждом запуске
(на лету не подхватывается). Если файла нет или он битый — скрипты не
падают, работают на DEFAULTS и печатают предупреждение в консоль. Если
заполнены не все поля — недостающие берутся из DEFAULTS (рекурсивное
слияние), конфиг можно редактировать частично.

Использование в classifier.py / ai_provider.py:
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
    # Пауза между проверками recognized.json в classifier.py, сек.
    "check_interval_sec": 0.5,

    "ai": {
        # Какая модель GigaChat используется для классификации.
        "model": "GigaChat-2",
        # 0.0 — детерминированный ответ (нам нужна стабильная метка,
        # а не разнообразие формулировок).
        "temperature": 0.0,
        # Ответ модели — одно слово (command/chat), большого лимита не
        # нужно, но с запасом на случай лишних символов.
        "max_tokens": 10,
        # GigaChat использует сертификат российского Минцифры — без
        # этого флага стандартные библиотеки его не узнают. Правильная
        # альтернатива на будущее — указать ca_bundle_file с реальным
        # корневым сертификатом вместо отключения проверки.
        "verify_ssl_certs": False,
    },

    # Имена файлов и папок, все относительно папки со скриптами (кроме
    # rvs_output_dir — он относительно СОСЕДНЕЙ папки system/rvs).
    "paths": {
        "rvs_output_dir": "../rvs/output",
        "rvs_output_file": "recognized.json",
        "output_dir": "output",
        "output_file": "classified.json",
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
    скрипта, SCRIPT_DIR) и накладывает его поверх DEFAULTS.

    Не бросает исключений наружу: если файла нет или он битый —
    печатает предупреждение и возвращает DEFAULTS, чтобы опечатка в
    конфиге не роняла весь скрипт."""
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
