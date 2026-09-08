"""
config.py — загрузка настроек аналитического слоя (logs/analytics).

Та же схема, что у logs/listener/config.py и остальных config.py в
проекте: настройки лежат в user/configs/analytics.json (рядом с
файлом-маркером smos.root). Меняешь значение — перезапускаешь
analytics.py watch (у разовых отчётов перечитывается при каждом
запуске). Файла нет / корень не найден / JSON битый — работаем на
DEFAULTS и печатаем предупреждение. Частично заполненный файл
дополняется из DEFAULTS (рекурсивное слияние).
"""

import copy
import json
from pathlib import Path

CONFIG_NAME = "analytics.json"
ROOT_MARKER = "smos.root"

DEFAULTS = {
    "window": {
        # За какой период считать отчёт по умолчанию (analytics.py report
        # без --since / --last).
        "default_hours": 24,
    },
    "latency": {
        # Пороги «медленно» для межстадийных промежутков (секунды). Разрешение
        # ts в логах — 1 секунда (listener пишет timespec="seconds"), поэтому
        # промежутки меньше секунды видны как 0 — пороги имеют смысл только для
        # ощутимо долгих участков (STT, GigaChat у SWL, синтез+озвучка).
        "slow_gap_sec": {
            "recorded->stt": 6.0,
            "classified->intent": 6.0,
            "dispatched->executed": 5.0,
            "phrased->spoken": 10.0,
            "heard->phrased": 8.0,   # выполнение без озвучки
            "heard->spoken": 20.0,
        },
    },
    "anomaly": {
        # task_started без task_done/task_failed дольше этого — «завис».
        "hang_timeout_sec": 30,
        # Больше restart_flap_count запусков системы за это окно — «хлопанье».
        "restart_flap_window_sec": 300,
        "restart_flap_count": 3,
        # ERROR+CRITICAL в час на модуль выше этого — «всплеск ошибок».
        "error_spike_per_hour": 10,
        # Доля неудачных распознаваний (speech_not_recognized + stt_request_error)
        # от всех попыток STT выше этого — предупреждение (сеть/микрофон).
        "stt_failure_rate_warn": 0.4,
        # mute.flag старше стольких секунд, но всё ещё «активный» по формату —
        # возможный дедлок (CNPS упал в середине озвучки, wake оглох навсегда).
        "mute_flag_stuck_sec": 20,
    },
    "watch": {
        # Как часто watch-режим перечитывает свежие события.
        "poll_interval_sec": 60,
        # Ширина скользящего окна, по которому watch считает аномалии и
        # собирает открытые трейсы (должна с запасом перекрывать самую
        # долгую реплику).
        "report_window_min": 15,
        # Слать найденные аномалии обратно в шину логов (module="analytics").
        "emit_anomaly_events": True,
        # Периодически слать компактную сводку окна тем же каналом.
        "emit_summary_events": True,
    },
    "store": {
        # Постоянное хранилище истории по КАЖДОЙ команде. watch собирает
        # события одной реплики (по trace_id) и пишет их одним файлом
        # logs/analytics/traces/<дата>/<trace_id>.json — потом видно, через
        # что прошла команда, без пересканирования всех raw-логов. Плюс
        # строка на команду в traces/index.jsonl (быстрый список истории).
        "enabled": True,
        "dir": "traces",                 # относительно папки analytics/
        "index_file": "index.jsonl",
        # Реплика без новых событий дольше этого — считается закрытой
        # (дописывается строка в index; файл дальше не трогается, если не
        # прилетит опоздавшее событие).
        "idle_close_sec": 90,
        # Папки-дни старше этого возраста watch подчищает при запуске.
        # index.jsonl не трогается (одна строка на команду — растёт медленно).
        "keep_days": 30,
    },
    "paths": {
        # Папка с сырыми событиями, относительно logs/ (на уровень выше
        # папки analytics/). Та же, куда пишет listener.
        "raw_dir": "raw",
        # mute.flag относительно корня проекта — analytics его стат'ит
        # для проверки «залип ли» (см. anomaly.mute_flag_stuck_sec).
        "mute_flag_from_root": "system/fwl/rvs/flags/mute.flag",
        # Рабочее состояние watch (какие трейсы уже занесены в index),
        # относительно папки analytics/. В .gitignore.
        "state_file": "watch_state.json",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _project_root(start: Path) -> Path | None:
    start = Path(start).resolve()
    for folder in (start, *start.parents):
        if (folder / ROOT_MARKER).exists():
            return folder
    return None


def load(base_dir: Path) -> dict:
    """Загружает user/configs/analytics.json поверх DEFAULTS. Не бросает
    исключений: корень не найден / нет файла / битый JSON — предупреждение
    и DEFAULTS."""
    root = _project_root(base_dir)
    if root is None:
        print(f"[config] не найден корень проекта (файл {ROOT_MARKER}) — использую значения по умолчанию.")
        return copy.deepcopy(DEFAULTS)

    config_file = root / "user" / "configs" / CONFIG_NAME
    if not config_file.exists():
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


def project_root(base_dir: Path) -> Path | None:
    """Публичный доступ к поиску корня — analytics.py резолвит по нему
    путь к mute.flag."""
    return _project_root(base_dir)
