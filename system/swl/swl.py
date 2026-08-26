"""
swl.py — SWL (Second Working Loop): распознанная команда -> структурированная цель.

Место в системе (см. swl_design.md, ../core/how_core_works.md):
SWL — линейное звено пайплайна на одну фразу, между классификатором и
ядром. Классификатор пометил фразу как `command`; SWL определяет, ЧЕГО
пользователь хочет достичь, и кладёт цель в папку-очередь демона ядра.
Дальше ядро (core.py) само находит и выполняет путь к цели через модули.

Поток работы (по форме — как classifier.py следит за recognized.json):
- Следит за ../fwl/classifier/output/classified.json по mtime. При
  старте запоминает текущий mtime и НЕ переобрабатывает то, что уже
  лежало — реагирует только на новые записи (иначе каждый перезапуск
  дублировал бы последнюю фразу).
- Новая запись с label == "command": фраза уходит в intent_provider
  (GigaChat на bootstrap-этапе, см. swl_design.md) вместе с каталогом
  целей, собранным из манифестов модулей. Получаем (goal, params).
    - goal найден -> пишем goal.json в ../core/goals/ (атомарно,
      temp + rename — ядро читает эту папку и не должно поймать файл на
      середине записи) и дописываем фразу+разбор в output/dataset.jsonl
      (материал для будущей локальной модели-разборщика).
    - goal == None (LLM не нашла подходящей цели) -> пока просто
      логируем `no_intent_match` и ничего не отправляем. Что делать в
      этом случае по-хорошему (переспросить голосом? отдать в чат?) —
      открытый вопрос, см. swl_design.md.
- label == "chat": SWL не его дело — пропускаем (chat-модуля пока нет).

Запуск:
    python swl.py                 # демон: следит за classified.json
    python swl.py "погода в питере"  # разовый прогон одной фразы (для проверки)

Демон ядра (core.py) может быть ещё не запущен — тогда goal.json просто
копятся в очереди и разберутся, когда ядро поднимется.
"""

import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402
from log_client import send_log  # noqa: E402
import catalog  # noqa: E402
import intent_provider  # noqa: E402

CFG = config.load(SCRIPT_DIR)

CLASSIFIED_FILE = SCRIPT_DIR / CFG["paths"]["classifier_output_dir"] / CFG["paths"]["classifier_output_file"]
GOALS_DIR = SCRIPT_DIR / CFG["paths"]["core_goals_dir"]
OUTPUT_DIR = SCRIPT_DIR / CFG["paths"]["output_dir"]
DATASET_FILE = OUTPUT_DIR / CFG["paths"]["dataset_file"]

CHECK_INTERVAL_SEC = CFG["check_interval_sec"]


def load_classified() -> dict:
    return json.loads(CLASSIFIED_FILE.read_text(encoding="utf-8"))


def write_goal(goal: str, state: dict, source_text: str) -> str:
    """Кладёт одну цель в папку-очередь ядра. Пишет АТОМАРНО: сначала
    во временный файл рядом, потом переименование — ядро читает эту
    папку в своём цикле и не должно поймать файл на середине записи
    (тот же приём temp + rename, что везде в проекте).

    Имя файла начинается с метки времени, чтобы ядро разбирало очередь
    в порядке поступления (см. core.pending_goal_files), плюс короткий
    случайный хвост на случай двух целей в одну секунду."""
    GOALS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
    payload = {
        "goal": goal,
        "state": state,
        "origin": "swl",
        "source_text": source_text,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    tmp_file = GOALS_DIR / (name + ".tmp")
    tmp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(GOALS_DIR / name)
    return name


def append_to_dataset(text: str, goal: str | None, params: dict) -> None:
    """Дописывает одну строку в накопительный датасет (JSONL) — фраза и
    как её разобрала облачная LLM. Пригодится, когда будем обучать
    локальную модель-разборщик (см. swl_design.md, по аналогии с
    dataset.jsonl классификатора). Пишем и разбор с goal == None —
    отрицательные примеры тоже нужны."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    row = {"text": text, "goal": goal, "params": params, "source": "llm_api"}
    with open(DATASET_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def handle_command(text: str) -> None:
    """Разбирает одну фразу-команду в цель и отправляет её ядру."""
    goals_catalog = catalog.build()

    try:
        goal, params = intent_provider.extract(text, goals_catalog)
    except Exception as e:
        send_log("ERROR", "intent_extraction_failed", {"text": text, "error": str(e)})
        print(f"[swl] ошибка разбора фразы: {e}")
        return

    append_to_dataset(text, goal, params)

    if goal is None:
        send_log("INFO", "no_intent_match", {"text": text})
        print(f"[swl] ни одна цель не подошла: {text!r}")
        return

    goal_file = write_goal(goal, params, text)
    send_log("INFO", "intent_extracted", {
        "text": text, "goal": goal, "params": params, "goal_file": goal_file,
    })
    print(f"[swl] {text!r} -> цель {goal!r}, параметры {params} -> {goal_file}")


def main() -> None:
    send_log("INFO", "swl_started")
    print(f"[swl] слежу за {CLASSIFIED_FILE}")
    print(f"[swl] цели кладу в {GOALS_DIR}")

    # При старте не переобрабатываем то, что уже лежит в classified.json
    # (иначе каждый перезапуск swl.py дублировал бы последнюю команду) —
    # реагируем только на изменения ПОСЛЕ запуска.
    last_mtime = CLASSIFIED_FILE.stat().st_mtime if CLASSIFIED_FILE.exists() else None

    while True:
        if CLASSIFIED_FILE.exists():
            mtime = CLASSIFIED_FILE.stat().st_mtime
            if mtime != last_mtime:
                last_mtime = mtime

                try:
                    classified = load_classified()
                except (json.JSONDecodeError, OSError):
                    # classifier.py пишет classified.json атомарно, но
                    # подстрахуемся тем же приёмом, что и он с
                    # recognized.json — просто пробуем на следующем цикле.
                    time.sleep(CHECK_INTERVAL_SEC)
                    continue

                text = (classified.get("text") or "").strip()
                label = classified.get("label")

                if not text:
                    pass
                elif label == "command":
                    handle_command(text)
                else:
                    # label == "chat" (или что-то ещё) — не забота SWL.
                    send_log("DEBUG", "skipped_non_command", {"text": text, "label": label})

        time.sleep(CHECK_INTERVAL_SEC)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # Разовый прогон одной фразы — для проверки разбора без
        # классификатора и микрофона.
        phrase = " ".join(sys.argv[1:])
        print(f"[swl] разовый разбор: {phrase!r}")
        handle_command(phrase)
    else:
        try:
            main()
        except KeyboardInterrupt:
            send_log("INFO", "swl_stopped")
            print("\n[swl] остановлен")
