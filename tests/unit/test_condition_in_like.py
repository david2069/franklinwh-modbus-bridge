"""`in` / `not_in` / `like` / `not_like` condition operators.

The motivating case: "Battery SoC in (10,20,30,40,50)" driving a push
notification at each level. Because the engine's one-shot HA actions are
edge-triggered, that notifies once per level entered and re-arms on leaving —
so the operator only has to answer "is the live value one of these".
"""

from __future__ import annotations

from franklinwh_bridge.gateway.scheduler_conditions import OPERATORS, evaluate


def check(sensor, op, value, snapshot, **extra):
    node = {"sensor": sensor, "op": op, "value": value, **extra}
    ok, _ = evaluate({"match": "ALL", "conditions": [node]}, snapshot)
    return ok


# ── in ────────────────────────────────────────────────────────


def test_the_soc_levels_case():
    """The user's example, at each listed level and between them."""
    levels = "10,20,30,40,50"

    assert check("battery.soc_pct", "in", levels, {"battery.soc_pct": 30})
    assert check("battery.soc_pct", "in", levels, {"battery.soc_pct": 10})
    assert not check("battery.soc_pct", "in", levels, {"battery.soc_pct": 31})
    assert not check("battery.soc_pct", "in", levels, {"battery.soc_pct": 0})


def test_written_the_way_it_reads():
    """`(10, 20, 30)` — brackets and spaces as a person would type them."""
    for spelling in ("(10, 20, 30)", "10,20,30", " 10 , 20 , 30 ", "[10;20;30]"):
        assert check("s", "in", spelling, {"s": 20}), spelling


def test_a_json_array_works_too():
    assert check("s", "in", [10, 20, 30], {"s": 20})
    assert not check("s", "in", [10, 20, 30], {"s": 25})


def test_numeric_comparison_ignores_formatting():
    """A float sensor against integer members, and a string live value."""
    assert check("s", "in", "10,20", {"s": 20.0})
    assert check("s", "in", "10,20", {"s": "20"})
    assert check("s", "in", [20], {"s": 20.000})


def test_it_works_on_enums_not_just_numbers():
    assert check("mode.name", "in", "TOU,Self-Consumption",
                 {"mode.name": "Self-Consumption"})
    assert not check("mode.name", "in", "TOU,Self-Consumption",
                     {"mode.name": "Emergency Backup"})


def test_an_empty_list_matches_nothing():
    """A cleared field must not silently arm for every value."""
    assert not check("s", "in", "", {"s": 20})
    assert not check("s", "in", [], {"s": 20})
    assert not check("s", "in", "()", {"s": 20})


def test_not_in_is_the_complement():
    assert check("s", "not_in", "10,20,30", {"s": 25})
    assert not check("s", "not_in", "10,20,30", {"s": 20})


def test_a_missing_sensor_fails_closed_for_both():
    """The bridge-wide rule: never act on absent data — including not_in,
    where 'the value is not in the list' is superficially true of nothing."""
    assert not check("s", "in", "10,20", {})
    assert not check("s", "not_in", "10,20", {})
    assert not check("s", "in", "10,20", {"s": None})
    assert not check("s", "not_in", "10,20", {"s": None})


# ── like ──────────────────────────────────────────────────────


def test_like_without_a_wildcard_is_contains():
    assert check("mode.name", "like", "Self", {"mode.name": "Self-Consumption"})
    assert not check("mode.name", "like", "Backup", {"mode.name": "Self-Consumption"})


def test_like_is_case_insensitive():
    assert check("mode.name", "like", "self", {"mode.name": "Self-Consumption"})
    assert check("mode.name", "like", "SELF*", {"mode.name": "Self-Consumption"})


def test_both_wildcard_dialects_work():
    """SQL LIKE and shell globbing are the two spellings people arrive with."""
    for pat in ("Self*", "Self%", "Self-Consumptio?", "Self-Consumptio_"):
        assert check("mode.name", "like", pat, {"mode.name": "Self-Consumption"}), pat


def test_a_wildcard_pattern_anchors_like_a_glob():
    """With a wildcard present the match is whole-string, so 'Backup*' does
    not match a value that merely contains 'Backup'."""
    assert not check("m", "like", "Backup*", {"m": "Emergency Backup"})
    assert check("m", "like", "*Backup", {"m": "Emergency Backup"})


def test_not_like_is_the_complement():
    assert check("m", "not_like", "Backup", {"m": "Self-Consumption"})
    assert not check("m", "not_like", "Self", {"m": "Self-Consumption"})


def test_like_on_a_number_compares_its_text():
    """Useful for 'any firmware 10.x' style checks."""
    assert check("fw", "like", "V10*", {"fw": "V10R01B04D00"})
    assert check("n", "like", "3*", {"n": 31})


def test_like_with_no_pattern_fails_closed():
    assert not check("m", "like", None, {"m": "Self-Consumption"})


# ── registration ──────────────────────────────────────────────


def test_the_new_operators_are_registered():
    """OPERATORS gates what the API will accept; an unlisted op is rejected
    before it ever reaches the evaluator."""
    assert {"in", "not_in", "like", "not_like"} <= OPERATORS


def test_an_unknown_operator_still_fails_closed():
    assert not check("s", "matches_regex", "10", {"s": 10})


# ── trace ─────────────────────────────────────────────────────


def test_the_trace_reports_the_list_for_test_verification():
    """The Test Verification button renders this row, so the list the user
    typed has to survive into the trace."""
    _, trace = evaluate(
        {"match": "ALL",
         "conditions": [{"sensor": "battery.soc_pct", "op": "in",
                         "value": "10,20,30", "cid": "c1"}]},
        {"battery.soc_pct": 20},
    )

    assert trace[0]["op"] == "in"
    assert trace[0]["value"] == "10,20,30"
    assert trace[0]["live_value"] == 20
    assert trace[0]["result"] is True
    assert trace[0]["cid"] == "c1"
