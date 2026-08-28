"""
phrase_provider.py — обёртка над облачной LLM для outputstructurizer:
по выполненной команде и её результату возвращает короткую фразу для
озвучки.

По задумке — такой же тонкий файл, как system/swl/intent_provider.py и
system/fwl/classifier/ai_provider.py: ЕДИНСТВЕННОЕ место в
outputstructurizer, которое знает, что сейчас используется именно
GigaChat. Сменить онлайн-ИИ (или увести формулировку в chat-ветку
целиком, см. ../audio/IDEA_responder_v2_chat_branch.md) — правится
только этот файл, сигнатура formulate(...) наружу не меняется.

Всё, что НЕ про конкретного провайдера, живёт в других файлах:
- выбор слоя (готовая фраза / формулировщик / сырой результат) — phrasing.py;
- цикл работы, очереди, датасет — outputstructurizer.py;
- таблица готовых фраз — phrases.json.

Позже, когда наберётся датасет (output/dataset.jsonl), тем же приёмом,
что у классификатора и SWL (bootstrap -> shadow -> offline), это
заменится локальной моделью-формулировщиком — отдельная будущая работа.

Установка: pip install gigachat python-dotenv
Ключ доступа — переменная окружения GIGACHAT_CREDENTIALS, в файле
user/.env в корне проекта (GIGACHAT_CREDENTIALS=...). Это общий секрет
облачных провайдеров — тот же файл читают классификатор и SWL, одна
копия на всю систему. user/.env в .gitignore. Путь к нему находит
config.user_env_file().
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

# Один общий секрет на всю систему — user/.env в корне проекта.
_env_file = config.user_env_file(SCRIPT_DIR)
if _env_file:
    load_dotenv(_env_file)

CFG = config.load(SCRIPT_DIR)

SYSTEM_PROMPT = (
    "Ты — генератор коротких устных ответов для голосового ассистента. "
    "Ассистент только что выполнил команду пользователя. Тебе дают: goal "
    "(что это была за команда), source_text (как пользователь её "
    "произнёс), status и либо result (результат работы модуля, JSON), "
    "либо error (текст ошибки, если команда не выполнилась).\n\n"
    "Сформулируй ОДНУ короткую естественную фразу на русском, которую "
    "ассистент скажет вслух.\n"
    "Правила:\n"
    "- Одно-два коротких предложения, разговорно, без канцелярита.\n"
    "- Обязательно передай конкретное содержание результата (числа, "
    "факты). Ничего не добавляй сверх него и не округляй числа.\n"
    "- Не сообщай, что «команда выполнена» — просто скажи сам результат.\n"
    "- Если status == error — коротко и по-человечески скажи, что не "
    "получилось и почему (по тексту error).\n"
    "- Ответь только самой фразой: без Markdown, без кавычек-обёрток, "
    "без префиксов вроде «Ответ:»."
)


def _clean(raw: str) -> str:
    """Убирает обрамляющие кавычки/бэктики, если модель всё-таки их
    добавила, и лишние пробелы. Мусор внутри не чинит — лучше отдать
    как есть, чем гадать."""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'", "`", "«"):
        text = text[1:-1].strip()
    return text


def formulate(goal: str, status: str, result, error, source_text: str | None) -> str:
    """Выполненная команда + её результат -> короткая фраза для озвучки.

    Намеренно без retries/кэша — та же линия, что в ai_provider.py и
    intent_provider.py. Любая проблема (нет ключа, сеть, пустой ответ) —
    исключение наружу; вызывающий код (phrasing.py) ловит его и падает
    на следующий слой fallback, ответ пользователю всё равно уйдёт.
    """
    task = {"goal": goal, "source_text": source_text, "status": status}
    if status == "error":
        task["error"] = error
    else:
        task["result"] = result

    user_content = "Выполненная команда:\n" + json.dumps(task, ensure_ascii=False)

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

    phrase = _clean(response.choices[0].message.content)
    if not phrase:
        raise ValueError("GigaChat вернул пустую фразу")
    return phrase


if __name__ == "__main__":
    # Разовая проверка формулировщика без всего конвейера.
    demo_goal = sys.argv[1] if len(sys.argv) > 1 else "calc_result"
    demo_result = {"expression": "2+2*10", "value": 22}
    print(formulate(demo_goal, "done", demo_result, None, "посчитай два плюс два умножить на десять"))
