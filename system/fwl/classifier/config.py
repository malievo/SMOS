"""
config.py — загрузка настроек классификатора SMOS.

Та же схема, что и в system/fwl/rvs/config.py: все настройки в одном
JSON-файле — user/configs/classifier.json, в общей папке
пользовательских настроек в корне проекта (рядом с файлом-маркером
smos.root). Читается заново при каждом запуске (на лету не
подхватывается). Если файла нет, корень не найден или JSON битый —
скрипты не падают, работают на DEFAULTS и печатают предупреждение.
DEFAULTS — и поставляемый baseline, и страховка. Заполнять файл можно
частично — недостающие поля берутся из DEFAULTS (рекурсивное слияние).

Секрет облачного провайдера (GIGACHAT_CREDENTIALS) лежит отдельно, в
user/.env — см. user_env_file() ниже; его читает ai_provider.py.

Использование в classifier.py / ai_provider.py (без изменений):
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["ai"]["model"]
    ...
"""

import copy
import json
from pathlib import Path

# Имя этого конфига внутри user/configs/. ROOT_MARKER — пустой файл в
# корне проекта, по нему находится папка user/ независимо от того,
# откуда запущен скрипт.
CONFIG_NAME = "classifier.json"
ROOT_MARKER = "smos.root"

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
    # rvs_output_dir — он относительно СОСЕДНЕЙ папки system/fwl/rvs).
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
        # system/fwl/rvs/flags/). Пока в ней всего один флаг — promoted_flag.
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


def _project_root(start: Path) -> Path | None:
    """Поднимается от start вверх до папки с файлом-маркером ROOT_MARKER
    (корень проекта SMOS). None — если маркер не найден нигде выше."""
    start = Path(start).resolve()
    for folder in (start, *start.parents):
        if (folder / ROOT_MARKER).exists():
            return folder
    return None


def user_env_file(start: Path) -> Path | None:
    """Путь к user/.env — общему секрету облачных провайдеров (GigaChat).
    None, если корень проекта не найден. Читается в ai_provider.py."""
    root = _project_root(start)
    return root / "user" / ".env" if root else None


def load(base_dir: Path) -> dict:
    """Загружает user/configs/<CONFIG_NAME> и накладывает его поверх
    DEFAULTS. base_dir — папка вызывающего скрипта (SCRIPT_DIR): от неё
    ищется корень проекта.

    Не бросает исключений наружу: корень не найден, файла нет, битый
    JSON или не JSON-объект — печатает предупреждение и возвращает
    DEFAULTS, чтобы опечатка в конфиге не роняла весь скрипт."""
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
