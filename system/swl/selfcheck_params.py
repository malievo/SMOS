"""selfcheck_params.py — регрессионный прогон системы шаблонов SWL
(params_design.md): slot_types, validate_and_normalize, catalog и сквозной
handle_command со стаб-разборщиком. Без живого GigaChat.

    python3 system/swl/selfcheck_params.py     # 0 = все прошли, 1 = есть провал
"""
import sys, json, tempfile, pathlib, importlib.util
ROOT = pathlib.Path(__file__).resolve().parents[2]  # .../SMOS

# Пришпиливаем именно system/swl/config.py как модуль `config` до всех
# остальных импортов: catalog->registry кидают system/core на sys.path, и
# тогда `import config` у intent_provider цепляет чужой system/core/config.py
# (у него нет user_env_file). В обычном запуске swl.py такого нет.
_spec = importlib.util.spec_from_file_location("config", ROOT / "system/swl/config.py")
_cfg = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_cfg)
sys.modules["config"] = _cfg

sys.path.insert(0, str(ROOT / "system/swl"))
import slot_types as st
import catalog, swl, intent_provider

sys.path.insert(0, str(ROOT / "system/core/planner"))
import registry
import planner

ERR = object()  # ожидаем SlotError

passed = failed = 0
fails = []

def check(label, got, exp):
    global passed, failed
    ok = (got is ERR and exp is ERR) or (got == exp)
    if ok:
        passed += 1
    else:
        failed += 1
        fails.append(f"  [{label}] got={got!r}  exp={exp!r}")

def norm(slot, raw):
    try:
        return st.normalize(raw, slot)
    except st.SlotError:
        return ERR

# ---------------------------------------------------------------- slot_types
DT = lambda h, m, mer: {"hour": h, "minute": m, "meridiem": mer, "date": None}

CASES = [
  # --- arithmetic_expression ---
  ("arith", {"type": "arithmetic_expression"}, "12 x 10", "12 * 10"),
  ("arith", {"type": "arithmetic_expression"}, "12 х 10", "12 * 10"),   # кир. х
  ("arith", {"type": "arithmetic_expression"}, "12 × 10", "12 * 10"),
  ("arith", {"type": "arithmetic_expression"}, "2х2", "2*2"),
  ("arith", {"type": "arithmetic_expression"}, "два плюс два", "2 + 2"),
  ("arith", {"type": "arithmetic_expression"}, "посчитай 2 + 2 * 3", "2 + 2 * 3"),
  ("arith", {"type": "arithmetic_expression"}, "сколько будет 100 разделить на 4", "100 / 4"),
  ("arith", {"type": "arithmetic_expression"}, "10 минус 3", "10 - 3"),
  ("arith", {"type": "arithmetic_expression"}, "(2 + 3) * 4", "(2 + 3) * 4"),
  ("arith", {"type": "arithmetic_expression"}, "2,5 + 1,5", "2.5 + 1.5"),
  ("arith", {"type": "arithmetic_expression"}, "9 ** 2", "9 ** 2"),
  ("arith", {"type": "arithmetic_expression"}, "5", "5"),
  ("arith", {"type": "arithmetic_expression"}, "10 / 0", "10 / 0"),        # деление на 0 — забота модуля
  ("arith", {"type": "arithmetic_expression"}, "двенадцать умножить на десять", "12 * 10"),
  ("arith", {"type": "arithmetic_expression"}, "привет мир", ERR),
  ("arith", {"type": "arithmetic_expression"}, "два в квадрате", ERR),     # вне v1
  ("arith", {"type": "arithmetic_expression"}, "sqrt(4)", ERR),
  ("arith", {"type": "arithmetic_expression"}, "", ERR),
  ("arith", {"type": "arithmetic_expression"}, "2 + + 3", "2 + + 3"),   # унарный плюс — валидно
  ("arith", {"type": "arithmetic_expression"}, "2 + * 3", ERR),          # настоящая синт. ошибка
  ("arith", {"type": "arithmetic_expression"}, ")2(", ERR),

  # --- datetime ---
  ("dt", {"type": "datetime"}, "7 утра", DT(7, 0, "am")),
  ("dt", {"type": "datetime"}, "в 19:30", DT(19, 30, None)),
  ("dt", {"type": "datetime"}, "19.30", DT(19, 30, None)),
  ("dt", {"type": "datetime"}, "8 вечера", DT(8, 0, "pm")),
  ("dt", {"type": "datetime"}, "полдень", DT(12, 0, None)),
  ("dt", {"type": "datetime"}, "полночь", DT(0, 0, None)),
  ("dt", {"type": "datetime"}, "полвосьмого", DT(7, 30, None)),
  ("dt", {"type": "datetime"}, "полвосьмого вечера", DT(7, 30, "pm")),
  ("dt", {"type": "datetime"}, "в семь", DT(7, 0, None)),
  ("dt", {"type": "datetime"}, "в 7 часов утра", DT(7, 0, "am")),
  ("dt", {"type": "datetime"}, "в 25:00", ERR),
  ("dt", {"type": "datetime"}, "в 12:75", ERR),
  ("dt", {"type": "datetime"}, "завтра", ERR),
  ("dt", {"type": "datetime"}, "", ERR),
  ("dt", {"type": "datetime"}, {"hour": 9, "minute": 15}, DT(9, 15, None)),

  # --- duration ---
  ("dur", {"type": "duration"}, "10 минут", 600),
  ("dur", {"type": "duration"}, "полчаса", 1800),
  ("dur", {"type": "duration"}, "полтора часа", 5400),
  ("dur", {"type": "duration"}, "два часа", 7200),
  ("dur", {"type": "duration"}, "1 час 30 минут", 5400),
  ("dur", {"type": "duration"}, "90 секунд", 90),
  ("dur", {"type": "duration"}, "минуту", 60),
  ("dur", {"type": "duration"}, "пять минут", 300),
  ("dur", {"type": "duration"}, "три дня", 259200),
  ("dur", {"type": "duration"}, "0 минут", ERR),
  ("dur", {"type": "duration"}, "скоро", ERR),
  ("dur", {"type": "duration"}, 45, 45),

  # --- integer / number ---
  ("int", {"type": "integer"}, "12", 12),
  ("int", {"type": "integer"}, "двенадцать", 12),
  ("int", {"type": "integer"}, "тридцать пять", 35),
  ("int", {"type": "integer"}, "сто", 100),
  ("int", {"type": "integer"}, "-5", -5),
  ("int", {"type": "integer"}, "3.0", 3),
  ("int", {"type": "integer"}, "3,5", ERR),
  ("int", {"type": "integer"}, "2 штуки", ERR),
  ("int", {"type": "integer"}, "много", ERR),
  ("int", {"type": "integer"}, 7, 7),
  ("int", {"type": "integer"}, True, ERR),
  ("num", {"type": "number"}, "3,5", 3.5),
  ("num", {"type": "number"}, "10", 10),
  ("num", {"type": "number"}, "-2.25", -2.25),
  ("num", {"type": "number"}, "пять", 5),

  # --- enum / crud_action / toggle ---
  ("crud", {"type": "crud_action"}, "заведи", "create"),
  ("crud", {"type": "crud_action"}, "создай", "create"),
  ("crud", {"type": "crud_action"}, "поставь будильник", "create"),
  ("crud", {"type": "crud_action"}, "удали", "delete"),
  ("crud", {"type": "crud_action"}, "убери напоминание", "delete"),
  ("crud", {"type": "crud_action"}, "перенеси", "edit"),
  ("crud", {"type": "crud_action"}, "CREATE", "create"),
  ("crud", {"type": "crud_action"}, "помой посуду", ERR),
  ("tgl", {"type": "toggle"}, "включи", "on"),
  ("tgl", {"type": "toggle"}, "выключи свет", "off"),
  ("tgl", {"type": "toggle"}, "поставь", ERR),
  ("enum", {"type": "enum", "values": ["small", "medium", "large"],
           "aliases": {"large": ["большой", "большую"], "small": ["маленький"]}}, "большую", "large"),
  ("enum", {"type": "enum", "values": ["small", "large"]}, "medium", ERR),
  ("enum", {"type": "enum", "values": ["small", "large"]}, "large", "large"),

  # --- city / name ---
  ("city", {"type": "city"}, "питер", "Санкт-Петербург"),
  ("city", {"type": "city"}, "в питере", "Санкт-Петербург"),
  ("city", {"type": "city"}, "спб", "Санкт-Петербург"),
  ("city", {"type": "city"}, "москве", "Москва"),
  ("city", {"type": "city"}, "нижний", "Нижний Новгород"),
  ("city", {"type": "city"}, "Казань", "Казань"),
  ("city", {"type": "city"}, "ростов на дону", "Ростов-На-Дону"),
  ("name", {"type": "name"}, "иван петров", "Иван Петров"),
  ("name", {"type": "name"}, "  анна  ", "Анна"),

  # --- text ---
  ("text", {"type": "text"}, "  купить хлеб ", "купить хлеб"),
  ("text", {"type": "text"}, 42, "42"),
]

for label, slot, raw, exp in CASES:
    check(f"{label} {raw!r}", norm(slot, raw), exp)

# ---------------------------------------------------- validate_and_normalize
cat = {c["goal"]: c for c in catalog.build()}

def vn(goal, raw, entry=None):
    e = entry if entry is not None else cat.get(goal, {})
    return swl.validate_and_normalize(goal, raw, e)

check("v&n calc x",   vn("calc_result", {"expression": "12 x 10"}), {"expression": "12 * 10"})
check("v&n calc bad", vn("calc_result", {"expression": "мусор"}), {})          # отброшен, expression в needs
check("v&n calc none",vn("calc_result", {}), {})
check("v&n note trim",vn("note_saved", {"note_text": "  привет "}), {"note_text": "привет"})
check("v&n note miss",vn("note_saved", {}), {})                                # в needs -> планировщику, не None
check("v&n weather",  vn("weather_forecast", {"city": "питер"}), {"city": "Санкт-Петербург"})
check("v&n weather 0", vn("weather_forecast", {}), {})
check("v&n passthru",  vn("calc_result", {"expression": "2+2", "junk": "x"}), {"expression": "2+2", "junk": "x"})

SY_REQ = {"goal": "x", "needs": [], "params": {"foo": {"type": "text", "required": True}}}
check("v&n req miss -> None", vn("x", {}, SY_REQ), None)
check("v&n req ok",          vn("x", {"foo": "bar"}, SY_REQ), {"foo": "bar"})

SY_DEF = {"goal": "a", "needs": [], "params": {
    "act": {"type": "crud_action", "required": True},
    "label": {"type": "text", "default": "будильник"}}}
check("v&n enum+default",  vn("a", {"act": "заведи"}, SY_DEF), {"act": "create", "label": "будильник"})
check("v&n enum bad->None",vn("a", {"act": "спой песню"}, SY_DEF), None)
check("v&n default only",  vn("a", {"act": "удали"}, SY_DEF), {"act": "delete", "label": "будильник"})

SY_UNK = {"goal": "u", "needs": [], "params": {"p": {"type": "totally_made_up"}}}
check("v&n unknown type -> text passthrough", vn("u", {"p": "  hi "}, SY_UNK), {"p": "hi"})

SY_DT = {"goal": "t", "needs": [], "params": {"when": {"type": "datetime", "required": True}}}
check("v&n datetime struct", vn("t", {"when": "7 утра"}, SY_DT), {"when": DT(7, 0, "am")})
check("v&n datetime bad->None", vn("t", {"when": "когда-нибудь"}, SY_DT), None)

# ---------------------------------------------------------------- catalog
goals = {c["goal"] for c in catalog.build()}
check("catalog: city hidden", "city" in goals, False)
check("catalog: calc present", "calc_result" in goals, True)
check("catalog: calc schema",
      cat["calc_result"]["params"].get("expression", {}).get("type"), "arithmetic_expression")
check("catalog: weather city optional",
      cat["weather_forecast"]["params"].get("city", {}).get("required", False), False)
check("catalog: datetime no params", cat["current_datetime"]["params"], {})

# ------------------------------------------------ сквозной handle_command
tmp = pathlib.Path(tempfile.mkdtemp())
swl.GOALS_DIR = tmp; swl.OUTPUT_DIR = tmp; swl.DATASET_FILE = tmp / "dataset.jsonl"

E2E = [
  ("посчитай двенадцать умножить на десять", ("calc_result", {"expression": "двенадцать умножить на десять"}),
   {"expression": "12 * 10"}),
  ("сколько будет сто минус пять", ("calc_result", {"expression": "сто минус пять"}),
   {"expression": "100 - 5"}),
  ("запиши заметку позвонить маме", ("note_saved", {"note_text": "позвонить маме"}),
   {"note_text": "позвонить маме"}),
  ("какая погода в питере", ("weather_forecast", {"city": "питере"}), {"city": "Санкт-Петербург"}),
  ("какая погода", ("weather_forecast", {}), {}),
  ("покажи мои заметки", ("notes_list", {}), {}),
  ("который час", ("current_datetime", {}), {}),
  ("посчитай абракадабру", ("calc_result", {"expression": "абракадабра"}), {}),   # слот отброшен -> {}
  ("выключи компьютер", ("smos_stopped", {}), {}),
]
e2e_fail = 0
for phrase, fake, exp_state in E2E:
    intent_provider.extract = lambda t, c, _f=fake: _f
    for f in tmp.glob("*.json"):
        f.unlink()
    swl.handle_command(phrase, trace_id="t")
    files = list(tmp.glob("*.json"))
    got_state = json.loads(files[0].read_text())["state"] if files else "NO_GOAL_FILE"
    check(f"e2e {phrase!r}", got_state, exp_state)

# no-goal / missing-required паттерны
intent_provider.extract = lambda t, c: (None, {})
for f in tmp.glob("*.json"):
    f.unlink()
swl.handle_command("сделай мне красиво", trace_id="t")
check("e2e goal=None -> no file", len(list(tmp.glob("*.json"))), 0)

# ------------------------------------------------ планировщик на нормализованном
graph = registry.build_registry(registry.scan())
try:
    r = planner.achieve("calc_result", {"expression": "12 * 10"}, graph)
    check("planner calc 12*10 -> 120", r.get("value"), 120)
except Exception as e:
    check("planner calc", f"EXC {e}", 120)
try:
    r = planner.achieve("weather_forecast", {}, graph)   # добор city через location
    check("planner weather via location", bool(r.get("city")), True)
except Exception as e:
    check("planner weather", f"EXC {e}", True)

# ---------------------------------------------------------------- итог
print(f"\n{'='*60}")
print(f"PASS {passed}   FAIL {failed}   (всего {passed + failed})")
if fails:
    print("\nПРОВАЛЫ:")
    print("\n".join(fails))
else:
    print("все проверки прошли")
sys.exit(1 if failed else 0)
