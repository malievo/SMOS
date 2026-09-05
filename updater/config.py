"""
config.py — загрузка настроек апдейтера SMOS.

Та же схема, что и у остальных процессов (system/audio/config.py,
system/fwl/rvs/config.py и т.д.): все настройки в одном JSON-файле —
user/configs/updater.json, в общей папке пользовательских настроек в
корне проекта (рядом с файлом-маркером smos.root). Читается заново при
каждом запуске. Если файла нет, корень не найден или JSON битый —
updater.py не падает, работает на DEFAULTS и печатает предупреждение.
Частично заполненный файл валиден (рекурсивное слияние с DEFAULTS).

github.owner/github.repo пустые по умолчанию — их обязательно заполнить
в user/configs/updater.json, как только проект окажется в репозитории на
GitHub. Пустые значения — не ошибка конфига, а осознанный сигнал
"обновления ещё не настроены": cmd_check() в этом случае просто вернёт
no_update и не будет пытаться ходить в сеть.

Подробности всего цикла обновления — updater_design.md рядом с этим
файлом.

Использование в updater.py:
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["github"]["owner"]
    ...

ВАЖНО, отличие от копий этого файла в других процессах: предупреждения
здесь печатаются в stderr, не в stdout. У updater.py stdout — не просто
человекочитаемый вывод для дебага (как у демонов, которые smos.py
ретранслирует построчно), а машинно разбираемый контракт: ровно одна
JSON-строка с результатом (см. updater.py). Предупреждение о конфиге,
случайно попавшее в stdout, сломало бы этот разбор.
"""

import copy
import json
import sys
from pathlib import Path

CONFIG_NAME = "updater.json"
ROOT_MARKER = "smos.root"

DEFAULTS = {
    # Выключить проверку обновлений совсем (альтернатива разовому
    # флагу --no-update у smos.py).
    "enabled": True,

    "github": {
        # Владелец и имя репозитория на GitHub — обязательно заполнить
        # в user/configs/updater.json. Пустые значения -> cmd_check()
        # сразу возвращает no_update, в сеть не ходит.
        "owner": "",
        "repo": "",
        # Ветка, с которой сверяется версия и с которой скачивается zip
        # при обновлении.
        "branch": "main",
    },

    # Таймаут запроса project_info.json с GitHub (raw.githubusercontent.com), сек.
    "check_timeout_sec": 5,
    # Таймаут скачивания zip-архива всего проекта при apply, сек —
    # щедрее, чем check_timeout_sec, это весь проект, не один файл.
    "download_timeout_sec": 60,
    # Сколько секунд ждать ОДНОЗНАЧНОГО ответа (да/нет/отмена) в
    # голосовом диалоге, прежде чем считать это отменой. Не то же самое,
    # что "первая попытка записи" — случайные срабатывания wake.py
    # --nowake от шума и нераспознанный/неоднозначный текст ожидание не
    # прерывают (см. updater.py _wait_for_answer), таймер общий на весь
    # диалог.
    "dialog_timeout_sec": 30,

    # Сколько часов не переспрашивать про ОДНУ И ТУ ЖЕ версию после
    # отказа/отложить ("нет", "потом", "напомни позже" — см. updater.py
    # _NO_WORDS). Если на GitHub тем временем выйдет более новая версия
    # — спросит сразу, не дожидаясь конца окна (см. updater.py cmd_check).
    "decline_cooldown_hours": 24,

    # Сколько секунд после перезапуска smos.py на новой версии следить
    # за логами (logs/raw/*/events.jsonl), прежде чем решить, что
    # обновление прижилось. См. updater_design.md, раздел «Здоровье
    # после обновления».
    "health_check_timeout_sec": 25,
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
        print(f"[config] не найден корень проекта (файл {ROOT_MARKER}) — использую значения по умолчанию.", file=sys.stderr)
        return copy.deepcopy(DEFAULTS)

    config_file = root / "user" / "configs" / CONFIG_NAME

    if not config_file.exists():
        print(f"[config] {config_file} не найден — использую значения по умолчанию.", file=sys.stderr)
        return copy.deepcopy(DEFAULTS)

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            user_config = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[config] Не удалось прочитать {config_file} ({e}) — использую значения по умолчанию.", file=sys.stderr)
        return copy.deepcopy(DEFAULTS)

    if not isinstance(user_config, dict):
        print(f"[config] {config_file} должен содержать JSON-объект — использую значения по умолчанию.", file=sys.stderr)
        return copy.deepcopy(DEFAULTS)

    return _deep_merge(DEFAULTS, user_config)
