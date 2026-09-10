"""
slot_types.py — реестр типов слотов SWL: валидация и нормализация
значений параметров под форму, объявленную в манифесте действия.

См. params_design.md (рядом с этим файлом). Ключевые принципы:

- На каждый тип — пара {validate, normalize}, здесь они слиты в один
  обработчик: `normalize(raw, slot, ctx)` возвращает значение нужной
  формы либо бросает SlotError, если сырое значение к этой форме не
  приводится.
- Тип задаёт ФОРМУ значения по проводу (что уйдёт модулю в
  JSON-параметрах). `datetime` -> {hour, minute, meridiem, date};
  `arithmetic_expression` -> строка из безопасного charset; и т.д.
- Нормализация — это переписывание ПРЕДСТАВЛЕНИЯ, без вычислений и без
  обращения к «сейчас». «12 x 10» -> «12 * 10» (не 120); «семь утра» ->
  {hour: 7, ...} с date: null (перекат «уже прошло -> завтра» делает
  модуль, ему приходит референсный timestamp).
- Зависимостей нет (только re, ast) — модуль обязан импортироваться и в
  офлайне. Позже сюда же въезжает офлайновый слой правил/NER
  (local_brain_design.md §3): enum/number/duration -> Yargy,
  city/name -> Natasha.

Использование (в swl.py):
    import slot_types
    value = slot_types.normalize(raw_value, slot_schema)   # SlotError при провале
"""

import ast
import re

__all__ = ["SlotError", "normalize", "describe", "KNOWN_TYPES"]


class SlotError(Exception):
    """Значение слота присутствует, но не приводится к форме его типа."""


# --------------------------------------------------------------------------
# Русские числительные — общий помощник для integer/number/datetime/duration.
# Покрывает единицы, десятки, сотни и простые составные («тридцать пять»).
# --------------------------------------------------------------------------

_RU_UNITS = {
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19,
}
_RU_TENS = {
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90,
}
_RU_HUNDREDS = {
    "сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500,
    "шестьсот": 600, "семьсот": 700, "восемьсот": 800, "девятьсот": 900,
}


def _ru_number(s: str):
    """«тридцать пять» -> 35. Возвращает int или None, если хоть один
    токен не из числительных (значит это не числовая фраза)."""
    s = s.strip().lower()
    if not s:
        return None
    total = 0
    found = False
    for tok in re.split(r"[\s-]+", s):
        if tok in _RU_HUNDREDS:
            total += _RU_HUNDREDS[tok]
        elif tok in _RU_TENS:
            total += _RU_TENS[tok]
        elif tok in _RU_UNITS:
            total += _RU_UNITS[tok]
        elif tok in ("тысяча", "тысячи", "тысяч"):
            total = (total or 1) * 1000
        else:
            return None
        found = True
    return total if found else None


def _titlecase_ru(s: str) -> str:
    """«санкт-петербург» -> «Санкт-Петербург», «нижний новгород» ->
    «Нижний Новгород». Каждое буквенное слово с заглавной."""
    return re.sub(r"[A-Za-zА-Яа-яЁё]+", lambda m: m.group(0).capitalize(), s)


# --------------------------------------------------------------------------
# Обработчики типов. Сигнатура: (raw, slot, ctx) -> value | raise SlotError.
# raw — то, что вернула модель намерения для слота (str/число/dict).
# slot — схема слота из манифеста ({type, values?, aliases?, ...}).
# ctx — задел на будущее (язык и т.п.); форму значения не трогает.
# --------------------------------------------------------------------------

def _text(raw, slot, ctx):
    return str(raw).strip()


def _integer(raw, slot, ctx):
    if isinstance(raw, bool):
        raise SlotError("ожидалось число, получено булево")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if raw.is_integer():
            return int(raw)
        raise SlotError(f"ожидалось целое, получено {raw}")
    s = str(raw).strip().lower().replace(",", ".")
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    if re.fullmatch(r"-?\d+\.0+", s):
        return int(float(s))
    n = _ru_number(s)
    if n is not None:
        return n
    raise SlotError(f"не целое число: {raw!r}")


def _number(raw, slot, ctx):
    if isinstance(raw, bool):
        raise SlotError("ожидалось число, получено булево")
    if isinstance(raw, (int, float)):
        f = float(raw)
        return int(f) if f.is_integer() else f
    s = str(raw).strip().lower().replace(",", ".")
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        f = float(s)
        return int(f) if f.is_integer() else f
    n = _ru_number(s)
    if n is not None:
        return n
    raise SlotError(f"не число: {raw!r}")


# Именованные enum-типы: общие таблицы синонимов из реестра (params_design.md
# §5). Слот может взять готовый тип ("type": "crud_action") и/или добавить
# свои "aliases" под доменные значения.
_NAMED_ENUMS = {
    "crud_action": {
        "values": ["create", "edit", "delete"],
        "aliases": {
            "create": ["создай", "создать", "поставь", "поставить", "заведи",
                       "завести", "добавь", "добавить", "новый", "новая", "сделай"],
            "edit": ["измени", "изменить", "поменяй", "поменять", "перенеси",
                     "перенести", "обнови", "обновить", "отредактируй", "правь"],
            "delete": ["удали", "удалить", "убери", "убрать", "отмени",
                       "отменить", "сними", "снять", "сотри", "стереть"],
        },
    },
    "toggle": {
        "values": ["on", "off"],
        "aliases": {
            "on": ["включи", "включить", "вкл", "зажги", "запусти", "открой"],
            "off": ["выключи", "выключить", "выкл", "погаси", "останови", "закрой"],
        },
    },
}


def _enum(raw, slot, ctx):
    base = _NAMED_ENUMS.get(slot.get("type"), {})
    values = list(slot.get("values") or base.get("values") or [])
    if not values:
        raise SlotError("для enum-слота не заданы values")

    aliases: dict[str, set] = {}
    for canon, syns in (base.get("aliases") or {}).items():
        aliases.setdefault(canon, set()).update(syns)
    for canon, syns in (slot.get("aliases") or {}).items():
        syns = syns if isinstance(syns, (list, tuple, set)) else [syns]
        aliases.setdefault(canon, set()).update(syns)

    s = str(raw).strip().lower()
    for v in values:
        if s == v.lower():
            return v
    words = set(re.split(r"[\s,.!?;:]+", s))
    for canon, syns in aliases.items():
        if canon not in values:
            continue
        for syn in syns:
            syn = str(syn).lower()
            if s == syn or syn in words:
                return canon
    raise SlotError(f"{raw!r} не сопоставлено ни с одним из {values}")


_RU_MERIDIEM = {
    "утра": "am", "утром": "am", "ночи": "am", "ночью": "am",
    "дня": "pm", "днём": "pm", "днем": "pm", "вечера": "pm", "вечером": "pm",
}
_RU_ORDINAL = {
    "первого": 1, "второго": 2, "третьего": 3, "четвёртого": 4, "четвертого": 4,
    "пятого": 5, "шестого": 6, "седьмого": 7, "восьмого": 8, "девятого": 9,
    "десятого": 10, "одиннадцатого": 11, "двенадцатого": 12,
}


def _dt(hour, minute, mer):
    """Собирает результат datetime с проверкой диапазона. hour 0..23,
    minute 0..59 — иначе это не время (напр. «в 25:00»)."""
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        raise SlotError(f"время вне диапазона: {hour:02d}:{minute:02d}")
    return {"hour": hour, "minute": minute, "meridiem": mer, "date": None}


def _datetime(raw, slot, ctx):
    """-> {hour, minute, meridiem, date}. date всегда None: SWL не знает,
    сегодня или завтра — это решает модуль по референсному времени.
    meridiem («am»/«pm»/None) — подсказка, 12→24 переводит модуль."""
    if isinstance(raw, dict):
        if raw.get("hour") is None:
            raise SlotError("в структуре времени нет hour")
        return _dt(int(raw["hour"]), int(raw.get("minute") or 0), raw.get("meridiem"))

    s = str(raw).strip().lower()
    if "полночь" in s:
        return _dt(0, 0, None)
    if "полдень" in s:
        return _dt(12, 0, None)

    mer = None
    for w, m in _RU_MERIDIEM.items():
        if re.search(rf"\b{w}\b", s):
            mer = m
            break

    m = re.search(r"\b(\d{1,2})[:.\-](\d{2})\b", s)
    if m:
        return _dt(int(m.group(1)), int(m.group(2)), mer)

    m = re.search(r"\bпол\s?(" + "|".join(_RU_ORDINAL) + r")\b", s)
    if m:
        n = _RU_ORDINAL[m.group(1)]
        return _dt((n - 1) % 24, 30, mer)

    m = re.search(r"\b(\d{1,2})\b", s)
    if m:
        return _dt(int(m.group(1)), 0, mer)

    cleaned = re.sub(r"\b(в|во|на|к|около|часов|часа|час|утра|утром|вечера|"
                     r"вечером|дня|днём|днем|ночи|ночью)\b", " ", s)
    n = _ru_number(cleaned)
    if n is not None and 0 <= n <= 23:
        return _dt(n, 0, mer)

    raise SlotError(f"не распознано как время: {raw!r}")


_DUR_UNITS = [
    (r"(?:секунд\w*|сек)", 1),
    (r"(?:минут\w*|мин)", 60),
    (r"(?:час\w*|ч)\b", 3600),
    (r"(?:дн\w*|день|сут\w*)", 86400),
]


def _duration(raw, slot, ctx):
    """-> длительность в секундах (int > 0)."""
    if isinstance(raw, bool):
        raise SlotError("ожидалась длительность, получено булево")
    if isinstance(raw, (int, float)):
        v = int(raw)
        if v <= 0:
            raise SlotError("длительность должна быть положительной")
        return v

    s = str(raw).strip().lower()
    if "полчаса" in s:
        return 1800
    if "полтора часа" in s:
        return 5400

    total = 0
    saw_count = False
    for unit_re, mult in _DUR_UNITS:
        for m in re.finditer(r"(\d+|[а-яё]+)\s*" + unit_re, s):
            tok = m.group(1)
            n = int(tok) if tok.isdigit() else _ru_number(tok)
            if n is not None:
                saw_count = True
                total += n * mult

    # «час» / «минуту» без числа — единица по умолчанию. Но не когда число
    # уже назвали (иначе «0 минут» превратилось бы в 60).
    if total <= 0 and not saw_count:
        if re.search(r"\bчас\w*\b", s):
            total = 3600
        elif re.search(r"\b(минут\w*|минуту)\b", s):
            total = 60

    if total <= 0:
        raise SlotError(f"не распознано как длительность: {raw!r}")
    return total


_ARITH_WORD_OPS = [
    (r"\bплюс\b", "+"),
    (r"\bминус\b", "-"),
    (r"\b(?:умнож\w*|помнож\w*)(?:\s+на)?\b", "*"),
    (r"\b(?:раздел\w*|подел\w*|дел\w*\s+на)(?:\s+на)?\b", "/"),
]
_ARITH_STRIP = re.compile(
    r"\b(посчитай\w*|подсчитай\w*|посчитать|вычисли\w*|сосчитай\w*|реши\w*|"
    r"сколько\s+будет|сколько\s+это\s+будет|чему\s+равно|пожалуйста|спасибо)\b"
)
_ARITH_TRANS = {ord(k): v for k, v in {
    "х": "*", "x": "*", "×": "*", "✕": "*", "⋅": "*", "·": "*", "∙": "*",
    ":": "/", "÷": "/", "–": "-", "—": "-", "−": "-",
}.items()}
_RU_NUM_TOKEN = {**_RU_UNITS, **_RU_TENS, **_RU_HUNDREDS}


def _arithmetic_expression(raw, slot, ctx):
    """Токен-aware приведение к безопасному арифметическому виду. Ничего
    не вычисляет — только переписывает строку. Валиден charset
    `0-9 + - * / ( ) . пробел` И строка разбирается ast.parse."""
    s = str(raw).strip().lower()
    s = _ARITH_STRIP.sub(" ", s)
    for pat, op in _ARITH_WORD_OPS:
        s = re.sub(pat, op, s)
    # одиночные числительные словом -> цифры («два» -> «2»); составные
    # («двадцать три») не трогаем — редки в устном счёте, упадут по charset.
    s = re.sub(r"[а-яё]+",
               lambda m: str(_RU_NUM_TOKEN[m.group(0)]) if m.group(0) in _RU_NUM_TOKEN else m.group(0),
               s)
    s = s.translate(_ARITH_TRANS)
    s = s.replace(",", ".")
    s = re.sub(r"\s+", " ", s).strip()

    if not s or not re.fullmatch(r"[0-9+\-*/().\s]+", s):
        raise SlotError(f"не арифметическое выражение: {raw!r} -> {s!r}")
    try:
        ast.parse(s, mode="eval")
    except SyntaxError as e:
        raise SlotError(f"выражение не разбирается: {s!r} ({e.msg})") from e
    return s


# Открытые слоты. v1 — лёгкая лексическая нормализация; настоящий NER
# (Natasha) въезжает в 1.3, см. local_brain_design.md §3.
_CITY_ALIASES = {
    "санкт-петербург": ["питер", "питере", "спб", "петербург", "петербурге",
                        "санкт петербург", "ленинград"],
    "москва": ["мск", "москоу", "москве"],
    "нижний новгород": ["нижний", "нн"],
    "екатеринбург": ["екб", "ебург", "екат"],
    "ростов-на-дону": ["ростов на дону", "ростов", "ростове"],
}


def _city(raw, slot, ctx):
    s = re.sub(r"\s+", " ", str(raw).strip().lower())
    s = re.sub(r"^(?:в|во|на|город|городе|г\.?)\s+", "", s)
    for canon, al in _CITY_ALIASES.items():
        if s == canon or s in al:
            s = canon
            break
    return _titlecase_ru(s)


def _name(raw, slot, ctx):
    return _titlecase_ru(str(raw).strip())


TYPES = {
    "text": _text,
    "integer": _integer,
    "number": _number,
    "enum": _enum,
    "crud_action": _enum,
    "toggle": _enum,
    "datetime": _datetime,
    "duration": _duration,
    "arithmetic_expression": _arithmetic_expression,
    "city": _city,
    "name": _name,
}

KNOWN_TYPES = frozenset(TYPES)


def normalize(raw, slot: dict, ctx: dict | None = None):
    """Приводит raw к форме слота. Неизвестный тип трактуется как `text`
    (вызывающий код может отдельно предупредить). SlotError — если
    значение к форме не приводится."""
    handler = TYPES.get(slot.get("type", "text"), _text)
    return handler(raw, slot, ctx or {})


def describe(slot: dict) -> str:
    """Короткое описание слота для промпта модели намерения / отладки."""
    t = slot.get("type", "text")
    vals = slot.get("values") or _NAMED_ENUMS.get(t, {}).get("values")
    extra = f" (одно из: {', '.join(vals)})" if vals else ""
    req = " [обязательный]" if slot.get("required") else ""
    return f"{t}{extra}{req}"


if __name__ == "__main__":
    # Ручная проверка: таблица (тип, сырое) -> результат/ошибка.
    samples = [
        ({"type": "arithmetic_expression"}, "12 x 10"),
        ({"type": "arithmetic_expression"}, "посчитай 2 плюс 2 умножить на 3"),
        ({"type": "arithmetic_expression"}, "два в квадрате"),
        ({"type": "integer"}, "двенадцать"),
        ({"type": "number"}, "3,5"),
        ({"type": "datetime"}, "7 утра"),
        ({"type": "datetime"}, "в 19:30"),
        ({"type": "datetime"}, "полвосьмого вечера"),
        ({"type": "duration"}, "полтора часа"),
        ({"type": "duration"}, "10 минут"),
        ({"type": "crud_action"}, "заведи"),
        ({"type": "enum", "values": ["small", "large"],
          "aliases": {"large": ["большой", "большую"]}}, "большую"),
        ({"type": "city"}, "в питере"),
    ]
    for slot, raw in samples:
        try:
            print(f"{slot['type']:>22} | {raw!r:<40} -> {normalize(raw, slot)!r}")
        except SlotError as e:
            print(f"{slot['type']:>22} | {raw!r:<40} -> SlotError: {e}")
