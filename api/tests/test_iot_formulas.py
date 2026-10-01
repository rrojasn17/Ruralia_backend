from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from modules.mod_caficultura.iot_formulas import (
    calculated_series,
    evaluate_formula,
    parse_formula,
)
from modules.mod_caficultura.schema_iot import IoTWidgetIn


def test_temperature_conversion_and_multivariable_math():
    assert evaluate_formula(parse_formula("x1 * 9 / 5 + 32", {"x1"}), {"x1": 25}) == 77
    assert (
        evaluate_formula(
            parse_formula("sqrt(x1 ** 2) + max(x2, 2)", {"x1", "x2"}),
            {"x1": -3, "x2": 4},
        )
        == 7
    )
    assert (
        evaluate_formula(
            parse_formula("exp(x1) + log(x2)", {"x1", "x2"}), {"x1": 0, "x2": 1}
        )
        == 1
    )


@pytest.mark.parametrize(
    "expression",
    [
        '__import__("os")',
        "x1.__class__",
        "x1[0]",
        "[x1]",
        "x1 if True else 0",
        "unknown + x1",
        "True + x1",
        "sqrt(x1, 2)",
        "1e309 * x1",
        "x1 ^ 2",
        "2 + 3",
        "max(x1)",
        "x1; x1",
    ],
)
def test_formula_rejects_executable_or_invalid_input(expression):
    with pytest.raises(ValueError):
        parse_formula(expression, {"x1"})


@pytest.mark.parametrize(
    "expression,value",
    [
        ("1 / x1", 0),
        ("sqrt(x1)", -1),
        ("exp(x1)", 1000),
        ("x1 ** 1000", 3),
        ("x1 ** 0.5", -1),
    ],
)
def test_invalid_results_are_missing_not_zero(expression, value):
    assert evaluate_formula(parse_formula(expression, {"x1"}), {"x1": value}) is None


def test_alignment_uses_previous_values_within_tolerance_and_never_future():
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)

    def row(node, minute, data):
        return SimpleNamespace(
            node_id=node, recorded_at=now + timedelta(minutes=minute), data=data
        )

    widget = dict(
        id="derived",
        formula="x1 + x2",
        inputs={
            "x1": {"node_id": 1, "variable_id": "temp"},
            "x2": {"node_id": 2, "variable_id": "rh"},
        },
        max_gap_minutes=5,
    )
    rows = [
        row(1, 0, {"temp": 10}),
        row(2, 1, {"rh": 20}),
        row(1, 2, {"temp": 11}),
        row(1, 10, {"temp": 12}),
    ]
    result = calculated_series([widget], rows)["derived"]
    assert result["points"] == [{"timestamp": now + timedelta(minutes=2), "value": 31}]
    assert result["skipped"] == 2
    widget["max_gap_minutes"] = 0
    assert calculated_series([widget], rows)["derived"]["points"] == []


def test_null_boolean_and_nonfinite_readings_are_not_measurements():
    now = datetime.now(timezone.utc)
    widget = dict(
        id="f", formula="x1 + 1", inputs={"x1": {"node_id": 1, "variable_id": "temp"}}
    )
    rows = [
        SimpleNamespace(node_id=1, recorded_at=now, data={"temp": value})
        for value in (None, True, float("nan"))
    ]
    assert calculated_series([widget], rows)["f"]["points"] == []


def test_formula_schema_rejects_invalid_sources_and_bounds():
    base = dict(
        id="f",
        node_id=1,
        variable_id="temp",
        formula="x1",
        inputs={"x1": {"node_id": 1, "variable_id": "temp"}},
    )
    assert IoTWidgetIn(**base).max_gap_minutes == 15
    with pytest.raises(ValueError):
        IoTWidgetIn(**{**base, "node_id": 2})
    with pytest.raises(ValueError):
        IoTWidgetIn(**{**base, "minimum": 100, "maximum": 0})
    with pytest.raises(ValueError):
        IoTWidgetIn(**{**base, "max_gap_minutes": -1})


def test_extremely_large_numeric_literal_is_a_validation_error():
    with pytest.raises(ValueError):
        parse_formula("9" * 400 + " + x1", {"x1"})
