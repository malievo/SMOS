"""
train_local_model.py — обучение локального классификатора «команда /
разговор» на датасете, накопленном classifier.py (см.
model_combination_design.md).

train() вызывается двумя путями:
- вручную: `python3 train_local_model.py` — удобно для отладки/проверки
  без запуска всего classifier.py;
- автоматически из classifier.py, когда датасет дорастает до нужного
  размера (и для первого обучения, и для дообучения в shadow-режиме).

Читает output/dataset.jsonl, обучает маленький классификатор
(логистическая регрессия) поверх эмбеддингов фраз
(embedding_classifier_design.md), сохраняет в output/local_model.joblib.

Установка: pip install sentence-transformers scikit-learn
"""

import json
import shutil
import sys
from collections import Counter
from pathlib import Path

import joblib
from sentence_transformers import SentenceTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score
from sklearn.model_selection import train_test_split

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402

CFG = config.load(SCRIPT_DIR)

DATASET_FILE = SCRIPT_DIR / CFG["paths"]["output_dir"] / CFG["paths"]["dataset_file"]
MODEL_FILE = SCRIPT_DIR / CFG["paths"]["output_dir"] / CFG["local_model"]["model_file"]
MODEL_BACKUP_FILE = SCRIPT_DIR / CFG["paths"]["output_dir"] / CFG["local_model"]["model_backup_file"]
MIN_EXAMPLES_PER_CLASS = CFG["local_model"]["min_examples_per_class"]


def load_dataset() -> tuple[list[str], list[str]]:
    texts, labels = [], []
    with open(DATASET_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            texts.append(row["text"])
            labels.append(row["predicted_label"])
    return texts, labels


def train() -> bool:
    """Пытается обучить локальную модель на текущем output/dataset.jsonl.

    Возвращает True, если модель обучена и сохранена, False — если
    данных недостаточно (по каждому классу нужно минимум
    MIN_EXAMPLES_PER_CLASS примеров) или файла датасета ещё нет.
    Не бросает исключений на "недостаточно данных" — это ожидаемое,
    штатное состояние на раннем этапе, а не ошибка."""
    if not DATASET_FILE.exists():
        print(f"[train] {DATASET_FILE} не найден — сначала дайте classifier.py поработать и накопить данные.")
        return False

    texts, labels = load_dataset()
    counts = Counter(labels)
    print(f"[train] в датасете {len(texts)} фраз всего: {dict(counts)}")

    for label in ("command", "chat"):
        have = counts.get(label, 0)
        if have < MIN_EXAMPLES_PER_CLASS:
            print(
                f"[train] недостаточно примеров класса '{label}' "
                f"({have} из нужных {MIN_EXAMPLES_PER_CLASS}) — обучение отменено, "
                "нужно больше данных."
            )
            return False

    print(f"[train] загружаю модель эмбеддингов {CFG['local_model']['embedding_model']}...")
    embedder = SentenceTransformer(CFG["local_model"]["embedding_model"])
    embeddings = embedder.encode(texts, show_progress_bar=True)

    # Отдельная отложенная выборка — только для честной оценки точности,
    # в финальную модель ниже пойдут уже все данные.
    X_train, X_test, y_train, y_test = train_test_split(
        embeddings, labels, test_size=0.2, random_state=42, stratify=labels
    )

    eval_clf = LogisticRegression(max_iter=1000)
    eval_clf.fit(X_train, y_train)
    accuracy = accuracy_score(y_test, eval_clf.predict(X_test))
    print(f"[train] точность на отложенной выборке ({len(y_test)} фраз): {accuracy:.2%}")

    final_clf = LogisticRegression(max_iter=1000)
    final_clf.fit(embeddings, labels)

    MODEL_FILE.parent.mkdir(exist_ok=True)

    if MODEL_FILE.exists():
        # Версионирование в один уровень: перед перезаписью сохраняем
        # текущую (работающую) модель как .bak — если новое обучение
        # окажется неудачным, есть куда вручную откатиться (переименовать
        # .bak обратно в MODEL_FILE и позвать local_provider.reload()).
        shutil.copy2(MODEL_FILE, MODEL_BACKUP_FILE)
        print(f"[train] предыдущая версия модели сохранена в {MODEL_BACKUP_FILE}")

    joblib.dump(final_clf, MODEL_FILE)
    print(f"[train] модель сохранена в {MODEL_FILE}")
    return True


if __name__ == "__main__":
    train()
