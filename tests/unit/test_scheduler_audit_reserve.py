"""Reserve-SOC condition sensors + schedule audit-trail clarity helpers."""

from __future__ import annotations

from franklinwh_bridge.gateway.scheduler import (
    ScheduleEngine,
    _action_summary,
    _condition_reason,
    _leaf_str,
)
from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog, snapshot


# ── Reserve-SOC sensors ───────────────────────────────────────
def test_reserve_sensors_registered_and_grouped():
    cat = {s["id"]: s for s in sensor_catalog({})}
    for sid in ("battery.reserve_pct", "battery.reserve_self_pct", "battery.reserve_tou_pct"):
        assert sid in cat, f"{sid} missing from catalog"
        assert cat[sid]["group"] == "Battery"
        assert cat[sid]["unit"] == "%"


def test_reserve_current_follows_mode():
    pts = {"self_reserve_pct": 20.0, "tou_reserve_pct": 30.0}
    assert snapshot({**pts, "mode_name": "Time of Use"})["battery.reserve_pct"] == 30.0
    assert snapshot({**pts, "mode_name": "Self-Consumption"})["battery.reserve_pct"] == 20.0
    # unknown/absent mode → self reserve
    assert snapshot(pts)["battery.reserve_pct"] == 20.0
    # falls back to the other when one is absent
    assert snapshot({"tou_reserve_pct": 15.0, "mode_name": "Self"})["battery.reserve_pct"] == 15.0


# ── Audit clarity helpers ─────────────────────────────────────
def test_action_summary():
    assert _action_summary("force_discharge", {"power_pct": 100}) == "Force Discharge @ 100%"
    assert _action_summary("force_charge", {"power_w": 1000}) == "Force Charge @ 1000W"
    assert _action_summary("reserve_self", {"pct": 20}) == "Self-Consumption Reserve 20%"
    assert _action_summary("mode", {"mode": "TOU"}) == "Operating Mode: TOU"
    assert _action_summary("force_standby", {}) == "Force Standby"


def test_condition_reason_shows_range_and_live():
    trace = [
        {"sensor": "battery.soc_pct", "op": "between", "value": 50, "value2": 100,
         "live_value": None, "result": False},
        {"sensor": "grid.connected", "op": "==", "value": 1, "live_value": True, "result": True},
    ]
    reason = _condition_reason("entry gated", trace)
    assert "battery.soc_pct between 50..100 (live=None)" in reason
    assert "grid.connected" not in reason  # passing leaves are omitted


def test_leaf_str_between():
    assert _leaf_str(
        {"sensor": "x", "op": "between", "value": 1, "value2": 2, "live_value": 5}
    ) == "x between 1..2 (live=5)"


def test_target_label_uses_gateway_name_and_mock_flag():
    labels = {"default": "Default Gateway", "Mock GW 1": "FHP 2 (mock)"}
    eng = ScheduleEngine(db=None, resolver=lambda *_: [], gw_label_fn=lambda g: labels.get(g, g))
    assert eng._target_label(("gateway", "default", "default")) == "Default Gateway"
    assert eng._target_label(("gateway", "Mock GW 1", "Mock GW 1")) == "FHP 2 (mock)"
    assert eng._target_label("site") == "site"  # non-tuple passes through
