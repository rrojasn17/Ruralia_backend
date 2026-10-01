"""Bounded arithmetic expressions; no Python eval or executable user code."""

from __future__ import annotations

import ast
import math
import operator
from bisect import bisect_right
from datetime import timezone


FUNCTIONS = {
    "sqrt": (math.sqrt, 1, 1),
    "exp": (math.exp, 1, 1),
    "log": (math.log, 1, 1),
    "abs": (abs, 1, 1),
    "min": (min, 2, 16),
    "max": (max, 2, 16),
}
OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}


def parse_formula(expression: str, aliases: set[str]):
    if not expression.strip() or len(expression) > 500:
        raise ValueError("La fórmula debe tener entre 1 y 500 caracteres")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, RecursionError) as exc:
        raise ValueError("La fórmula no es válida") from exc
    if sum(1 for _ in ast.walk(tree)) > 120:
        raise ValueError("La fórmula es demasiado compleja")
    used = set()

    def check(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            if abs(node.value) > 1e100 or not math.isfinite(float(node.value)):
                raise ValueError("Constante fuera de rango")
        elif isinstance(node, ast.Name) and node.id in aliases:
            used.add(node.id)
        elif isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
            check(node.left)
            check(node.right)
        elif isinstance(node, ast.UnaryOp) and isinstance(
            node.op, (ast.UAdd, ast.USub)
        ):
            check(node.operand)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in FUNCTIONS
        ):
            _, low, high = FUNCTIONS[node.func.id]
            if node.keywords or not low <= len(node.args) <= high:
                raise ValueError("Cantidad de argumentos de función inválida")
            for argument in node.args:
                check(argument)
        else:
            raise ValueError(
                "Use las variables seleccionadas, números, + - * / **, paréntesis y sqrt, exp, log, abs, min o max"
            )

    check(tree.body)
    if not used:
        raise ValueError("La fórmula debe usar al menos una variable de sensor")
    if used != aliases:
        raise ValueError("La fórmula debe usar todas las variables seleccionadas")
    return tree.body


def evaluate_formula(tree, values):
    def visit(node):
        if isinstance(node, ast.Constant):
            result = float(node.value)
        elif isinstance(node, ast.Name):
            result = values[node.id]
        elif isinstance(node, ast.UnaryOp):
            result = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponente fuera de rango")
            result = OPERATORS[type(node.op)](left, right)
        else:
            result = FUNCTIONS[node.func.id][0](
                *(visit(argument) for argument in node.args)
            )
        if (
            isinstance(result, complex)
            or not math.isfinite(result)
            or abs(result) > 1e100
        ):
            raise ValueError("Resultado fuera de rango")
        return result

    try:
        return visit(tree)
    except (ArithmeticError, ValueError, TypeError, KeyError):
        return None


def calculated_series(widgets, readings):
    """Align on first input; use only previous samples within the chosen tolerance."""
    formulas = [widget for widget in widgets if widget.get("formula")]
    if not formulas:
        return {}
    required = {
        (source["node_id"], source["variable_id"])
        for widget in formulas
        for source in (widget.get("inputs") or {}).values()
    }
    series = {}
    for row in readings:
        timestamp = row.recorded_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        for variable, value in (row.data or {}).items():
            if (
                (row.node_id, variable) in required
                and type(value) in (int, float)
                and math.isfinite(value)
            ):
                series.setdefault((row.node_id, variable), {})[timestamp] = float(value)
    indexed = {key: sorted(points.items()) for key, points in series.items()}
    times = {key: [point[0] for point in points] for key, points in indexed.items()}
    output = {}
    for widget in formulas:
        inputs = widget.get("inputs") or {}
        result = {"points": [], "skipped": 0, "error": None}
        output[widget["id"]] = result
        try:
            tree = parse_formula(widget["formula"], set(inputs))
        except ValueError as exc:
            result["error"] = str(exc)
            continue
        anchor = next(iter(inputs.values()))
        anchor_key = (anchor["node_id"], anchor["variable_id"])
        for timestamp, _ in indexed.get(anchor_key, []):
            values = {}
            for alias, source in inputs.items():
                key = (source["node_id"], source["variable_id"])
                index = bisect_right(times.get(key, []), timestamp) - 1
                if index < 0:
                    break
                sampled_at, value = indexed[key][index]
                if (timestamp - sampled_at).total_seconds() > widget.get(
                    "max_gap_minutes", 15
                ) * 60:
                    break
                values[alias] = value
            value = (
                evaluate_formula(tree, values) if len(values) == len(inputs) else None
            )
            if value is None:
                result["skipped"] += 1
            else:
                result["points"].append({"timestamp": timestamp, "value": value})
    return output
