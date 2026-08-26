"""
intent_provider.py — обёртка над облачной LLM для SWL: по фразе и
каталогу целей возвращает (goal, params).

По задумке — такой же тонкий файл, как system/fwl/classifier/ai_provider.py:
ЕДИНСТВЕННОЕ место в SWL, которое знает, что сейчас используется именно
GigaChat. Сменить онлайн-ИИ — правится только этот файл, сигнатура
extract(text, catalog) наружу не меняется.

Всё, что НЕ про конкретного провайдера, живёт в других файлах:
- каталог целей (что система вообще умеет) — catalog.py;
- цикл работы, запись цели, датасет — swl.py.

Позже, когда наберётся датасет, тем же приёмом, что у классификатора
(bootstrap -> shadow -> offline), это заменится локальной
моделью-разборщиком — но это отдельная будущая работа, и она тоже будет
брать каталог из catalog.py, а не отсюда.

Установка: pip install gigachat python-dotenv
Ключ доступа — переменная окружения GIGACHAT_CREDENTIALS, в файле .env
рядом с этим скриптом (GIGACHAT_CREDENTIALS=...). .env уже в .gitignore.
Если своего .env у SWL нет — как запасной вариант подхватывается .env
классификатора (тот же ключ, тот же GigaChat), чтобы не держать две
копии секрета.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from gigachat import GigaChat
from gigachat.models import Chat, Messages, MessagesRole

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402

# .env: сначала свой, потом — как запасной — у классификатора (тот же ключ).
load_dotenv(SCRIPT_DIR / ".env")
if not os.environ.get("GIGACHAT_CREDENTIALS"):
    load_dotenv(SCRIPT_DIR.parent / "fwl" / "classifier" / ".env")

CFG = config.load(SCRIPT_DIR)

SYSTEM_PROMPT = (
    "Ты — разборщик коротких голосовых команд для голосового ассистента. "
    "Тебе дают список ЦЕЛЕЙ, которые ассистент умеет достигать (JSON-массив; "
    "у каждой: goal — ключ цели, description — что это, needs — какие "
    "параметры для неё бывают нужны), и фразу пользователя.\n"
    "Определи, какую ОДНУ цель из списка просит достичь пользователь, и "
    "вытащи из фразы значения параметров (в первую очередь из needs, но "
    "можно и другие, если они явно названы в фразе; чего в фразе нет — не "
    "придумывай, просто не клади в params).\n"
    "Ответь СТРОГО одним JSON-объектом, без markdown и без пояснений:\n"
    '{"goal": "<ключ из списка>", "params": {"<имя>": <значение>, ...}}\n'
    'Если под фразу не подходит ни одна цель из списка — верни {"goal": null, "params": {}}.'
)


def _parse_llm_json(raw: str) -> dict:
    """Разбирает ответ модели в dict. Терпит обёртку ```json ... ``` на
    случай, если модель всё-таки её добавила, но не пытается чинить
    произвольный мусор — как и у классификатора, лучше сразу увидеть
    ValueError, что модель не послушалась формата, чем молча гадать."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"GigaChat вернул не-JSON: {raw!r} ({e})") from e
    if not isinstance(obj, dict):
        raise ValueError(f"GigaChat вернул не JSON-объект: {raw!r}")
    return obj


def extract(text: str, catalog: list[dict]) -> tuple[str | None, dict]:
    """Фраза + каталог целей -> (goal, params).

    goal — ключ из каталога, либо None, если LLM решила, что ни одна
    известная цель не подходит (что делать в этом случае — решает
    вызывающий код, см. swl.py). params — то, что удалось вытащить из
    фразы; может быть пустым (тогда недостающее ядро добудет само через
    achieve()).

    catalog обязателен и приходит СНАРУЖИ (из catalog.build()) — этот
    файл не знает и не должен знать, откуда берутся цели; только как
    спросить про них у GigaChat и как разобрать ответ. Проверка "goal
    из каталога" здесь — это проверка "модель ответила допустимым
    значением", ровно как ai_classify проверяет label in (command, chat).

    Намеренно без retries/кэша — MVP-обёртка, та же линия, что в
    ai_provider.py. Неожиданный ответ модели -> ValueError."""
    user_content = (
        "Список целей:\n"
        + json.dumps(catalog, ensure_ascii=False)
        + f"\n\nФраза пользователя: {text}"
    )

    payload = Chat(
        messages=[
            Messages(role=MessagesRole.SYSTEM, content=SYSTEM_PROMPT),
            Messages(role=MessagesRole.USER, content=user_content),
        ],
        temperature=CFG["ai"]["temperature"],
        max_tokens=CFG["ai"]["max_tokens"],
    )

    with GigaChat(
        credentials=os.environ["GIGACHAT_CREDENTIALS"],
        model=CFG["ai"]["model"],
        verify_ssl_certs=CFG["ai"]["verify_ssl_certs"],
    ) as giga:
        response = giga.chat(payload)

    obj = _parse_llm_json(response.choices[0].message.content)

    if "goal" not in obj:
        raise ValueError(f"в ответе GigaChat нет ключа 'goal': {obj!r}")
    goal = obj["goal"]

    known_goals = {c["goal"] for c in catalog}
    if goal is not None and goal not in known_goals:
        raise ValueError(f"GigaChat вернул цель не из каталога: {goal!r}")

    params = obj.get("params") or {}
    if not isinstance(params, dict):
        raise ValueError(f"'params' в ответе GigaChat — не объект: {params!r}")

    return goal, params
