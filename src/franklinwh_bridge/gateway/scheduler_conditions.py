"""Scheduler v2 condition-tree evaluator — a pure, controller-free function.

One shared ALL/ANY tree drives BOTH a job's entry gate (must be true at
fire-time) and its exit criteria (while active, first true terminates the
window). The evaluator takes a *snapshot* dict — sensor id → live value, read
once per tick by ``scheduler_sensors`` — so a nested tree evaluates against a
single consistent view.

Shapes (JSON-serialisable, stored on the ``schedules`` row):

    ConditionTree { match: "ALL" | "ANY", conditions: (Condition | ConditionTree)[] }
    Condition     { sensor: str, op: str, value: ..., value2?: number }

Operators: ``<  <=  ==  !=  >=  >  between``. ``between`` is inclusive and uses
``value``/``value2`` as the (order-independent) bounds.

Two deliberate semantics, both chosen so a scheduler never drives the battery on
missing/indeterminate data:

- **Absent or ``None`` live value → the condition is ``False``** for every
  operator (including ``!=``). A sensor with no live value (e.g. a not-yet-wired
  external source, or an HA entity that is ``unavailable``) simply fails closed
  rather than firing.
- **Vacuous truth follows the match mode**: an empty ``ALL`` is ``True``, an
  empty ``ANY`` is ``False`` (standard).

The evaluator never short-circuits: every leaf is visited so the returned trace
is complete, which powers the FWHAI "Test Verification" button
(``POST /api/scheduler/evaluate``).

All functions here are pure and unit-tested without a controller or hardware.
"""

from __future__ import annotations

from typing import Any

MATCH_ALL = "ALL"
MATCH_ANY = "ANY"
OPERATORS = frozenset({"<", "<=", "==", "!=", ">=", ">", "between"})

# Snapshot: sensor id -> live value (number | str | bool | None).
Snapshot = dict[str, Any]
# One row of the evaluation trace, matching the /api/scheduler/evaluate contract.
Trace = list[dict[str, Any]]


def _coerce_number(x: Any) -> float | None:
    """Best-effort numeric coercion. Bools are NOT numbers here (an ordering
    comparison on a bool is meaningless → treated as non-numeric → fails)."""
    if isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x)
        except ValueError:
            return None
    return None


def _eq(a: Any, b: Any) -> bool:
    """Equality that compares numerically when both sides are numeric, treats
    bools exactly, and otherwise falls back to direct/string equality (enums)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    na, nb = _coerce_number(a), _coerce_number(b)
    if na is not None and nb is not None:
        return na == nb
    return a == b


def _apply_op(op: str, live: Any, value: Any, value2: Any) -> bool:
    """Evaluate one operator. ``None`` live value fails closed for every op."""
    if live is None:
        return False
    if op == "==":
        return _eq(live, value)
    if op == "!=":
        return not _eq(live, value)
    if op in ("<", "<=", ">=", ">", "between"):
        lv = _coerce_number(live)
        rv = _coerce_number(value)
        if lv is None or rv is None:
            return False
        if op == "<":
            return lv < rv
        if op == "<=":
            return lv <= rv
        if op == ">=":
            return lv >= rv
        if op == ">":
            return lv > rv
        # between (inclusive, bounds order-independent)
        rv2 = _coerce_number(value2)
        if rv2 is None:
            return False
        lo, hi = sorted((rv, rv2))
        return lo <= lv <= hi
    return False  # unknown operator → fail closed


def _is_tree(node: Any) -> bool:
    """A node is a nested tree if it carries tree keys rather than a sensor."""
    return isinstance(node, dict) and ("conditions" in node or "match" in node)


def _eval_node(node: Any, snapshot: Snapshot, trace: Trace) -> bool:
    if _is_tree(node):
        return _eval_tree(node, snapshot, trace)
    sensor = node.get("sensor") if isinstance(node, dict) else None
    op = node.get("op") if isinstance(node, dict) else None
    value = node.get("value") if isinstance(node, dict) else None
    value2 = node.get("value2") if isinstance(node, dict) else None
    live = snapshot.get(sensor)
    result = _apply_op(op, live, value, value2)
    item: dict[str, Any] = {
        "sensor": sensor,
        "op": op,
        "value": value,
        "live_value": live,
        "result": result,
    }
    if op == "between":
        item["value2"] = value2
    trace.append(item)
    return result


def _eval_tree(tree: dict, snapshot: Snapshot, trace: Trace) -> bool:
    match = str(tree.get("match", MATCH_ALL)).upper()
    nodes = tree.get("conditions") or []
    # Evaluate every node (no short-circuit) so the trace is complete.
    results = [_eval_node(n, snapshot, trace) for n in nodes]
    if match == MATCH_ANY:
        return any(results)
    return all(results)  # ALL is the default; all([]) is True (vacuous)


def evaluate(tree: dict | None, snapshot: Snapshot) -> tuple[bool, Trace]:
    """Evaluate a condition tree against a snapshot.

    Returns ``(result, per_condition_trace)``. A ``None`` tree means "no gate":
    ``(True, [])`` — used for an entry gate that always allows, and treated as
    "no exit criteria" by the engine (it checks ``exit_conditions is not None``
    before calling here).
    """
    if tree is None:
        return True, []
    trace: Trace = []
    result = _eval_tree(tree, snapshot, trace)
    return result, trace


def evaluate_dict(tree: dict | None, snapshot: Snapshot) -> dict[str, Any]:
    """``evaluate`` in the ``/api/scheduler/evaluate`` response shape."""
    result, trace = evaluate(tree, snapshot)
    return {"result": result, "per_condition": trace}
