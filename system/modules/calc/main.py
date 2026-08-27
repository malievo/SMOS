"""
main.py — модуль "calc": настоящий калькулятор выражений.

Разбирает выражение через ast и вычисляет сам, обходя узлы дерева —
никакого eval(). Разрешены только арифметика, скобки, унарный минус,
несколько функций и констант из math. Всё остальное (имена, вызовы
чужих функций, атрибуты, индексация) — отказ.

  python3 main.py evaluate '{"expression": "2 + 2 * (10 - 4)"}'
  -> {"status": "ok", "data": {"calc_result": {"expression": "...", "value": 14}}}

Если выражение не передали — отвечает status "missing", чтобы планировщик
мог добыть его отдельно.
"""

import ast
import json
import math
import operator
import sys

from log_client import send_log

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}
_ALLOWED_NAMES = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}
_ALLOWED_FUNCS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "min": min,
    "max": max,
}

# Потолок на показатель степени — чтобы "9**9**9" не подвесил процесс.
_MAX_POW_EXPONENT = 1000


class CalcError(Exception):
    """Выражение синтаксически разобрано, но содержит что-то запрещённое
    или невычислимое."""


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise CalcError(f"недопустимая константа: {node.value!r}")

    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _BIN_OPS:
            raise CalcError(f"оператор {op_type.__name__} не разрешён")
        left, right = _eval(node.left), _eval(node.right)
        if op_type is ast.Pow and isinstance(right, (int, float)) and right > _MAX_POW_EXPONENT:
            raise CalcError("слишком большой показатель степени")
        return _BIN_OPS[op_type](left, right)

    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _UNARY_OPS:
            raise CalcError(f"унарный оператор {op_type.__name__} не разрешён")
        return _UNARY_OPS[op_type](_eval(node.operand))

    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_NAMES:
            return _ALLOWED_NAMES[node.id]
        raise CalcError(f"имя {node.id!r} не разрешено")

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise CalcError("вызов такой функции не разрешён")
        if node.keywords:
            raise CalcError("именованные аргументы не поддерживаются")
        args = [_eval(a) for a in node.args]
        return _ALLOWED_FUNCS[node.func.id](*args)

    raise CalcError(f"конструкция {type(node).__name__} не разрешена")


def evaluate(expression: str) -> dict:
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise CalcError(f"синтаксическая ошибка: {e.msg}") from e

    value = _eval(tree)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return {"expression": expression, "value": value}


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else None
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    if command != "evaluate":
        print(json.dumps({"status": "error", "error": f"неизвестная команда {command!r}"}, ensure_ascii=False))
        return

    expression = params.get("expression")
    if not expression or not str(expression).strip():
        print(json.dumps({"status": "missing", "missing": ["expression"]}, ensure_ascii=False))
        return

    try:
        result = evaluate(str(expression))
    except CalcError as e:
        send_log("WARNING", "calc_rejected", {"expression": str(expression), "reason": str(e)})
        print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
        return
    except (ValueError, ZeroDivisionError, OverflowError, TypeError) as e:
        send_log("WARNING", "calc_failed", {"expression": str(expression), "reason": str(e)})
        print(json.dumps({"status": "error", "error": f"не удалось вычислить: {e}"}, ensure_ascii=False))
        return

    send_log("INFO", "calc_evaluated", {"expression": result["expression"], "value": result["value"]})
    print(json.dumps({"status": "ok", "data": {"calc_result": result}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
