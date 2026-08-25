"""
local_provider.py — локальный классификатор «команда / разговор»,
обученный на данных пользователя (см. train_local_model.py,
model_combination_design.md).

Работает офлайн, без обращения к облаку. Симметричен ai_provider.py по
форме (одна функция text -> метка), но это отдельный, самостоятельный
файл — они не должны знать друг о друге; сравнивать их будет
classifier.py, когда дойдём до shadow-режима (шаг 3b).

Пока модель не обучена — используется is_available(), чтобы вызывающий
код мог понять это заранее, не ловя исключение.

Установка: pip install sentence-transformers scikit-learn
"""

import sys
from pathlib import Path

import joblib
from sentence_transformers import SentenceTransformer

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402

CFG = config.load(SCRIPT_DIR)

MODEL_FILE = SCRIPT_DIR / CFG["paths"]["output_dir"] / CFG["local_model"]["model_file"]

_embedder = None
_classifier = None


def _ensure_loaded() -> None:
    """Модели грузятся один раз, при первом вызове local_classify — не
    при импорте модуля, потому что на момент импорта обученной модели
    может ещё не существовать."""
    global _embedder, _classifier

    if _classifier is None:
        if not MODEL_FILE.exists():
            raise FileNotFoundError(
                f"Локальная модель не найдена ({MODEL_FILE}) — сначала запустите train_local_model.py"
            )
        _classifier = joblib.load(MODEL_FILE)

    if _embedder is None:
        _embedder = SentenceTransformer(CFG["local_model"]["embedding_model"])


def reload() -> None:
    """Сбрасывает закэшированную модель — следующий вызов local_classify
    перечитает её заново с диска. Нужно звать после того, как
    train_local_model.train() перезаписал model_file поверх уже
    загруженной в память версии, иначе classifier.py продолжит работать
    со старой моделью до перезапуска процесса."""
    global _classifier
    _classifier = None


def is_available() -> bool:
    """True, если обученная модель уже есть на диске. Проверять этим
    перед вызовом local_classify, а не ловить исключение — модель может
    просто ещё не быть обучена, это не ошибка, а ожидаемое состояние
    на раннем этапе (см. model_combination_design.md)."""
    return MODEL_FILE.exists()


def local_classify(text: str) -> str:
    """Возвращает 'command' или 'chat', используя локальную модель.

    Бросает FileNotFoundError, если модель ещё не обучена — вызывающий
    код должен сам проверить is_available() заранее."""
    _ensure_loaded()
    embedding = _embedder.encode([text])
    return _classifier.predict(embedding)[0]
