"""
timerlib.py — общие мелочи модуля mod_timer: поиск корня проекта,
разбор длительности из фразы в секунды, человекочитаемая длительность,
атомарная запись JSON.

Не точка входа и не демон — просто функции, которыми пользуются main.py
(диспетчер) и timer_daemon.py (долгоживущий процесс).
"""

import json
import re
import uuid
from pathlib import Path

ROOT_MARKER = "smos.root"

# Числительные словами -> число. SWL (GigaChat) на bootstrap-этапе
# частенько отдаёт длительность прописью ("десять секунд"), а не цифрой,
# поэтому разбор держит сам модуль — не полагаемся на нормализацию выше.
# Десятки и единицы складываются ("двадцать пять" = 20 + 5).
_NUM_WORDS = {
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "полтора": 1.5, "полторы": 1.5,
    "два": 2, "две": 2, "пара": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11,
    "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
    "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
}

# Токены-единицы -> сколько в них секунд. Одиночные "м"/"с" СОЗНАТЕЛЬНО
# не синонимы: "две с половиной минуты" не должно поймать "с" как
# секунды. В речи всё равно говорят "минут"/"секунд".
_UNITS = (
    (("час", "часа", "часов", "ч"), 3600),
    (("минута", "минуту", "минуты", "минут", "мин"), 60),
    (("секунда", "секунду", "секунды", "секунд", "сек"), 1),
)

_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d]+", re.UNICODE)


def project_root(start: Path) -> Path:
    """Поднимается от start вверх до папки с файлом-маркером smos.root.
    Тот же приём, что в каждом config.py проекта. Бросает RuntimeError,
    если маркер не найден нигде выше (модуль запущен вне дерева SMOS)."""
    start = Path(start).resolve()
    for folder in (start, *start.parents):
        if (folder / ROOT_MARKER).exists():
            return folder
    raise RuntimeError(f"не найден корень проекта (файл {ROOT_MARKER}) выше {start}")


def sessions_dir(start: Path) -> Path:
    return project_root(start) / "system" / "core" / "sessions"


def outputstructurizer_queue(start: Path) -> Path:
    return project_root(start) / "system" / "outputstructurizer" / "queue"


def _unit_of(token: str) -> int | None:
    for words, secs in _UNITS:
        if token in words:
            return secs
    return None


def parse_duration(raw) -> int | None:
    """Приводит длительность к целым секундам. Принимает:
      - число (int/float) — уже секунды;
      - строку из одних цифр — секунды ("600");
      - строку с единицами, цифрами ИЛИ числительными словами:
        "10 минут", "десять секунд", "1 час 30 минут", "двадцать пять
        минут", "полторы минуты", "полчаса", "минуту".
    Возвращает None, если разобрать не удалось — вызывающий решает, что
    это ошибка. Составные числительные вроде "сто двадцать" за пределами
    таблицы не поддерживаются (для таймера практически не встречаются)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw) if raw > 0 else None
    if not isinstance(raw, str):
        return None

    s = raw.strip().lower().replace(",", ".")
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        val = int(float(s))
        return val if val > 0 else None

    total = 0.0
    current: float | None = None
    matched_unit = False

    for tok in _TOKEN_RE.findall(s):
        # "полчаса", "полминуты" — единым токеном
        if tok.startswith("пол") and len(tok) > 3:
            rest = tok[3:]
            u = _unit_of(rest)
            if u is not None:
                total += 0.5 * u
                matched_unit = True
                current = None
                continue

        if re.fullmatch(r"\d+(?:\.\d+)?", tok):
            current = (current or 0) + float(tok)
            continue

        if tok in _NUM_WORDS:
            current = (current or 0) + _NUM_WORDS[tok]
            continue

        if tok in ("пол", "половина", "половину", "половиной"):
            # "пол часа" / "две с половиной минуты"
            current = (current or 0) + 0.5
            continue

        u = _unit_of(tok)
        if u is not None:
            n = current if current is not None else 1
            total += n * u
            matched_unit = True
            current = None

    if not matched_unit or total <= 0:
        return None
    return int(round(total))


def human_duration(seconds: int) -> str:
    """Короткая форма для озвучки: "10 мин", "1 ч 30 мин", "45 сек".
    Сокращения намеренно без склонений — правильные русские формы
    ("1 минуту" / "5 минут") оставлены на потом."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} ч")
    if m:
        parts.append(f"{m} мин")
    if s and not h:
        parts.append(f"{s} сек")
    return " ".join(parts) if parts else "0 сек"


def atomic_write_json(path: Path, data: dict) -> None:
    """temp-файл + rename — тот же приём, что везде в SMOS: читатель
    (ядро, outputstructurizer) не поймает файл на середине записи."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
