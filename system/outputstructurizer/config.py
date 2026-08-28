"""
config.py — загрузка настроек outputstructurizer.

Та же схема, что и в system/swl/config.py и system/fwl/classifier/config.py:
все настройки в одном JSON-файле — user/configs/outputstructurizer.json,
в общей папке пользовательских настроек в корне проекта (рядом с
файлом-маркером smos.root). Читается заново при каждом запуске (на лету
не подхватывается). Если файла нет, корень не найден или JSON битый —
скрипт не падает, работает на DEFAULTS и печатает предупреждение.
DEFAULTS — и поставляемый baseline, и страховка. Частично заполненный
файл валиден (рекурсивное слияние с DEFAULTS).

ВАЖНО: таблица готовых фраз (phrases.json) — это НЕ настройки, а контент,
и живёт отдельным файлом рядом с кодом (под гитом, правится руками).
Здесь только параметры процесса: интервал опроса, пути очередей, шаблон
ответа на ошибку.

Секрет облачного провайдера (GIGACHAT_CREDENTIALS) лежит отдельно, в
user/.env — см. user_env_file() ниже; его читает phrase_provider.py.

Использование в outputstructurizer.py / phrase_provider.py:
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["check_interval_sec"]
    ...
"""

import copy
import json
from pathlib import Path

CONFIG_NAME = "outputstructurizer.json"
ROOT_MARKER = "smos.root"

DEFAULTS = {
    # Пауза между проверками очереди результатов (queue/), сек. Очередь
    # наполняется редко (одна выполненная команда за раз) — частить
    # незачем, но и заметного лага между «сделано» и «сказано» не нужно.
    "check_interval_sec": 0.3,

    "ai": {
        # GigaChat как формулировщик ответа на bootstrap-этапе (см.
        # outputstructurizer_design.md — позже дистилляция в локальную
        # модель / передача в chat-ветку, как у классификатора и SWL).
        # Те же ключи и значения, что у классификатора и SWL.
        "model": "GigaChat-2",
        # Небольшая температура: нам нужен связный короткий ответ, а
        # мёртвая детерминированность здесь не так важна, как в разборе
        # цели — но разнообразие тоже ни к чему, ответы должны быть
        # предсказуемо короткими.
        "temperature": 0.3,
        # Ответ — одна короткая фраза для озвучки, не абзац.
        "max_tokens": 120,
        # GigaChat использует сертификат российского Минцифры — без
        # этого флага стандартные библиотеки его не узнают (та же
        # причина и значение, что у классификатора и SWL). Правильная
        # альтернатива на будущее — ca_bundle_file с реальным корневым
        # сертификатом вместо отключения проверки.
        "verify_ssl_certs": False,
    },

    "phrasing": {
        # Если готовой фразы под goal-ключ нет и формулировщик (GigaChat)
        # недоступен — этим шаблоном отвечаем на ошибку (последний слой
        # fallback для status == "error"). {error} подставляется.
        "error_fallback_template": "Не удалось выполнить: {error}",
        # То же для успеха без шаблона и без формулировщика: сырое
        # значение результата словами. {result} подставляется.
        "result_fallback_template": "Готово: {result}",
        # Если у goal-ключа в phrases.json список вариантов — брать
        # случайный (немного разнообразия в подтверждениях). false —
        # всегда первый.
        "random_variants": True,
    },

    # Уровень привилегий, который проставляется в заявке для аудио. Для
    # ответов модулей пользователю он всегда 2. Другие уровни (будильник,
    # критическая ошибка, ambient) появятся вместе с приоритетной
    # очередью настоящего аудио-демона — см. ../audio/audio_design.md.
    "privilege_level": 2,

    "paths": {
        # Вход: очередь файлов-результатов, которую наполняет ядро
        # (task_runner._emit_completion пишет сюда по файлу на
        # завершённую задачу). Относительно папки со скриптом.
        "queue_dir": "queue",
        # Куда убирать файлы, которые не удалось разобрать (битый JSON,
        # нет обязательных полей) — чтобы не крутились в очереди вечно и
        # не пропадали молча. Подпапка queue_dir.
        "rejected_subdir": "rejected",
        # Куда убирать файлы, найденные в очереди УЖЕ на старте демона:
        # озвученный ответ полезен только свежим, а не «пачкой за час».
        # Их не произносим, но и не теряем — видно глазами. Подпапка
        # queue_dir.
        "stale_subdir": "stale",
        # Выход: папка-очередь заявок на озвучку. outputstructurizer
        # кладёт сюда по одному <task_id>.json на ответ, аудио-демон их
        # произносит. Относительно папки со скриптом.
        "audio_tasks_dir": "../audio/tasks",
        # Поставляемая таблица готовых фраз (goal-ключ -> шаблон/шаблоны).
        # Рядом со скриптом, под гитом. Относительно папки со скриптом.
        "phrases_file": "phrases.json",
        # Накопительный датасет: фраза-ответ, сформулированная GigaChat
        # (только LLM-слой — это и есть «учительский» сигнал), + goal и
        # result. Материал для будущей локальной модели-формулировщика,
        # по аналогии с dataset.jsonl классификатора и SWL. JSONL,
        # дозапись.
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
    None, если корень проекта не найден. Читается в phrase_provider.py."""
    root = _project_root(start)
    return root / "user" / ".env" if root else None


def load(base_dir: Path) -> dict:
    """Загружает user/configs/<CONFIG_NAME> и накладывает его поверх
    DEFAULTS. base_dir — папка вызывающего скрипта (SCRIPT_DIR): от неё
    ищется корень проекта. Наружу не бросает исключений: корень не
    найден, файла нет, битый JSON или не JSON-объект — печатает
    предупреждение и возвращает DEFAULTS, чтобы опечатка в конфиге не
    уронила процесс."""
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
