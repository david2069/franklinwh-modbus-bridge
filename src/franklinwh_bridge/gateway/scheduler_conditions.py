"""Scheduler v2 condition-tree evaluator — a pure, controller-free function.

One shared ALL/ANY tree drives BOTH a job's entry gate (must be true at
fire-time) and its exit criteria (while active, first true terminates the
window). The evaluator takes a *snapshot* dict — sensor id → live value, read
once per tick by ``scheduler_sensors`` — so a nested tree evaluates against a
single consistent view.

Shapes (JSON-serialisable, stored on the ``schedules`` row):

    ConditionTree { match: "ALL" | "ANY", conditions: (Condition | ConditionTree)[] }
    Condition     { sensor: str, op: str, value: ..., value2?: number }

Operators: ``<  <=  ==  !=  >=  >  between  in  not_in  like  not_like``.
``between`` is inclusive and uses ``value``/``value2`` as the (order-independent)
bounds. ``in`` takes a list — a JSON array or a delimited string, so
``Battery SoC in (10,20,30,40,50)`` can be typed as it reads — and compares
members with the same numeric-aware equality as ``==``. ``like`` is a
case-insensitive wildcard match (``*``/``%`` for many, ``?``/``_`` for one) and
falls back to *contains* when the pattern has no wildcard.

``in`` pairs with the engine's edge-triggered one-shot HA actions: a job
conditioned on ``soc in (10,20,...)`` notifies once per level entered and
re-arms when the level is left, rather than repeating every tick.

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
OPERATORS = frozenset({
    "<", "<=", "==", "!=", ">=", ">", "between",
    "in", "not_in", "like", "not_like",
})

#: Members of an ``in`` list may be written as a delimited string. Comma is the
#: documented separator; semicolon and newline are accepted because a user
#: pasting a list from elsewhere should not have to reformat it.
_LIST_SEPARATORS = ",;\n"

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


def _members(value: Any) -> list[Any]:
    """The members of an ``in`` list.

    Accepts a real JSON array, or a delimited string — ``"10,20,30"`` and
    ``"(10, 20, 30)"`` both work, because the natural way to write this by hand
    is the way you'd say it. Surrounding brackets are stripped, blanks dropped.
    """
    if isinstance(value, (list, tuple, set)):
        return [v for v in value if v is not None and v != ""]
    if isinstance(value, str):
        s = value.strip()
        if len(s) >= 2 and s[0] in "([{" and s[-1] in ")]}":
            s = s[1:-1]
        for sep in _LIST_SEPARATORS[1:]:
            s = s.replace(sep, _LIST_SEPARATORS[0])
        return [p.strip().strip("'\"") for p in s.split(_LIST_SEPARATORS[0]) if p.strip()]
    if value is None:
        return []
    return [value]


def _like(live: Any, pattern: Any) -> bool:
    """Case-insensitive wildcard match against a string sensor.

    ``*`` and ``%`` both mean "any run of characters" and ``?``/``_`` mean "any
    one" — SQL's LIKE and shell globbing are the two spellings people arrive
    with, and silently matching nothing because they guessed the other dialect
    is a poor lesson. A pattern with no wildcard at all is treated as
    *contains*, since "like" reads that way to almost everyone.
    """
    if pattern is None:
        return False
    text = str(live)
    pat = str(pattern)
    if not any(ch in pat for ch in "*%?_"):
        return pat.casefold() in text.casefold()

    import fnmatch

    glob = pat.replace("%", "*").replace("_", "?")
    return fnmatch.fnmatch(text.casefold(), glob.casefold())


def _apply_op(op: str, live: Any, value: Any, value2: Any) -> bool:
    """Evaluate one operator. ``None`` live value fails closed for every op."""
    if live is None:
        return False
    if op == "==":
        return _eq(live, value)
    if op == "!=":
        return not _eq(live, value)
    if op in ("in", "not_in"):
        members = _members(value)
        # An empty list matches nothing. `not_in ()` is therefore True, which is
        # consistent — but an empty `in` must never read as "any", or a cleared
        # field would silently arm the condition for every value.
        hit = any(_eq(live, m) for m in members)
        return hit if op == "in" else not hit
    if op in ("like", "not_like"):
        hit = _like(live, value)
        return hit if op == "like" else not hit
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
    value2 = node.get("value2") if isinstance(node, dict) else None
    live = snapshot.get(sensor)

    # RHS is a literal Value by default, or a Lookup of another sensor's live
    # value ("value_kind":"sensor" + "value_sensor":<id>) for sensor-to-sensor
    # comparisons (e.g. import_price > export_price). A Lookup whose RHS sensor is
    # unavailable (None) fails closed, like a None LHS.
    value_kind = (node.get("value_kind") if isinstance(node, dict) else None) or "value"
    rhs_sensor = node.get("value_sensor") if isinstance(node, dict) else None
    if value_kind == "sensor":
        value = snapshot.get(rhs_sensor)
        result = False if value is None else _apply_op(op, live, value, value2)
    else:
        value = node.get("value") if isinstance(node, dict) else None
        result = _apply_op(op, live, value, value2)

    item: dict[str, Any] = {
        "sensor": sensor,
        "op": op,
        "value": value,  # for a Lookup this is the resolved live RHS value
        "live_value": live,
        "result": result,
    }
    if value_kind == "sensor":
        item["value_kind"] = "sensor"
        item["value_sensor"] = rhs_sensor
    if op == "between":
        item["value2"] = value2
    # Echo a caller-supplied leaf id so the UI can map this trace row back to the
    # specific condition row it came from (per-condition failure highlighting).
    cid = node.get("cid") if isinstance(node, dict) else None
    if cid is not None:
        item["cid"] = cid
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
