"""Auto-notify triggers (owner, 2026-11-13): the Direct Connect bridge's rows, defaults and
wording, and off-grid with how long the battery lasts at current usage."""

from __future__ import annotations

import json

import pytest

from franklinwh_bridge import notify_triggers as nt
from franklinwh_bridge.actionable import _TEMPLATES

ON = {"connection_state": "Connected", "soc": 40.0, "wh_available": 6800, "home_load_ext": 1200}
OFF = {**ON, "connection_state": "Disconnected"}


def cfg(**changes) -> dict:
    return nt.apply(nt.merged(None), {"triggers": changes})


def test_rows_and_defaults_match_the_direct_connect_bridge():
    c = nt.merged(None)
    assert list(c["triggers"]) == [
        "aGate_offline", "off_grid", "dispatch", "soc_low", "soc_full", "bridge",
        "schedule_failed", "schedule_missed", "schedule_aborted", "schedule_no_reply"]
    on = {k for k, t in c["triggers"].items() if t["enabled"]}
    assert on == {"aGate_offline", "off_grid", "dispatch", "bridge", "schedule_failed",
                  "schedule_missed", "schedule_no_reply"}
    assert c["triggers"]["soc_low"]["threshold"] == 20
    assert c["triggers"]["soc_full"]["threshold"] == 100
    assert all(t["target"] == {"type": "all"} for t in c["triggers"].values())


def test_off_grid_says_how_long_the_battery_lasts_at_current_usage():
    out = nt.evaluate(ON, OFF, nt.merged(None))
    assert out == [("off_grid", {"figures": "Battery 40%, 6.8 kWh left; home using 1.2 kW: "
                                            "lasts about 5 h 40 min at current usage."})]
    _, msg = _TEMPLATES.render("off_grid", gateway="Shed", **out[0][1])
    assert msg.startswith("Gateway went OFF-GRID [Shed] — running on battery. Battery 40%")


def test_off_grid_without_load_gives_no_estimate():
    assert nt.off_grid_figures({**OFF, "home_load_ext": 10}) == \
        "Battery 40%, 6.8 kWh left; home using 0.0 kW."
    assert nt.off_grid_figures({"connection_state": "Disconnected"}) == ""


def test_back_on_grid():
    assert nt.evaluate(OFF, ON, nt.merged(None)) == [("on_grid", {})]


def test_first_poll_and_unknown_state_announce_nothing():
    assert nt.evaluate(None, OFF, nt.merged(None)) == []
    assert nt.evaluate({"soc": 40}, OFF, nt.merged(None)) == []


def test_switched_off_trigger_and_master_send_nothing():
    assert nt.evaluate(ON, OFF, cfg(off_grid={"enabled": False})) == []
    off = nt.apply(nt.merged(None), {"master": False})
    assert nt.evaluate(ON, OFF, off) == []


def test_soc_thresholds_fire_once_on_crossing():
    c = cfg(soc_low={"enabled": True, "threshold": 30}, soc_full={"enabled": True, "threshold": 90})
    assert nt.evaluate({"soc": 31}, {"soc": 29.6}, c) == [("soc_low", {"threshold": 30, "soc": 30})]
    assert nt.evaluate({"soc": 29}, {"soc": 28}, c) == []
    assert nt.evaluate({"soc": 89}, {"soc": 90}, c) == [("soc_full", {"threshold": 90, "soc": 90})]
    # Off by default.
    assert nt.evaluate({"soc": 25}, {"soc": 15}, nt.merged(None)) == []


@pytest.mark.parametrize("patch", [
    {"triggers": {"nope": {"enabled": True}}},
    {"triggers": {"off_grid": {"enabled": "yes"}}},
    {"triggers": {"off_grid": {"threshold": 5}}},
    {"triggers": {"soc_low": {"threshold": 101}}},
    {"triggers": {"soc_low": {"target": {"type": "role", "role": "admin"}}}},
    {"triggers": {"soc_low": {"target": {"type": "device", "ids": []}}}},
    {"master": "on"},
])
def test_a_bad_save_is_refused(patch):
    with pytest.raises(ValueError):
        nt.apply(nt.merged(None), patch)


def test_saved_config_round_trips_and_ignores_junk():
    c = cfg(soc_low={"enabled": True, "threshold": 15,
                     "target": {"type": "device", "ids": ["d1"]}})
    back = nt.merged(json.loads(json.dumps(nt.stored(c))))
    assert back == c
    junk = {"master": "x", "triggers": {"gone": {}, "soc_low": {"threshold": True,
                                                              "target": {"type": "x"}}}}
    assert nt.merged(junk) == nt.merged(None)


def test_stats_count_both_directions():
    rows = [("off_grid", 3, 100.0), ("on_grid", 2, 200.0), ("bridge_stopped", 1, 50.0)]
    assert nt.stats_by_trigger(rows) == {"off_grid": {"count": 5, "last_ts": 200.0},
                                         "bridge": {"count": 1, "last_ts": 50.0}}


def test_every_event_renders_with_its_variables():
    for e in nt.EVENTS:
        title, msg = _TEMPLATES.render(e.key, gateway="Shed", **{v: "x" for v in e.variables})
        assert "{" not in title + msg, e.key
