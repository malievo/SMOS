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
        # Накопительный датасет (фраза + метка от онлайн-ИИ) для
        # обучения локальной модели, см. model_combination_design.md.
        # JSONL — по одной записи на строку, дописывается, не
        # перезаписывается. Пополняется и в bootstrap-, и в
        # shadow-режиме (везде, где ещё вызывается онлайн-ИИ).
        "dataset_file": "dataset.jsonl",
        # Счётчики shadow-режима (сколько сравнений с момента последнего
        # обучения, сколько из них совпало) — не флаг, а числа, поэтому
        # рядом с датасетом, а не в flags/.
        "shadow_state_file": "shadow_state.json",
        # Папка с простыми файлами-маркерами (по аналогии с
        # system/rvs/flags/). Пока в ней всего один флаг — promoted_flag.
        "flags_dir": "flags",
        # Наличие этого файла = локальная модель сейчас основная
        # (champion). Отсутствие = используется онлайн-ИИ (bootstrap
        # или shadow-режим — см. model_combination_design.md).
        "promoted_flag": "promoted.flag",
        # Наличие = проверочное окно после переключения пройдено успешно,
        # онлайн-ИИ больше НИКОГДА не вызывается (полностью офлайн). Это
        # финальное состояние — намеренно нет автоматического пути назад
        # из него (см. model_combination_design.md: страховка должна
        # быть ограничена по времени, а не работать вечно — иначе ИИ
        # никогда не станет по-настоящему бесплатным/офлайн).
        "offline_only_flag": "offline_only.flag",
    },

    "local_model": {
        # Модель эмбеддингов текста (sentence-transformers), см.
        # embedding_classifier_design.md.
        "embedding_model": "cointegrated/rubert-tiny2",
        # Куда сохраняется обученный классификатор поверх эмбеддингов.
        "model_file": "local_model.joblib",
        # Перед перезаписью model_file текущая версия копируется сюда —
        # один уровень отката на случай неудачного дообучения.
        "model_backup_file": "local_model.joblib.bak",
        # Минимум примеров НА КАЖДЫЙ класс (command/chat) в датасете,
        # чтобы train_local_model.py вообще согласился обучаться в
        # первый раз — защита от заведомо несбалансированной/слишком
        # маленькой выборки. Подобрано на глаз, скорректировать по
        # практике.
        "min_examples_per_class": 30,
    },

    # Настройки shadow-режима (шаги 3b-3d в model_combination_design.md):
    # локальная модель уже обучена, но ещё не основная — работает
    # параллельно с онлайн-ИИ только для сравнения.
    "shadow": {
        # Сколько НОВЫХ примеров должно накопиться в датасете с момента
        # последнего обучения, чтобы classifier.py сам запустил
        # дообучение локальной модели.
        "retrain_batch_size": 50,
        # Минимальное число сравнений (онлайн-ИИ vs локальная модель) с
        # момента последнего обучения, прежде чем вообще рассматривать
        # переключение — иначе можно переключиться по случайному
        # везению на первых нескольких фразах.
        "min_comparisons_for_promotion": 30,
        # Доля совпадений (0..1) среди этих сравнений, при которой
        # локальная модель считается готовой стать основной. Тот же
        # порог используется и для проверочного окна после переключения
        # (ниже) — не разводим два похожих числа без причины.
        "agreement_threshold": 0.9,
        # ПОСЛЕ переключения (флаг promoted уже есть) реальный ответ уже
        # даёт локальная модель, но ещё это число раз онлайн-ИИ
        # дополнительно спрашивается параллельно — только для проверки,
        # не подменяя ответ. Это ограниченное по времени окно, не
        # постоянный режим (см. offline_only_flag выше) — так GigaChat
        # рано или поздно перестаёт быть нужен вообще, и не приходится
        # держать платный/общий ключ работающим бесконечно.
        "post_promotion_validation_comparisons": 40,
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
