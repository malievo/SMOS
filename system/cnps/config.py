"""
config.py — загрузка настроек CNPS.

Та же схема, что и в system/audio/config.py, system/sysaudio/config.py,
system/swl/config.py и др.: все настройки в одном JSON —
user/configs/cnps.json, в общей папке пользовательских настроек в корне
проекта (рядом с файлом-маркером smos.root). Читается заново при каждом
запуске (на лету не подхватывается). Файла нет / корень не найден /
битый JSON — CNPS не падает, работает на DEFAULTS и печатает
предупреждение. Частично заполненный файл валиден (рекурсивное слияние
с DEFAULTS).

Смысл полей и решения по ним — см. system/cnps/cnps_design.md
(раздел «Форма и файлы» — набросок конфига, «Классы приоритета»,
«Аналитический слой»).

Использование:
    import config
    CFG = config.load(SCRIPT_DIR)
    CFG["classes"]["RESULT"]["ttl_soft_sec"]
    ...
"""

import copy
import json
from pathlib import Path

CONFIG_NAME = "cnps.json"
ROOT_MARKER = "smos.root"

# Классы важности от высшего к низшему. Индекс = «ранг»: меньше — важнее.
# Зажим манифеста по потолку источника = max(ранг запрошенного, ранг
# потолка) — нельзя подняться ВЫШЕ потолка (см. classify.py).
CLASS_ORDER = ["CRITICAL", "ALERT", "RESULT", "CHAT", "AMBIENT"]

DEFAULTS = {
    # false — CNPS принимает заявки и ведёт весь учёт (класс, TTL, Recap,
    # квитанции), но НЕ синтезирует и НЕ проигрывает: только пишет строку
    # в лог/консоль и считает заявку доставленной. Машина без звука /
    # отладка конвейера.
    "enabled": True,

    # Пауза между проходами приёмника заявок (tasks/) и housekeeping, сек.
    "check_interval_sec": 0.3,

    # Проигрыватель WAV: список команд-кандидатов, берётся первая, чья
    # программа есть в PATH. Путь к файлу добавляется последним аргументом.
    # aplay — из alsa-utils, paplay — из PulseAudio. Громкость (louder/
    # quieter) умеет только paplay (--volume); с aplay уровень громкости
    # запоминается, но на воспроизведение не влияет (см. cnps_design.md,
    # «Открытое»).
    "players": [["aplay", "-q"], ["paplay"]],
    # Потолок на проигрывание одного предложения/файла, сек (предохранитель
    # от зависшего проигрывателя).
    "play_timeout_sec": 30,

    "synth": {
        # Движки: "gtts" (Google TTS, ЖЕНСКИЙ голос, онлайн, MP3 — голос
        # ассистента: ответы outputstructurizer, болталка), "piper"
        # (нейросетевой, МУЖСКОЙ ru_RU-dmitri-medium, офлайн — голос
        # системных сообщений; и офлайн-запас для gtts), "spd-say"
        # (speech-dispatcher, последний запас).
        #
        # Какой движок у конкретной заявки:
        #   1) поле "voice" (или "engine") в манифесте заявки, если задано;
        #   2) иначе engine_by_source по её `source`;
        #   3) иначе engine (общий дефолт).
        "engine": "piper",
        "engine_by_source": {
            # Ответы модулей пользователю и болталка идут через
            # outputstructurizer — их озвучиваем женским gtts (голос
            # ассистента). Всё остальное (smos/updater/watcher/core) —
            # piper (голос системы).
            "outputstructurizer": "gtts",
        },
        # Цепочка запаса, если движок заявки не сработал (нет пакета /
        # сети / модели / команды). Строка или список; пробуются по
        # порядку, дубль с основным пропускается. gtts офлайн -> piper
        # -> spd-say. [] / "" — сразу печать текста.
        "fallback_engine": ["piper", "spd-say"],

        # Голос Piper. По умолчанию переиспользуется модель system/sysaudio/
        # (ru_RU-dmitri-medium) — CNPS работает «из коробки» без отдельной
        # загрузки. Это тот же голос, что у sysaudio; различие «ассистент
        # vs система» держится на gtts (женский) vs piper (мужской).
        "piper_voice": "ru_RU-dmitri-medium",
        # Где искать <piper_voice>.onnx (+ .onnx.json). Относительно папки
        # с cnps.py. По умолчанию — кэш моделей sysaudio.
        "model_dir": "../sysaudio/models",
        # Потолок на синтез одного предложения Piper, сек.
        "piper_timeout_sec": 30,

        # Google TTS (движок "gtts").
        "gtts": {
            # Язык и «домен» голоса Google (tld: com, ru, co.uk…).
            "lang": "ru",
            "tld": "com",
            "slow": False,
            # Чем играть полученный MP3: список команд-кандидатов, берётся
            # первая с программой в PATH. gst-play-1.0 (пакет
            # gstreamer1.0-tools) играет MP3 и сам завершается.
            "players": [["gst-play-1.0", "--quiet"], ["mpg123", "-q"],
                        ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]],
        },

        # Команда speech-dispatcher (движок "spd-say"). Текст добавляется
        # последним аргументом. -w — ждать конца фразы.
        "spd_say_command": ["spd-say", "-w", "-l", "ru"],
    },

    # UDP-канал управления воспроизведением (стоп/пауза/повтори/продолжи/
    # тише/громче). Отдельно от очереди tasks/, потому что должен
    # срабатывать мимо очереди. Слушает только 127.0.0.1. Штатный
    # отправитель — модуль system/modules/mod_playback (по команде
    # пользователя через ядро); v2 — стоп-споттер wake.py.
    "control_udp_host": "127.0.0.1",
    "control_udp_port": 47120,

    # Аналитический слой: заявке нельзя доверять класс важности. CNPS
    # определяет его по источнику (`source` в манифесте) и зажимает
    # запрошенный класс по потолку источника. Ключ — значение `source`,
    # значение — максимальный (самый важный) допустимый класс.
    "source_ceiling": {
        "smos": "CRITICAL",
        "updater": "CRITICAL",
        "sysaudio": "CRITICAL",
        "watcher": "ALERT",
        "core": "ALERT",
        "outputstructurizer": "RESULT",
        "chat": "CHAT",
    },
    # Класс по умолчанию, если манифест класс не просит: ключ — `source`,
    # значение — класс. Источник не в списке -> DEFAULT_UNKNOWN_CLASS.
    "source_default_class": {
        "smos": "CRITICAL",
        "updater": "CRITICAL",
        "sysaudio": "ALERT",
        "watcher": "ALERT",
        "core": "RESULT",
        "outputstructurizer": "RESULT",
        "chat": "CHAT",
    },
    # Источники, которым разрешено писать в tasks/ напрямую. Не в списке —
    # заявка не отклоняется, но получает потолок AMBIENT (сыграет, если
    # ничего важнее нет, и первой протухнет). Пустой список — проверку не
    # применять (любой источник по своей таблице).
    "trusted_direct_sources": ["outputstructurizer", "core", "watcher", "chat", "smos", "updater", "sysaudio"],
    # Класс для незнакомого / отсутствующего `source`.
    "unknown_source_class": "AMBIENT",

    # Пять классов важности. ttl_soft_sec — «легко прервать»: прерванная
    # заявка в пределах этого срока (от created_at) сама доигрывает, когда
    # очередь освободится. ttl_hard_sec — «выбросить молча»: после него
    # заявка не играется и не продолжается автоматически (только по
    # явной просьбе — repeat/continue из Recap). voice_interruptible —
    # можно ли оборвать командой управления (stop/pause/skip); CRITICAL —
    # нет, доигрывает. preemptible — может ли заявку вытеснить приход
    # строго более важной (по классу) заявки.
    "classes": {
        "CRITICAL": {"ttl_soft_sec": 3600, "ttl_hard_sec": 86400, "voice_interruptible": False, "preemptible": False},
        "ALERT":    {"ttl_soft_sec": 300,  "ttl_hard_sec": 3600,  "voice_interruptible": True,  "preemptible": True},
        "RESULT":   {"ttl_soft_sec": 60,   "ttl_hard_sec": 900,   "voice_interruptible": True,  "preemptible": True},
        "CHAT":     {"ttl_soft_sec": 30,   "ttl_hard_sec": 300,   "voice_interruptible": True,  "preemptible": True},
        "AMBIENT":  {"ttl_soft_sec": 15,   "ttl_hard_sec": 60,    "voice_interruptible": True,  "preemptible": True},
    },
    # Совместимость: outputstructurizer помечает свои заявки
    # privilege_level (сейчас всегда 2). Если в заявке нет `class`, но
    # есть privilege_level — мапится сюда.
    "privilege_level_class": {"0": "AMBIENT", "1": "CHAT", "2": "RESULT", "3": "ALERT", "4": "CRITICAL"},

    # Сколько последних произнесённых/недоигранных заявок держать в буфере
    # Recap — их можно достать по «повтори»/«продолжи» даже после
    # истечения TTL. Хранятся только метаданные + текст, не аудио.
    "recap_size": 10,

    # Шаг изменения громкости по команде louder/quieter (0..1).
    "volume_step": 0.15,
    # Стартовая громкость (0..1). Применяется только при player = paplay.
    "volume": 1.0,

    "mute": {
        # Глушение микрофона на время воспроизведения (анти-петля «система
        # слышит саму себя», v1.1.1). Раньше ставил system/audio/audio.py,
        # теперь — CNPS. Путь к флагу относительно КОРНЯ проекта (папка с
        # smos.root), чтобы явно указывать в чужую подсистему (rvs).
        # Контракт файла и wake.py в v1 не меняются — см.
        # system/fwl/rvs/wake.py, функция mic_muted.
        "flag_path": "system/fwl/rvs/flags/mute.flag",
        # CNPS пишет флаг в режиме {"until": now + это}. Пока играет —
        # переписывает чаще refresh_interval_sec (пульс). Упал, не сняв
        # флаг — микрофон оглохнет максимум на столько и сам отпустит.
        "until_margin_sec": 3,
        "refresh_interval_sec": 1.0,
    },

    "paths": {
        # Входящие заявки <id>.json. Рантайм-данные, в .gitignore.
        "tasks_dir": "tasks",
        # Куда убирать неразобранные заявки (битый JSON, нет обязательных
        # полей). Подпапка tasks_dir.
        "rejected_subdir": "rejected",
        # Кэш синтезированных предложений <hash>.wav. Рантайм, в .gitignore.
        "cache_dir": "cache",
        # Снимок состояния (что играет, очередь, отложенные, Recap,
        # громкость) — читается без обращения к процессу, как core/audio.
        # Рантайм, в .gitignore.
        "state_file": "state.json",
        # Клипы system/sysaudio для kind:"clip". Относительно папки с
        # cnps.py.
        "sysaudio_clips_dir": "../sysaudio/clips",
    },
    # Язык подпапки sysaudio clips/<lang>/ для kind:"clip".
    "sysaudio_lang": "ru",
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


def project_root(base_dir: Path) -> Path | None:
    """Корень проекта SMOS (папка с smos.root) или None. Нужен местам,
    которые обращаются к файлам вне своей подсистемы — сейчас это флаг
    глушения микрофона в system/fwl/rvs/."""
    return _project_root(base_dir)


def load(base_dir: Path) -> dict:
    """Загружает user/configs/<CONFIG_NAME> и накладывает его поверх
    DEFAULTS. base_dir — папка вызывающего скрипта (SCRIPT_DIR). Наружу
    не бросает исключений: корень не найден, файла нет, битый JSON или не
    JSON-объект — печатает предупреждение и возвращает DEFAULTS, чтобы
    опечатка в конфиге не уронила процесс."""
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
