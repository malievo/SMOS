"""
pipeline.py — «схема» сквозного пути одной реплики: список стадий от
микрофона до озвученного ответа и то, какими лог-событиями каждая
стадия отмечается.

Это единственное место, где зашита форма воронки. Меняется цепочка или
имена событий — правится здесь, отчёты (analytics.py) подстраиваются
сами. Ничего не импортирует из system/ — только описание.

Одна реплика = один trace_id (см. logs/PROTOCOL.md). analytics.py
группирует события по trace_id и для каждой реплики смотрит, до какой
стадии она дошла и на чём остановилась.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Stage:
    key: str                                  # короткое имя стадии
    title: str                                # для человека
    reached: list[tuple[str, str]]            # (module, message) — «реплика дошла сюда»
    failed: list[tuple[str, str, str]] = field(default_factory=list)  # (module, message, причина)
    note: str = ""


# Порядок = порядок прохождения. Индекс в этом списке = «как далеко дошла реплика».
STAGES: list[Stage] = [
    Stage(
        "heard", "услышана",
        reached=[
            ("rvs", "wake_word_detected"),
            ("rvs", "continuation_recording_started"),
            ("rvs", "nowake_recording_started"),
        ],
        note="сработал активатор / продолжение диалога / режим --nowake",
    ),
    Stage(
        "recorded", "записана",
        reached=[("rvs", "utterance_saved")],
        failed=[("rvs", "utterance_discarded_muted", "запись отброшена под mute.flag")],
    ),
    Stage(
        "stt", "распознана",
        reached=[("rvs", "speech_recognized")],
        failed=[
            ("rvs", "speech_not_recognized", "STT не расслышал"),
            ("rvs", "stt_request_error", "ошибка запроса к STT (сеть?)"),
        ],
    ),
    Stage(
        "classified", "классифицирована",
        reached=[("classifier", "phrase_classified")],
        failed=[
            ("classifier", "classification_failed", "ошибка классификатора (LLM)"),
            ("classifier", "local_classification_failed", "ошибка локальной модели классификатора"),
        ],
        note="label в data: command | chat. chat — легитимный выход из воронки команд",
    ),
    Stage(
        "intent", "разобрана в цель",
        reached=[("swl", "intent_extracted")],
        failed=[
            ("swl", "no_intent_match", "SWL: подходящей цели нет (пробел в возможностях)"),
            ("swl", "skipped_non_command", "классифицировано как chat — SWL не взял"),
            ("swl", "intent_extraction_failed", "ошибка разбора фразы (GigaChat)"),
        ],
    ),
    Stage(
        "dispatched", "принята ядром",
        reached=[("core", "goal_dispatched")],
        failed=[("core", "goal_rejected", "ядро отклонило цель")],
    ),
    Stage(
        "executed", "выполнена",
        reached=[("core", "task_done")],
        failed=[
            ("core", "task_failed", "планировщик/модуль вернул ошибку"),
            ("planner", "module_timeout", "модуль не ответил за таймаут"),
            ("planner", "module_crashed", "модуль упал"),
        ],
    ),
    Stage(
        "phrased", "сформулирован ответ",
        reached=[("outputstructurizer", "phrase_emitted")],
        note="completion_silent (core) — НЕ провал: ответ намеренно без озвучки (mod_playback и т.п.)",
    ),
    Stage(
        "spoken", "озвучен",
        reached=[("cnps", "delivery")],
        note="outcome в data: DELIVERED (полностью) | PARTIAL (частично) | NEVER (не прозвучал)",
    ),
]

STAGE_KEYS = [s.key for s in STAGES]
STAGE_INDEX = {s.key: i for i, s in enumerate(STAGES)}

# Промежутки между стадиями, для разбора задержки (человекочитаемые пары).
GAPS = [(STAGE_KEYS[i], STAGE_KEYS[i + 1]) for i in range(len(STAGE_KEYS) - 1)]
GAP_KEYS = [f"{a}->{b}" for a, b in GAPS] + ["heard->spoken"]


def reached_lookup() -> dict[tuple[str, str], int]:
    """(module, message) -> индекс стадии, которую это событие отмечает
    как достигнутую."""
    out: dict[tuple[str, str], int] = {}
    for i, st in enumerate(STAGES):
        for pair in st.reached:
            out[pair] = i
    return out


def failed_lookup() -> dict[tuple[str, str], tuple[int, str]]:
    """(module, message) -> (индекс стадии, где реплика застряла;
    человекочитаемая причина)."""
    out: dict[tuple[str, str], tuple[int, str]] = {}
    for i, st in enumerate(STAGES):
        for mod, msg, reason in st.failed:
            out[(mod, msg)] = (i, reason)
    return out
