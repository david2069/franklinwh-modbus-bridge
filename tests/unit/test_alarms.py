"""Tests for alarm event pairing and grid-mode interval computation."""

from franklinwh_bridge.store.alarms import (
    compute_grid_mode_intervals,
    pair_alarm_events,
)


def test_pair_simple_set_then_clear():
    events = [
        {
            "ts": 100.0, "gateway_id": "default", "source": "M714_PrtAlrms",
            "value_raw": 1, "alarms_set": "PortOverVoltage", "alarms_cleared": "",
            "severity": "warning",
        },
        {
            "ts": 110.0, "gateway_id": "default", "source": "M714_PrtAlrms",
            "value_raw": 0, "alarms_set": "", "alarms_cleared": "PortOverVoltage",
            "severity": "info",
        },
    ]
    paired = pair_alarm_events(events)
    assert len(paired) == 1
    row = paired[0]
    assert row["name"] == "PortOverVoltage"
    assert row["ts"] == 100.0
    assert row["end_ts"] == 110.0
    assert row["duration_seconds"] == 10.0
    assert row["ongoing"] is False
    assert row["register"] == 41044
    assert row["point"] == "DERMeasureDC.PrtAlrms"
    assert row["severity"] == "warning"


def test_pair_ongoing_alarm_with_no_clear():
    events = [
        {
            "ts": 200.0, "gateway_id": "default", "source": "M714_PrtAlrms",
            "value_raw": 1, "alarms_set": "PortOverVoltage", "alarms_cleared": "",
            "severity": "warning",
        },
    ]
    paired = pair_alarm_events(events)
    assert len(paired) == 1
    row = paired[0]
    assert row["ongoing"] is True
    assert row["end_ts"] == 200.0
    assert row["duration_seconds"] == 0.0


def test_pair_multi_bit_episode_set_and_cleared_together():
    events = [
        {
            "ts": 300.0, "gateway_id": "default", "source": "M714_PrtAlrms",
            "value_raw": 12345, "alarms_set": "PortOverVoltage, PortUnderVoltage, ContactorFault",
            "alarms_cleared": "", "severity": "fault",
        },
        {
            "ts": 318.0, "gateway_id": "default", "source": "M714_PrtAlrms",
            "value_raw": 0, "alarms_set": "",
            "alarms_cleared": "PortOverVoltage, PortUnderVoltage, ContactorFault",
            "severity": "info",
        },
    ]
    paired = pair_alarm_events(events)
    assert len(paired) == 3
    names = {row["name"] for row in paired}
    assert names == {"PortOverVoltage", "PortUnderVoltage", "ContactorFault"}
    for row in paired:
        assert row["duration_seconds"] == 18.0
        assert row["severity"] == "fault"  # severity carried from the SET row


def test_pair_m713_sta_chained_state_transitions():
    # M713_Sta rows: alarms_set = new state, alarms_cleared = previous state.
    events = [
        {
            "ts": 400.0, "gateway_id": "default", "source": "M713_Sta",
            "value_raw": 1, "alarms_set": "Charging", "alarms_cleared": "Idle",
            "severity": "info",
        },
        {
            "ts": 460.0, "gateway_id": "default", "source": "M713_Sta",
            "value_raw": 2, "alarms_set": "Discharging", "alarms_cleared": "Charging",
            "severity": "info",
        },
    ]
    paired = pair_alarm_events(events)
    # "Idle" was cleared but never opened in this window -> synthesized at clear time.
    idle = next(r for r in paired if r["name"] == "Idle")
    assert idle["ts"] == 400.0
    assert idle["duration_seconds"] == 0.0

    charging = next(r for r in paired if r["name"] == "Charging")
    assert charging["ts"] == 400.0
    assert charging["end_ts"] == 460.0
    assert charging["duration_seconds"] == 60.0
    assert charging["register"] == 41039
    assert charging["point"] == "DERStorageCapacity.Sta"

    # "Discharging" opened at 460.0 with no later clear -> ongoing.
    discharging = next(r for r in paired if r["name"] == "Discharging")
    assert discharging["ongoing"] is True


def test_grid_mode_single_sample_blip_has_nonzero_duration():
    points = [
        {"ts": 1000.0, "grid_mode": "Grid Following"},
        {"ts": 1010.0, "grid_mode": "PV Clipped"},
        {"ts": 1020.0, "grid_mode": "Grid Following"},
        {"ts": 1030.0, "grid_mode": "Grid Following"},
    ]
    intervals = compute_grid_mode_intervals(points)
    assert len(intervals) == 1
    row = intervals[0]
    assert row["name"] == "PV Clipped"
    assert row["ts"] == 1010.0
    assert row["end_ts"] == 1020.0
    assert row["duration_seconds"] == 10.0
    assert row["ongoing"] is False
    assert row["register"] == 40078
    assert row["point"] == "DERMeasureAC.DERMode"


def test_grid_mode_default_state_not_reported():
    points = [
        {"ts": 1000.0, "grid_mode": "Grid Following"},
        {"ts": 1010.0, "grid_mode": "Grid Following"},
        {"ts": 1020.0, "grid_mode": "Grid Following (default)"},
    ]
    intervals = compute_grid_mode_intervals(points)
    assert intervals == []


def test_grid_mode_trailing_ongoing_interval():
    points = [
        {"ts": 1000.0, "grid_mode": "Grid Following"},
        {"ts": 1010.0, "grid_mode": "Grid Forming"},
        {"ts": 1020.0, "grid_mode": "Grid Forming"},
    ]
    intervals = compute_grid_mode_intervals(points)
    assert len(intervals) == 1
    row = intervals[0]
    assert row["name"] == "Grid Forming"
    assert row["ts"] == 1010.0
    assert row["end_ts"] == 1020.0
    assert row["duration_seconds"] == 10.0
    assert row["ongoing"] is True
