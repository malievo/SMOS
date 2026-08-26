"""
registry.py — сканирование папок модулей SMOS и построение реестра.

Вызывается ОДИН РАЗ при старте ядра (см. ../core_design.md — осознанно
без динамической догрузки во время работы: новый модуль становится
виден только после перезапуска). Это не демон и не следящий процесс —
просто функции scan()/build_registry(), которые ядро вызовет один раз
на старте и дальше будет держать результат у себя в памяти.

Смотрит две папки одинаково, без разницы в обработке (см.
../../swl/swl_design.md):
- system/modules/  — модули, которые идут вместе с репозиторием SMOS
- modules/ (в корне проекта) — личные модули, вне репозитория

Скрипт сам находит своё расположение и вычисляет пути от него — не
зависит от того, откуда его запустили.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_DIR = SCRIPT_DIR.parent  # system/core/ — там общий для ядра log_client.py
for path in (SCRIPT_DIR, CORE_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import manifest  # noqa: E402
from log_client import send_log  # noqa: E402

PROJECT_ROOT = CORE_DIR.parent.parent  # system/core/ -> system/ -> SMOS/
SYSTEM_MODULES_DIR = PROJECT_ROOT / "system" / "modules"
USER_MODULES_DIR = PROJECT_ROOT / "modules"


def _scan_dir(modules_dir: Path) -> list[dict]:
    """Проходит по подпапкам modules_dir, возвращает список валидных
    манифестов модулей (с добавленным служебным полем _module_dir —
    он понадобится потом, чтобы знать, откуда запускать entrypoint)."""
    found = []
    if not modules_dir.exists():
        return found

    for entry in sorted(modules_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            data = manifest.load(entry)
        except manifest.ManifestError as e:
            send_log("WARNING", "manifest_invalid", {"path": str(entry), "error": str(e)})
            print(f"[module_init] пропускаю {entry.name}: {e}")
            continue

        data["_module_dir"] = str(entry)
        found.append(data)

    return found


def scan() -> list[dict]:
    """Сканирует обе папки модулей, возвращает список манифестов
    (каждый — уже провалидированный dict из manifest.load())."""
    modules = _scan_dir(SYSTEM_MODULES_DIR) + _scan_dir(USER_MODULES_DIR)
    send_log("INFO", "modules_scanned", {"count": len(modules)})
    return modules


def build_registry(modules: list[dict]) -> dict:
    """Разворачивает список манифестов модулей в плоский реестр
    действий — этим реестром пользуется планировщик (achieve() из
    core_design.md) для поиска "кто производит X".

    {action_command: {"module", "module_dir", "entrypoint", "needs", "produces"}}
    """
    registry = {}
    for mod in modules:
        for action in mod["actions"]:
            command = action["command"]
            if command in registry:
                send_log("WARNING", "duplicate_action", {
                    "command": command,
                    "modules": [registry[command]["module"], mod["name"]],
                })
                print(f"[module_init] предупреждение: команда {command!r} объявлена больше "
                      f"чем в одном модуле ({registry[command]['module']} и {mod['name']}), "
                      f"использую первую найденную")
                continue

            registry[command] = {
                "module": mod["name"],
                "module_dir": mod["_module_dir"],
                "entrypoint": mod["entrypoint"]["command"],
                "needs": action["needs"],
                "produces": action["produces"],
            }

    return registry


if __name__ == "__main__":
    found_modules = scan()
    action_registry = build_registry(found_modules)

    print(f"[module_init] найдено модулей: {len(found_modules)}, действий: {len(action_registry)}")
    for command, info in action_registry.items():
        print(f"  {command}: needs={info['needs']} produces={info['produces']} (модуль {info['module']!r}, {info['module_dir']})")
