"""Scheduler v2 condition-tree evaluator — a pure, controller-free function.

One shared ALL/ANY tree drives BOTH a job's entry gate (must be true at
fire-time) and its exit criteria (while active, first true terminates the
window). The evaluator takes a *snapshot* dict — sensor id → live value, read
once per tick by ``scheduler_sensors`` — so a nested tree evaluates against a
single consistent view.

Shapes (JSON-serialisable, stored on the ``schedules`` row):

    ConditionTree { match: "ALL" | "ANY", conditions: (Condition | ConditionTree)[] }
    Condition     { sensor: str, op: str, value: ..., value2?: number }

Operators: ``<  <=  ==  !=  >=  >  between``, the list operators ``in``,
``not_in``, ``matchlist``, ``not_matchlist``, and the pattern operators
``like``, ``not_like``. ``between`` is inclusive and uses ``value``/``value2``
as the (order-independent) bounds.

**Numbers and text have separate list operators, on purpose.** A single ``in``
has to decide, per member, whether a hyphen means "to" or is part of a name —
and gets ``Self-Consumption`` wrong silently, turning a mode name into a
numeric band that matches nothing. Choosing the operator states the intent:

- ``in`` / ``not_in`` — **numeric**. Members are numbers or inclusive ranges:
  ``in (10, 20, 49-42, 229-230.2)``. Bounds are order-independent like
  ``between``, so ``49-42`` and ``42-49`` are the same band, and ``..`` is
  accepted for unambiguous negatives (``-5000..-1000``). A non-numeric live
  value fails closed.
- ``matchlist`` / ``not_matchlist`` — **text**. Members are compared verbatim
  (case-insensitively); no range parsing exists on this path at all, so a dash
  is simply a character: ``mode.name matchlist (TOU, Self-Consumption)``.
- ``like`` / ``not_like`` — case-insensitive wildcard (``*``/``%`` for many,
  ``?``/``_`` for one), falling back to *contains* with no wildcard.

Both list operators accept a JSON array or a delimited string, so a list can be
typed the way it is said. An empty list matches nothing.

``in`` pairs with the engine's edge-triggered one-shot HA actions: a job
conditioned on ``soc in (10,20,...)`` notifies once per level entered and
re-arms when the level is left, rather than repeating every tick. Note it tests
membership, not *crossing* — on a fast-moving sensor a listed value can fall
between polls; a range member is the robust form there.

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
    # Numbers and text are deliberately separate operators. One "in" that
    # inspected each member to decide whether a hyphen meant "to" or was
    # part of a name works until it doesn't, and the failure is silent:
    # "Self-Consumption" quietly becomes a numeric band matching nothing.
    # Choosing the operator states the intent instead of inferring it.
    "in", "not_in",                 # numeric: scalars and a-b ranges
    "matchlist", "not_matchlist",   # text: verbatim names, no ranges
    "like", "not_like",
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


def _list_items(value: Any) -> list[Any]:
    """Split a list into its raw members, interpreting none of them.

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


def _number_members(value: Any) -> list[float | tuple[float, float]]:
    """Numeric members of an ``in`` list: scalars and inclusive ranges.

    A member that is neither is dropped rather than compared as text — this is
    the number operator, and silently matching a name here would re-create the
    ambiguity the split exists to remove.
    """
    out: list[float | tuple[float, float]] = []
    for m in _list_items(value):
        span = _as_range(m)
        if span is not None:
            out.append(span)
            continue
        n = _coerce_number(m)
        if n is not None:
            out.append(n)
    return out


def _text_members(value: Any) -> list[str]:
    """Text members of a ``matchlist``, verbatim.

    No range parsing whatsoever, so ``Self-Consumption`` is a name and a dash
    is just a character in it.
    """
    return [str(m) for m in _list_items(value)]


def _as_range(member: Any) -> tuple[float, float] | None:
    """Parse a list member as an inclusive numeric range, or ``None``.

    Only reachable from the numeric operators. Keeping range parsing out of the
    text operator is the whole point of the split: a hyphen means "to" in a
    number list and means nothing at all in a name list, and no amount of
    cleverness makes one function serve both without guessing.

    ``..`` is accepted alongside ``-`` and an en-dash. For ``-`` every possible
    split position is tried and the range is taken only if **exactly one**
    yields two numbers — so ``-5--1`` resolves (only one split works) while a
    genuinely ambiguous ``1-2-3`` is rejected rather than guessed at.

    Bounds are order-independent, matching ``between``: ``49-42`` is the same
    range as ``42-49``.
    """
    if isinstance(member, (list, tuple)) and len(member) == 2:
        lo, hi = _coerce_number(member[0]), _coerce_number(member[1])
        return (min(lo, hi), max(lo, hi)) if lo is not None and hi is not None else None
    if not isinstance(member, str):
        return None

    text = member.strip()
    for sep in ("..", "–", "—"):   # .., en dash, em dash
        if sep in text:
            left, _, right = text.partition(sep)
            lo, hi = _coerce_number(left.strip()), _coerce_number(right.strip())
            return (min(lo, hi), max(lo, hi)) if lo is not None and hi is not None else None

    hits = []
    for i, ch in enumerate(text):
        if ch != "-" or i == 0:
            continue
        lo = _coerce_number(text[:i].strip())
        hi = _coerce_number(text[i + 1:].strip())
        if lo is not None and hi is not None:
            hits.append((min(lo, hi), max(lo, hi)))
    return hits[0] if len(hits) == 1 else None


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
        lv = _coerce_number(live)
        if lv is None:
            return False  # a name is never inside a number list; fail closed
        # An empty list matches nothing. `not_in ()` is therefore True, which is
        # consistent — but an empty `in` must never read as "any", or a cleared
        # field would silently arm the condition for every value.
        hit = any(
            (m[0] <= lv <= m[1]) if isinstance(m, tuple) else (lv == m)
            for m in _number_members(value)
        )
        return hit if op == "in" else not hit
    if op in ("matchlist", "not_matchlist"):
        # Case-insensitive, like `like` — device enums arrive with fixed casing
        # and making someone match it exactly buys nothing.
        text = str(live).casefold()
        hit = any(text == m.casefold() for m in _text_members(value))
        return hit if op == "matchlist" else not hit
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
