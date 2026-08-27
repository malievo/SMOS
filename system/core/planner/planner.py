"""
planner.py — разрешение цели через реестр модулей (backward chaining).

Алгоритм и все решения по нему — см. ../core_design.md. Это не демон и
не класс — набор функций, которые вызывает поток задачи (создаёт core
на каждую цель от SWL, см. core_design.md про модель "поток на
задачу"). SWL ещё не существует — цель и начальное состояние (state)
пока задаются вручную (см. __main__), это осознанно: achieve() не
должен и не обязан знать, откуда взялась цель — она может прийти от
SWL, из теста, откуда угодно, ему всё равно.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_DIR = SCRIPT_DIR.parent
MODULE_INIT_DIR = CORE_DIR / "module_init"
for path in (SCRIPT_DIR, CORE_DIR, MODULE_INIT_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import registry as module_registry  # noqa: E402
from log_client import send_log  # noqa: E402

DEFAULT_TIMEOUT_SEC = 5


class PlanningError(Exception):
    """Цель не удалось разрешить — никто не производит нужный ключ,
    обнаружен цикл, или модуль в итоге вернул ошибку."""


def find_action_that_produces(target_key: str, graph: dict):
    """Первый найденный action (command, info), чей produces содержит
    target_key. Без выбора среди нескольких кандидатов — если их
    несколько, побеждает первый по порядку в graph (см. открытый
    вопрос "несколько модулей производят одно и то же" в
    core_design.md — сознательно не решается здесь)."""
    for command, action in graph.items():
        if target_key in action["produces"]:
            return command, action
    return None


def call_module(action: dict, command: str, params: dict) -> dict:
    """Запускает модуль как процесс, передаёт command+params, читает
    JSON-ответ из stdout. Протокол — см. ../module_init/manifest_design.md.

    В окружении модуля ставится SMOS_MODULE_DATA — абсолютный путь к его
    личной папке данных (`user/module_data/<name>/`). Саму папку заводит
    module_init при обнаружении модуля (registry._scan_dir); здесь
    mkdir(exist_ok=True) — только страховка на случай, если папку удалили
    при работе или модуль вызывают в тесте без предварительного scan().
    Всё, что модуль хочет сохранить между вызовами, он кладёт туда;
    папка отдельно от кода модуля и переживает его
    удаление/переустановку (см. manifest_design.md, раздел "Хранение
    данных модуля"). Что и как хранить внутри — дело модуля, система
    только выделяет место."""
    data_dir = Path(action["data_dir"])
    data_dir.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "SMOS_MODULE_DATA": str(data_dir)}

    cmd = [*action["entrypoint"], command, json.dumps(params, ensure_ascii=False)]

    try:
        result = subprocess.run(
            cmd,
            cwd=action["module_dir"],
            env=env,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        send_log("ERROR", "module_timeout", {"module": action["module"], "command": command})
        return {"status": "error", "error": "таймаут"}

    if result.returncode != 0:
        send_log("ERROR", "module_crashed", {
            "module": action["module"], "command": command, "stderr": result.stderr,
        })
        return {"status": "error", "error": f"процесс завершился с кодом {result.returncode}"}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        send_log("ERROR", "module_bad_output", {
            "module": action["module"], "command": command, "stdout": result.stdout,
        })
        return {"status": "error", "error": f"не удалось разобрать ответ модуля: {e}"}


def achieve(target_key: str, state: dict, graph: dict, _resolving: set | None = None):
    """Рекурсивно разрешает target_key, при необходимости вызывая
    другие модули за недостающими данными.

    - state — то, что уже известно (из SWL или из предыдущих шагов),
      пополняется по ходу выполнения.
    - graph — реестр действий, module_init.registry.build_registry().
    - _resolving — защита от циклов в манифестах, не передавать вручную.
    """
    if target_key in state:
        return state[target_key]

    if _resolving is None:
        _resolving = set()
    if target_key in _resolving:
        raise PlanningError(f"цикл при разрешении {target_key!r}")
    _resolving.add(target_key)

    found = find_action_that_produces(target_key, graph)
    if found is None:
        raise PlanningError(f"никто не производит {target_key!r}")
    command, action = found

    params = {}
    for need in action["needs"]:
        params[need] = achieve(need, state, graph, _resolving)

    response = call_module(action, command, params)

    if response.get("status") == "missing":
        # модуль сам, в рантайме, попросил что-то сверх заявленных needs
        for key in response.get("missing", []):
            state[key] = achieve(key, state, graph, _resolving)
        response = call_module(action, command, {**params, **state})

    _resolving.discard(target_key)

    if response.get("status") == "ok":
        state.update(response["data"])
        return state[target_key]

    raise PlanningError(response.get("error", "неизвестная ошибка модуля"))


if __name__ == "__main__":
    modules = module_registry.scan()
    graph = module_registry.build_registry(modules)
    print(f"[planner] в реестре {len(graph)} действий: {list(graph)}")

    # SWL ещё не существует — цель и начальное состояние задаём вручную,
    # чтобы проверить сам планировщик, не дожидаясь разбора фразы.
    goal = "echoed_text"
    initial_state = {"text": "привет, ядро"}
    print(f"\n[planner] цель: {goal!r}, начальное состояние: {initial_state}")
    try:
        result = achieve(goal, dict(initial_state), graph)
        print(f"[planner] результат: {result!r}")
    except PlanningError as e:
        print(f"[planner] не удалось: {e}")

    # Негативный случай: цели без единого способа её получить.
    print("\n[planner] цель без данных и без модуля, который их производит:")
    try:
        achieve("несуществующий_ключ", {}, graph)
    except PlanningError as e:
        print(f"[planner] ожидаемо не удалось: {e}")
