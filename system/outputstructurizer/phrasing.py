"""
phrasing.py — выбор формулировки ответа: из выполненной задачи делает
фразу для озвучки.

Три слоя, сверху вниз (см. outputstructurizer_design.md):

  1. Готовая фраза — шаблон из phrases.json по goal-ключу (для ошибок —
     ключ "_error"). Подстановка через str.format. Быстро, оффлайн,
     предсказуемо. Для подтверждений команд («записал», «сейчас 14:30»)
     этого достаточно.
  2. Формулировщик — GigaChat через phrase_provider.formulate(). Когда
     готового шаблона нет: системным промтом ему сообщается, что за
     команда выполнена и каков результат модуля, он возвращает короткую
     естественную фразу. Отдельный сменный файл — провайдера легко
     заменить.
  3. Сырой fallback — шаблон result_fallback_template / error_fallback_template
     из конфига. Срабатывает, только если формулировщик недоступен (нет
     ключа, сеть, битый ответ). Гарантирует, что ответ пользователю
     уйдёт всегда, даже без интернета.

Это НЕ демон — набор чистых функций, которые вызывает outputstructurizer.py
на каждый файл-результат из очереди.
"""

import random
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from log_client import send_log  # noqa: E402

ERROR_TEMPLATE_KEY = "_error"


def _pick_template(phrases: dict, goal: str | None, status: str, random_variants: bool):
    """Достаёт шаблон под задачу из phrases.json. Для status == "error" —
    ключ "_error", иначе — goal-ключ. Значение может быть строкой или
    списком строк (тогда берётся случайный вариант, если включено).
    None — если подходящего шаблона нет."""
    key = ERROR_TEMPLATE_KEY if status == "error" else goal
    if not key:
        return None
    value = phrases.get(key)
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return random.choice(value) if random_variants else value[0]
    return None


def _namespace(record: dict) -> dict:
    """Имена, доступные в шаблоне phrases.json: result — значение цели,
    state — всё накопленное состояние задачи, source_text — исходная
    фраза, goal, error."""
    return {
        "result": record.get("result"),
        "state": record.get("state") or {},
        "source_text": record.get("source_text"),
        "goal": record.get("goal"),
        "error": record.get("error"),
    }


def render(record: dict, phrases: dict, cfg: dict) -> tuple[str, str]:
    """Из файла-результата задачи (task_id, goal, status, result, error,
    state, source_text, ts) делает (текст, слой), где слой —
    "template" | "llm" | "fallback". Не бросает исключений: любой сбой
    слоя роняет обработку на следующий, последний слой всегда что-то
    возвращает."""
    goal = record.get("goal")
    status = record.get("status")
    result = record.get("result")
    error = record.get("error")
    source_text = record.get("source_text")
    phrasing_cfg = cfg["phrasing"]

    # --- Слой 1: готовая фраза ---
    template = _pick_template(phrases, goal, status, phrasing_cfg["random_variants"])
    if template is not None:
        try:
            return template.format(**_namespace(record)), "template"
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as e:
            send_log("WARNING", "template_render_failed",
                     {"goal": goal, "template": template, "error": str(e)})

    # --- Слой 2: формулировщик (GigaChat) ---
    try:
        import phrase_provider  # ленивый импорт: без gigachat/ключа слои 1 и 3 всё равно работают
        text = phrase_provider.formulate(goal, status, result, error, source_text)
        return text, "llm"
    except Exception as e:  # noqa: BLE001 — любой сбой провайдера роняем на слой 3
        send_log("WARNING", "formulator_unavailable", {"goal": goal, "error": str(e)})

    # --- Слой 3: сырой fallback ---
    if status == "error":
        text = phrasing_cfg["error_fallback_template"].format(error=error)
    else:
        text = phrasing_cfg["result_fallback_template"].format(result=result)
    return text, "fallback"


if __name__ == "__main__":
    # Прогон всех трёх слоёв на модельных записях, без очередей и ядра.
    import config

    CFG = config.load(SCRIPT_DIR)
    import json

    phrases_path = SCRIPT_DIR / CFG["paths"]["phrases_file"]
    PHRASES = json.loads(phrases_path.read_text(encoding="utf-8")) if phrases_path.exists() else {}

    samples = [
        {"task_id": "t1", "goal": "calc_result", "status": "done",
         "result": {"expression": "2+2*10", "value": 22}, "error": None,
         "state": {}, "source_text": "посчитай два плюс два умножить на десять"},
        {"task_id": "t2", "goal": "current_datetime", "status": "done",
         "result": {"time": "14:30:05", "weekday": "пятница"}, "error": None,
         "state": {}, "source_text": "который час"},
        {"task_id": "t3", "goal": "note_saved", "status": "done",
         "result": {"saved": True}, "error": None, "state": {}, "source_text": "запиши заметку"},
        {"task_id": "t4", "goal": "weather_forecast", "status": "done",
         "result": {"temp_c": 21, "sky": "ясно"}, "error": None,
         "state": {"city": "Москва"}, "source_text": "какая погода"},
        {"task_id": "t5", "goal": "calc_result", "status": "error",
         "result": None, "error": "деление на ноль", "state": {}, "source_text": "сколько будет один делить на ноль"},
    ]
    for s in samples:
        text, source = render(s, PHRASES, CFG)
        print(f"[{source:8}] {s['goal']:18} -> {text}")
