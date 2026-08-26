"""
catalog.py — «каталог целей» SWL: что система вообще умеет достигать.

Это НЕ про конкретный онлайн-ИИ (им занят intent_provider.py) и НЕ про
цикл работы (swl.py) — просто сборка списка целей из манифестов модулей.
Отдельный файл, потому что каталог нужен одинаково и онлайн-разбору
(GigaChat сейчас), и будущей локальной модели-разборщику — ни один из
них не должен носить эту логику в себе.

Одна запись каталога:
    {"goal": <produces-ключ>, "description": <текст из манифеста>, "needs": [<ключи>]}

`goal` — то, что модуль объявляет в `produces`; `description` — поле
манифеста, заведённое ровно под этот случай (см.
../core/module_init/manifest_design.md); `needs` — что этой цели бывает
нужно на вход.

Если один и тот же ключ производят несколько действий — берётся первое
(та же логика, что в planner.find_action_that_produces).

Состав модулей при работе не меняется (hot-reload сознательно нет, см.
swl_design.md), поэтому результат кэшируется — сканировать диск на
каждую фразу незачем. build(force=True) — пересобрать принудительно.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
# Наш log_client должен попасть в sys.modules ДО импорта registry — у
# registry внутри тоже "from log_client import send_log", и мы хотим,
# чтобы это был наш ("swl"), а не core/log_client.py, который registry
# добавляет в путь при импорте.
import log_client  # noqa: F401,E402

MODULE_INIT_DIR = SCRIPT_DIR.parent / "core" / "module_init"
if str(MODULE_INIT_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_INIT_DIR))

import registry as module_registry  # noqa: E402

_cache: list[dict] | None = None


def build(force: bool = False) -> list[dict]:
    """Собирает каталог целей из манифестов модулей. Кэширует результат;
    force=True — пересобрать."""
    global _cache
    if _cache is not None and not force:
        return _cache

    modules = module_registry.scan()
    catalog: list[dict] = []
    seen: set[str] = set()
    for mod in modules:
        for action in mod.get("actions", []):
            for key in action.get("produces", []):
                if key in seen:
                    continue
                seen.add(key)
                catalog.append({
                    "goal": key,
                    "description": action.get("description", ""),
                    "needs": action.get("needs", []),
                })

    _cache = catalog
    return catalog
