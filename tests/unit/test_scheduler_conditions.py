"""Unit tests for the scheduler v2 condition-tree evaluator (pure, no hardware)."""

import pytest

from franklinwh_bridge.gateway.scheduler_conditions import (
    MATCH_ALL,
    MATCH_ANY,
    evaluate,
    evaluate_dict,
)

SNAP = {
    "battery.soc_pct": 55,
    "solar.power_w": 3200,
    "grid.connected": True,
    "mode.name": "tou",
    "mode.raw": 3,
    "unmapped.sensor": None,  # a sensor with no live value (fails closed)
}


def cond(sensor, op, value, value2=None):
    c = {"sensor": sensor, "op": op, "value": value}
    if value2 is not None:
        c["value2"] = value2
    return c


# ── None tree = no gate ───────────────────────────────────────


def test_none_tree_is_true_no_trace():
    result, trace = evaluate(None, SNAP)
    assert result is True
    assert trace == []


# ── operator matrix ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("op", "value", "expected"),
    [
        ("<", 60, True),
        ("<", 55, False),
        ("<=", 55, True),
        ("<=", 54, False),
        (">", 50, True),
        (">", 55, False),
        (">=", 55, True),
        (">=", 56, False),
        ("==", 55, True),
        ("==", 56, False),
        ("!=", 56, True),
        ("!=", 55, False),
    ],
)
def test_numeric_operators(op, value, expected):
    tree = {"match": MATCH_ALL, "conditions": [cond("battery.soc_pct", op, value)]}
    result, _ = evaluate(tree, SNAP)
    assert result is expected


@pytest.mark.parametrize(
    ("value", "value2", "expected"),
    [
        (50, 60, True),  # 55 in [50,60]
        (55, 60, True),  # inclusive low bound
        (40, 55, True),  # inclusive high bound
        (10, 20, False),  # out of range
        (60, 50, True),  # bounds order-independent
    ],
)
def test_between_inclusive_and_orderless(value, value2, expected):
    tree = {"conditions": [cond("battery.soc_pct", "between", value, value2)]}
    result, _ = evaluate(tree, SNAP)
    assert result is expected


def test_between_missing_value2_fails_closed():
    tree = {"conditions": [{"sensor": "battery.soc_pct", "op": "between", "value": 50}]}
    result, _ = evaluate(tree, SNAP)
    assert result is False


# ── enum / bool equality ──────────────────────────────────────


def test_enum_string_equality():
    assert evaluate({"conditions": [cond("mode.name", "==", "tou")]}, SNAP)[0] is True
    assert evaluate({"conditions": [cond("mode.name", "==", "backup")]}, SNAP)[0] is False
    assert evaluate({"conditions": [cond("mode.name", "!=", "backup")]}, SNAP)[0] is True


def test_bool_equality():
    assert evaluate({"conditions": [cond("grid.connected", "==", True)]}, SNAP)[0] is True
    assert evaluate({"conditions": [cond("grid.connected", "==", False)]}, SNAP)[0] is False


def test_numeric_string_value_coerced():
    # value arriving as a JSON string still compares numerically
    assert evaluate({"conditions": [cond("battery.soc_pct", ">", "50")]}, SNAP)[0] is True


# ── missing / None sensor fails closed ────────────────────────


@pytest.mark.parametrize("op", ["<", "<=", "==", "!=", ">=", ">", "between"])
def test_none_live_value_fails_closed_all_ops(op):
    tree = {"conditions": [cond("unmapped.sensor", op, 10, 20)]}
    result, trace = evaluate(tree, SNAP)
    assert result is False
    assert trace[0]["live_value"] is None
    assert trace[0]["result"] is False


def test_absent_sensor_fails_closed():
    tree = {"conditions": [cond("does.not.exist", ">", 0)]}
    assert evaluate(tree, SNAP)[0] is False


def test_unknown_operator_fails_closed():
    tree = {"conditions": [{"sensor": "battery.soc_pct", "op": "~=", "value": 55}]}
    assert evaluate(tree, SNAP)[0] is False


# ── ALL / ANY semantics ───────────────────────────────────────


def test_match_all():
    tree = {
        "match": MATCH_ALL,
        "conditions": [
            cond("battery.soc_pct", ">", 50),
            cond("solar.power_w", ">", 1000),
        ],
    }
    assert evaluate(tree, SNAP)[0] is True
    tree["conditions"][1] = cond("solar.power_w", ">", 9999)
    assert evaluate(tree, SNAP)[0] is False


def test_match_any():
    tree = {
        "match": MATCH_ANY,
        "conditions": [
            cond("battery.soc_pct", ">", 9999),  # false
            cond("solar.power_w", ">", 1000),  # true
        ],
    }
    assert evaluate(tree, SNAP)[0] is True


def test_empty_all_is_true_empty_any_is_false():
    assert evaluate({"match": MATCH_ALL, "conditions": []}, SNAP)[0] is True
    assert evaluate({"match": MATCH_ANY, "conditions": []}, SNAP)[0] is False


def test_default_match_is_all():
    # no "match" key → ALL
    tree = {"conditions": [cond("battery.soc_pct", ">", 50), cond("solar.power_w", ">", 9999)]}
    assert evaluate(tree, SNAP)[0] is False


# ── nesting ───────────────────────────────────────────────────


def test_nested_tree():
    # soc > 50 AND (solar > 9999 OR mode == tou)
    tree = {
        "match": MATCH_ALL,
        "conditions": [
            cond("battery.soc_pct", ">", 50),
            {
                "match": MATCH_ANY,
                "conditions": [
                    cond("solar.power_w", ">", 9999),
                    cond("mode.name", "==", "tou"),
                ],
            },
        ],
    }
    result, trace = evaluate(tree, SNAP)
    assert result is True
    # trace flattens every leaf that was visited (no short-circuit)
    assert len(trace) == 3


# ── trace shape / evaluate_dict ───────────────────────────────


def test_trace_shape_and_no_short_circuit():
    tree = {
        "match": MATCH_ALL,
        "conditions": [
            cond("battery.soc_pct", "<", 10),  # false, but still traced
            cond("solar.power_w", ">", 1000),
        ],
    }
    result, trace = evaluate(tree, SNAP)
    assert result is False
    assert len(trace) == 2  # both visited despite the first failing under ALL
    first = trace[0]
    assert set(first) == {"sensor", "op", "value", "live_value", "result"}
    assert first["live_value"] == 55


def test_between_trace_includes_value2():
    tree = {"conditions": [cond("battery.soc_pct", "between", 50, 60)]}
    _, trace = evaluate(tree, SNAP)
    assert trace[0]["value2"] == 60


def test_evaluate_dict_contract_shape():
    tree = {"conditions": [cond("battery.soc_pct", ">=", 50)]}
    out = evaluate_dict(tree, SNAP)
    assert out["result"] is True
    assert isinstance(out["per_condition"], list)
    assert out["per_condition"][0]["sensor"] == "battery.soc_pct"
