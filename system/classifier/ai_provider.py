"""
ai_provider.py — обёртка над облачной LLM для классификатора SMOS
«команда / разговор» (bootstrap-этап, см. model_combination_design.md).

Единственная точка в проекте, которая знает, что сейчас используется
именно GigaChat. Если понадобится сменить провайдера — меняется только
этот файл, сигнатура ai_classify(text) наружу остаётся прежней.

Установка: pip install gigachat python-dotenv
Ключ доступа берётся из переменной окружения GIGACHAT_CREDENTIALS —
положите его в файл .env рядом с этим скриптом (GIGACHAT_CREDENTIALS=...),
.env уже в .gitignore, так что ключ не попадёт в git. Остальные настройки
(модель, temperature и т.п.) — в config.json, см. config.py.
"""

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

load_dotenv()
CFG = config.load(SCRIPT_DIR)

SYSTEM_PROMPT = (
    "Ты — классификатор коротких голосовых фраз для голосового "
    "ассистента. Определи, эта фраза — КОМАНДА (пользователь хочет, "
    "чтобы что-то было выполнено, например \"включи свет\", \"поставь "
    "таймер на пять минут\") или ОБЩЕНИЕ (обычный разговор с "
    "ассистентом как с другом — вопросы, реплики, светская беседа). "
    "Ответь строго одним словом, без пояснений и знаков препинания: "
    "command или chat."
)


def ai_classify(text: str) -> str:
    """Отправляет фразу в GigaChat, возвращает 'command' или 'chat'.

    Намеренно без доп. логики (retries, кэш и т.п.) — это MVP-обёртка,
    усложнять раньше времени не нужно (см. model_combination_design.md).
    При неожиданном ответе модели бросает ValueError — лучше сразу
    увидеть, что модель не послушалась промта, чем молча считать это
    каким-то из двух классов по умолчанию.
    """
    payload = Chat(
        messages=[
            Messages(role=MessagesRole.SYSTEM, content=SYSTEM_PROMPT),
            Messages(role=MessagesRole.USER, content=text),
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

    label = response.choices[0].message.content.strip().lower()

    if label not in ("command", "chat"):
        raise ValueError(f"GigaChat вернул неожиданную метку: {label!r}")

    return label
