"""Why the battery is doing what it is, and whether import is billable.

The engine already tracked which entry owned a target, but only internally —
"why is it discharging?" was answerable solely by reading the activity log.
"""

from __future__ import annotations

import datetime as dt

from franklinwh_bridge.gateway.scheduler_sensors import snapshot

FREE_WEEKDAY_MIDDAY = [{
    "months": [], "days": [0, 1, 2, 3, 4],
    "start": "11:00", "end": "13:00", "billable": False,
}]

MON_NOON = dt.datetime(2026, 9, 14, 12, 0)
SAT_NOON = dt.datetime(2026, 9, 19, 12, 0)
MON_9AM = dt.datetime(2026, 9, 14, 9, 0)


# ── Dispatch reason ───────────────────────────────────────────


def test_schedule_in_control_is_named():
    snap = snapshot({
        "dispatch_active": True, "dispatch_source": "schedule",
        "dispatch_entry": "Battery Bonus TOU Export",
        "dispatch_action": "Force Discharge",
        "dispatch_since_min": 12.5, "dispatch_expires_min": 77.5,
    }, MON_NOON)

    assert snap["dispatch.active"] is True
    assert snap["dispatch.source"] == "schedule"
    assert snap["dispatch.entry"] == "Battery Bonus TOU Export"
    assert snap["dispatch.action"] == "Force Discharge"
    assert snap["dispatch.since_min"] == 12.5
    assert snap["dispatch.expires_min"] == 77.5


def test_manual_command_is_distinguished_from_a_schedule():
    snap = snapshot({"dispatch_active": True, "dispatch_source": "manual"}, MON_NOON)

    assert snap["dispatch.active"] is True
    assert snap["dispatch.source"] == "manual"
    assert snap["dispatch.entry"] is None


def test_no_override_means_the_gateway_runs_its_own_mode():
    """source='none' is information, not absence: the bridge is deliberately
    not overriding, which is what the timeline draws as 'no block'."""
    snap = snapshot({"dispatch_active": False, "dispatch_source": "none"}, MON_NOON)

    assert snap["dispatch.active"] is False
    assert snap["dispatch.source"] == "none"


# ── Billable import ───────────────────────────────────────────


def test_free_window_reads_as_not_billable():
    """The user's case: every weekday 11:00-13:00 is non-billable."""
    snap = snapshot({"tariff_import_windows": FREE_WEEKDAY_MIDDAY}, MON_NOON)

    assert snap["tariff.import_window_active"] is True
    assert snap["tariff.import_billable"] is False


def test_the_same_hour_at_the_weekend_is_billable():
    snap = snapshot({"tariff_import_windows": FREE_WEEKDAY_MIDDAY}, SAT_NOON)

    assert snap["tariff.import_billable"] is True


def test_outside_the_window_is_billable():
    snap = snapshot({"tariff_import_windows": FREE_WEEKDAY_MIDDAY}, MON_9AM)

    assert snap["tariff.import_billable"] is True


def test_unconfigured_is_unknown_not_free():
    """None, never False. A misconfiguration must not invite an automation to
    charge from the grid believing it's free."""
    assert snapshot({}, MON_NOON)["tariff.import_billable"] is None


def test_a_billable_window_does_not_make_import_free():
    """A declared window with billable=true is still a window — it just costs."""
    windows = [{**FREE_WEEKDAY_MIDDAY[0], "billable": True}]

    snap = snapshot({"tariff_import_windows": windows}, MON_NOON)

    assert snap["tariff.import_window_active"] is True
    assert snap["tariff.import_billable"] is True


def test_billable_defaults_to_true_when_the_flag_is_absent():
    """An older window without the key must not silently become free."""
    windows = [{"months": [], "days": [], "start": "11:00", "end": "13:00"}]

    assert snapshot({"tariff_import_windows": windows}, MON_NOON)["tariff.import_billable"] is True
