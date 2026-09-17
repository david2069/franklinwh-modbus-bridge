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


def test_enums_belong_to_matchlist():
    assert check("mode.name", "matchlist", "TOU,Self-Consumption",
                 {"mode.name": "Self-Consumption"})
    assert not check("mode.name", "matchlist", "TOU,Self-Consumption",
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
    assert {"in", "not_in", "matchlist", "not_matchlist",
            "like", "not_like"} <= OPERATORS


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


# ── ranges inside a list ──────────────────────────────────────


def test_a_range_member_matches_anything_inside_it():
    """The user's example: `in (49-42, 229-230.2)` — voltage bands."""
    lst = "49-42, 229-230.2"

    assert check("v", "in", lst, {"v": 45})
    assert check("v", "in", lst, {"v": 229.5})
    assert not check("v", "in", lst, {"v": 50})
    assert not check("v", "in", lst, {"v": 231})


def test_range_bounds_are_inclusive():
    for v in (42, 49, 229, 230.2):
        assert check("v", "in", "49-42, 229-230.2", {"v": v}), v


def test_bounds_order_does_not_matter():
    """`49-42` is written high-low; it means the same band as `42-49`."""
    assert check("v", "in", "49-42", {"v": 45})
    assert check("v", "in", "42-49", {"v": 45})


def test_ranges_and_exact_levels_mix_in_one_list():
    lst = "10, 20, 49-42, 100"

    assert check("v", "in", lst, {"v": 10})
    assert check("v", "in", lst, {"v": 44})
    assert check("v", "in", lst, {"v": 100})
    assert not check("v", "in", lst, {"v": 15})


def test_a_dash_in_a_name_is_never_a_range():
    """The reason numbers and text are separate operators: on the text path no
    range parsing exists, so `Self-Consumption` cannot become a band."""
    assert check("mode.name", "matchlist", "Self-Consumption",
                 {"mode.name": "Self-Consumption"})
    # ...and the numeric operator refuses it outright rather than guessing.
    assert not check("mode.name", "in", "Self-Consumption", {"mode.name": 5})
    assert not check("mode.name", "in", "TOU,Self-Consumption",
                     {"mode.name": "Self-Consumption"})


def test_negative_bounds_resolve_when_unambiguous():
    assert check("w", "in", "-5000--1000", {"w": -3000})
    assert not check("w", "in", "-5000--1000", {"w": -500})


def test_a_dotdot_range_is_accepted():
    """Unambiguous for negative bounds, and a common way to write a span."""
    assert check("w", "in", "-5000..-1000", {"w": -3000})
    assert check("v", "in", "229..230.2", {"v": 230})


def test_an_ambiguous_member_is_rejected_rather_than_guessed():
    """`1-2-3` has no single sensible reading, so the numeric operator drops
    it instead of picking one."""
    assert not check("v", "in", "1-2-3", {"v": 2})
    assert not check("v", "in", "1-2-3", {"v": 1})
    # As text it is just a string, and matches itself.
    assert check("v", "matchlist", "1-2-3", {"v": "1-2-3"})


def test_a_lone_negative_number_is_still_a_value():
    assert check("w", "in", "-10, 5", {"w": -10})
    assert not check("w", "in", "-10, 5", {"w": -7})


def test_not_in_excludes_a_whole_band():
    """'alert unless voltage is in the normal band' — the obvious use."""
    assert check("v", "not_in", "229-230.2", {"v": 250})
    assert not check("v", "not_in", "229-230.2", {"v": 230})


def test_a_non_numeric_live_value_fails_closed_on_the_numeric_operator():
    """A name is never inside a number list — and `not_in` must not report
    True for it either, or "alert when SoC is outside the band" would fire on
    a sensor returning a string."""
    assert not check("mode.name", "in", "10-20", {"mode.name": "TOU"})
    assert not check("mode.name", "not_in", "10-20", {"mode.name": "TOU"})


def test_a_json_pair_is_also_a_range():
    """So a UI or an import can send bounds structurally."""
    assert check("v", "in", [[42, 49], 100], {"v": 45})
    assert check("v", "in", [[42, 49], 100], {"v": 100})
    assert not check("v", "in", [[42, 49], 100], {"v": 50})


# ── matchlist (text) ──────────────────────────────────────────


def test_matchlist_is_exact_not_contains():
    """`like` is the contains/wildcard operator; matchlist is membership."""
    assert check("m", "matchlist", "TOU,Self-Consumption", {"m": "Self-Consumption"})
    assert not check("m", "matchlist", "Self", {"m": "Self-Consumption"})


def test_matchlist_ignores_case():
    """Device enums arrive with fixed casing; demanding it exactly buys
    nothing, and `like` already sets this precedent."""
    assert check("m", "matchlist", "tou, self-consumption", {"m": "Self-Consumption"})


def test_matchlist_never_interprets_a_range():
    """The whole point of the split — on this path `10-20` is a name."""
    assert not check("v", "matchlist", "10-20", {"v": 15})
    assert check("v", "matchlist", "10-20", {"v": "10-20"})


def test_not_matchlist_is_the_complement():
    assert check("m", "not_matchlist", "TOU", {"m": "Self-Consumption"})
    assert not check("m", "not_matchlist", "TOU,Self-Consumption",
                     {"m": "Self-Consumption"})


def test_matchlist_fails_closed_on_a_missing_sensor():
    assert not check("m", "matchlist", "TOU", {})
    assert not check("m", "not_matchlist", "TOU", {})


def test_matchlist_on_a_number_compares_its_text():
    assert check("n", "matchlist", "10,20", {"n": 20})


def test_an_empty_matchlist_matches_nothing():
    assert not check("m", "matchlist", "", {"m": "TOU"})
