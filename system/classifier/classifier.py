"""
classifier.py — основной скрипт классификатора «команда / разговор» SMOS.

Следит за output/recognized.json подсистемы rvs (см. system/rvs/req.py)
и на каждую новую распознанную фразу определяет метку. Реализует полный
жизненный цикл из model_combination_design.md:

- BOOTSTRAP (локальной модели ещё нет): метку даёт только онлайн-ИИ
  (ai_provider.py), каждая фраза копится в output/dataset.jsonl.
- SHADOW (локальная модель обучена, но ещё не основная): онлайн-ИИ
  по-прежнему даёт РЕАЛЬНЫЙ ответ, локальная модель считается
  параллельно только для сравнения. Датасет продолжает расти, и когда
  накопится достаточно новых примеров — модель дообучается сама.
  Когда согласие моделей стабильно высокое — модель повышается
  (flags/promoted.flag).
- VALIDATING (flags/promoted.flag существует, flags/offline_only.flag —
  ещё нет): реальный ответ уже даёт локальная модель, но ограниченное
  число раз (shadow.post_promotion_validation_comparisons) онлайн-ИИ
  ещё дополнительно спрашивается параллельно — только для проверки, не
  подменяя ответ. Если согласие держится — переходим в OFFLINE. Если
  падает — откат обратно в SHADOW (снимается promoted.flag).
- OFFLINE (flags/offline_only.flag существует): финальное состояние,
  онлайн-ИИ больше никогда не вызывается — работает полностью офлайн и
  бесплатно, без зависимости от общего/платного ключа GigaChat.

Скрипт сам находит свою папку, не зависит от того, откуда его запустили.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))  # чтобы гарантированно найти соседние модули

import config  # noqa: E402
import local_provider  # noqa: E402
import train_local_model  # noqa: E402
from ai_provider import ai_classify  # noqa: E402
from local_provider import local_classify  # noqa: E402
from log_client import send_log  # noqa: E402

CFG = config.load(SCRIPT_DIR)

# --- Пути ---
RECOGNIZED_FILE = SCRIPT_DIR / CFG["paths"]["rvs_output_dir"] / CFG["paths"]["rvs_output_file"]
OUTPUT_DIR = SCRIPT_DIR / CFG["paths"]["output_dir"]
OUTPUT_FILE = OUTPUT_DIR / CFG["paths"]["output_file"]
DATASET_FILE = OUTPUT_DIR / CFG["paths"]["dataset_file"]
SHADOW_STATE_FILE = OUTPUT_DIR / CFG["paths"]["shadow_state_file"]
FLAGS_DIR = SCRIPT_DIR / CFG["paths"]["flags_dir"]
PROMOTED_FLAG = FLAGS_DIR / CFG["paths"]["promoted_flag"]
OFFLINE_ONLY_FLAG = FLAGS_DIR / CFG["paths"]["offline_only_flag"]

CHECK_INTERVAL_SEC = CFG["check_interval_sec"]
RETRAIN_BATCH_SIZE = CFG["shadow"]["retrain_batch_size"]
MIN_COMPARISONS_FOR_PROMOTION = CFG["shadow"]["min_comparisons_for_promotion"]
AGREEMENT_THRESHOLD = CFG["shadow"]["agreement_threshold"]
POST_PROMOTION_VALIDATION_COMPARISONS = CFG["shadow"]["post_promotion_validation_comparisons"]

DEFAULT_SHADOW_STATE = {
    "examples_at_last_training": 0,
    "comparisons_since_retrain": 0,
    "agreements_since_retrain": 0,
}


# --- Вспомогательные функции: файлы, режимы ---

def load_recognized() -> dict:
    return json.loads(RECOGNIZED_FILE.read_text(encoding="utf-8"))


def save_result(text: str, label: str) -> None:
    """Пишет classified.json атомарно (temp-файл + rename) — тот же
    приём, что и в wake.py для utterance.wav."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    payload = {
        "text": text,
        "label": label,
        "timestamp": datetime.now().astimezone().isoformat(),
    }
    tmp_file = OUTPUT_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(OUTPUT_FILE)


def append_to_dataset(text: str, label: str) -> None:
    """Дописывает одну строку в накопительный датасет (JSONL). Растёт и
    в bootstrap-, и в shadow-режиме — везде, где ещё вызывается
    онлайн-ИИ и есть чем размечать."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    row = {"text": text, "predicted_label": label, "source": "llm_api"}
    with open(DATASET_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def dataset_size() -> int:
    if not DATASET_FILE.exists():
        return 0
    with open(DATASET_FILE, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def load_shadow_state() -> dict:
    if not SHADOW_STATE_FILE.exists():
        return dict(DEFAULT_SHADOW_STATE)
    try:
        return {**DEFAULT_SHADOW_STATE, **json.loads(SHADOW_STATE_FILE.read_text(encoding="utf-8"))}
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_SHADOW_STATE)


def save_shadow_state(state: dict) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    SHADOW_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def is_promoted() -> bool:
    return PROMOTED_FLAG.exists()


def is_offline_only() -> bool:
    return OFFLINE_ONLY_FLAG.exists()


def reset_comparison_counters(state: dict) -> dict:
    state["comparisons_since_retrain"] = 0
    state["agreements_since_retrain"] = 0
    return state


def promote(state: dict) -> dict:
    """Локальная модель становится основной. Счётчики сравнений
    обнуляются — с этого момента они считают уже ПРОВЕРОЧНОЕ окно
    (см. post_promotion_validation_comparisons), а не старую shadow-
    статистику."""
    FLAGS_DIR.mkdir(exist_ok=True)
    PROMOTED_FLAG.touch()
    state = reset_comparison_counters(state)
    save_shadow_state(state)
    return state


def confirm_offline_only() -> None:
    """Проверочное окно после переключения пройдено успешно — онлайн-ИИ
    больше никогда не понадобится. Обратного пути из этого состояния
    нет (см. docstring файла) — это финал."""
    FLAGS_DIR.mkdir(exist_ok=True)
    OFFLINE_ONLY_FLAG.touch()


def demote(state: dict) -> dict:
    """Согласие в проверочном окне после переключения упало ниже порога
    — откат обратно в SHADOW: снимаем promoted.flag, обнуляем счётчики,
    дальше снова копим данные и дообучаемся как раньше."""
    if PROMOTED_FLAG.exists():
        PROMOTED_FLAG.unlink()
    state = reset_comparison_counters(state)
    save_shadow_state(state)
    return state


# --- Обучение/дообучение локальной модели ---

def try_train_and_reload(state: dict) -> dict:
    """Пробует обучить/дообучить локальную модель. При успехе — просит
    local_provider перечитать её с диска и сбрасывает счётчики
    shadow-сравнений (старые сравнения относились к прошлой версии
    модели, для новой они не показательны)."""
    trained = train_local_model.train()
    if trained:
        local_provider.reload()
        state["examples_at_last_training"] = dataset_size()
        state["comparisons_since_retrain"] = 0
        state["agreements_since_retrain"] = 0
        save_shadow_state(state)
        send_log("INFO", "local_model_trained", {"dataset_size": state["examples_at_last_training"]})
        print(f"[classifier] локальная модель обучена/дообучена ({state['examples_at_last_training']} фраз в датасете)")
    return state


# --- Обработка одной новой фразы ---

def handle_phrase(text: str, state: dict) -> dict:
    if is_offline_only():
        # OFFLINE: финал. Только локальная модель, онлайн-ИИ не трогаем никогда.
        try:
            label = local_classify(text)
        except Exception as e:
            send_log("ERROR", "local_classification_failed", {"text": text, "error": str(e)})
            print(f"[classifier] ошибка локальной классификации: {e}")
            return state

        save_result(text, label)
        send_log("INFO", "phrase_classified", {"text": text, "label": label, "source": "local"})
        print(f"[classifier] (офлайн) {label}: {text}")
        return state

    if is_promoted():
        return handle_validation_phrase(text, state)

    # BOOTSTRAP / SHADOW: реальный ответ всегда даёт онлайн-ИИ.
    try:
        teacher_label = ai_classify(text)
    except Exception as e:
        send_log("ERROR", "classification_failed", {"text": text, "error": str(e)})
        print(f"[classifier] ошибка классификации: {e}")
        return state

    save_result(text, teacher_label)
    append_to_dataset(text, teacher_label)
    send_log("INFO", "phrase_classified", {"text": text, "label": teacher_label, "source": "llm_api"})
    print(f"[classifier] {teacher_label}: {text}")

    if local_provider.is_available():
        # SHADOW: параллельно спрашиваем локальную модель — только для сравнения.
        try:
            challenger_label = local_classify(text)
        except Exception as e:
            send_log("ERROR", "shadow_classification_failed", {"text": text, "error": str(e)})
            print(f"[classifier] ошибка теневой классификации: {e}")
            return state

        agree = teacher_label == challenger_label
        state["comparisons_since_retrain"] += 1
        if agree:
            state["agreements_since_retrain"] += 1
        save_shadow_state(state)

        send_log(
            "INFO",
            "shadow_comparison",
            {"text": text, "teacher": teacher_label, "challenger": challenger_label, "agree": agree},
        )
        print(f"[classifier] (shadow) локальная модель сказала {challenger_label}, {'совпало' if agree else 'РАСХОЖДЕНИЕ'}")

    # Проверяем, не пора ли (пере)обучиться.
    new_examples = dataset_size() - state["examples_at_last_training"]
    if not local_provider.is_available() or new_examples >= RETRAIN_BATCH_SIZE:
        state = try_train_and_reload(state)
        return state  # счётчики сравнений только что сбросились — проверять повышение рано

    # Проверяем, не пора ли повысить локальную модель до основной.
    if local_provider.is_available() and state["comparisons_since_retrain"] >= MIN_COMPARISONS_FOR_PROMOTION:
        agreement_rate = state["agreements_since_retrain"] / state["comparisons_since_retrain"]
        if agreement_rate >= AGREEMENT_THRESHOLD:
            comparisons = state["comparisons_since_retrain"]
            state = promote(state)
            send_log("INFO", "local_model_promoted", {"agreement_rate": agreement_rate, "comparisons": comparisons})
            print(f"[classifier] === ЛОКАЛЬНАЯ МОДЕЛЬ ПОВЫШЕНА (согласие {agreement_rate:.1%}) — начинаю проверочное окно ===")

    return state


def handle_validation_phrase(text: str, state: dict) -> dict:
    """VALIDATING: реальный ответ уже даёт локальная модель. Онлайн-ИИ
    параллельно ещё спрашивается — ограниченное число раз
    (POST_PROMOTION_VALIDATION_COMPARISONS) — только для проверки,
    результат никак не влияет на save_result. По итогам окна: либо
    насовсем уходим в OFFLINE, либо откатываемся в SHADOW."""
    try:
        label = local_classify(text)
    except Exception as e:
        send_log("ERROR", "local_classification_failed", {"text": text, "error": str(e)})
        print(f"[classifier] ошибка локальной классификации: {e}")
        return state

    save_result(text, label)
    send_log("INFO", "phrase_classified", {"text": text, "label": label, "source": "local"})
    print(f"[classifier] (проверка) {label}: {text}")

    try:
        teacher_label = ai_classify(text)
    except Exception as e:
        # Проверка не смогла состояться в этот раз — не страшно, просто
        # пропускаем её, ответ пользователю уже ушёл от локальной модели.
        send_log("ERROR", "validation_check_failed", {"text": text, "error": str(e)})
        print(f"[classifier] проверочный запрос к онлайн-ИИ не удался: {e}")
        return state

    agree = label == teacher_label
    state["comparisons_since_retrain"] += 1
    if agree:
        state["agreements_since_retrain"] += 1
    append_to_dataset(text, teacher_label)  # пригодится, если придётся откатиться и дообучаться дальше
    save_shadow_state(state)

    send_log(
        "INFO",
        "validation_comparison",
        {"text": text, "local": label, "teacher": teacher_label, "agree": agree},
    )
    print(f"[classifier] (проверка) онлайн-ИИ сказал {teacher_label}, {'совпало' if agree else 'РАСХОЖДЕНИЕ'}")

    if state["comparisons_since_retrain"] >= POST_PROMOTION_VALIDATION_COMPARISONS:
        agreement_rate = state["agreements_since_retrain"] / state["comparisons_since_retrain"]
        if agreement_rate >= AGREEMENT_THRESHOLD:
            confirm_offline_only()
            send_log("INFO", "offline_only_confirmed", {"agreement_rate": agreement_rate})
            print(f"[classifier] === ПРОВЕРКА ПРОЙДЕНА (согласие {agreement_rate:.1%}) — онлайн-ИИ больше не понадобится ===")
        else:
            state = demote(state)
            send_log("WARNING", "local_model_demoted", {"agreement_rate": agreement_rate})
            print(f"[classifier] === СОГЛАСИЕ УПАЛО ({agreement_rate:.1%}) — откат в SHADOW-режим ===")

    return state


def main() -> None:
    send_log("INFO", "classifier_started")
    print(f"[classifier] слежу за {RECOGNIZED_FILE}")

    if is_offline_only():
        print("[classifier] режим: OFFLINE (только локальная модель, онлайн-ИИ больше не используется)")
    elif is_promoted():
        print("[classifier] режим: VALIDATING (локальная модель основная, проверяю её ограниченное время)")
    elif local_provider.is_available():
        print("[classifier] режим: SHADOW (сравниваю онлайн-ИИ и локальную модель)")
    else:
        print("[classifier] режим: BOOTSTRAP (только онлайн-ИИ)")

    state = load_shadow_state()
    # При старте не пере-обрабатываем то, что уже лежит в recognized.json
    # (иначе каждый перезапуск classifier.py дублирует последнюю фразу в
    # датасете) — реагируем только на изменения ПОСЛЕ запуска.
    last_mtime = RECOGNIZED_FILE.stat().st_mtime if RECOGNIZED_FILE.exists() else None

    while True:
        if RECOGNIZED_FILE.exists():
            mtime = RECOGNIZED_FILE.stat().st_mtime

            if mtime != last_mtime:
                last_mtime = mtime

                try:
                    recognized = load_recognized()
                except (json.JSONDecodeError, OSError):
                    # req.py пишет recognized.json не атомарно — есть шанс
                    # поймать файл в момент записи. Пробуем ещё раз на
                    # следующем цикле, а не падаем.
                    time.sleep(CHECK_INTERVAL_SEC)
                    continue

                text = recognized.get("text", "").strip()
                if text:
                    state = handle_phrase(text, state)

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        send_log("INFO", "classifier_stopped")
        print("\n[classifier] остановлен")
