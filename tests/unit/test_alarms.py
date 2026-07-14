"""Tests for alarm event pairing and grid-mode interval computation."""

from franklinwh_bridge.store.alarms import (
    _FAULT_BITS_M701,
    _M701_ALRM_BITS,
    _WARN_BITS_M701,
    _decode_bitfield,
    compute_grid_mode_intervals,
    pair_alarm_events,
)

# Ground truth: official SunSpec Model 701 "Alrm" bitfield, bit value -> name,
# per sunspec2's model_701.json (the reference implementation of the SunSpec
# Alliance spec), cross-checked against FranklinWH's own PICS certification
# (SM-000028), which marks all 17 as "supported" in this exact order. This is
# intentionally a separate, hand-transcribed copy rather than importing
# sunspec2 directly — sunspec2 is a transitive dependency (via
# franklinwh-modbus), not a declared bridge dependency, so importing it here
# would make this test fragile to unrelated dependency changes.
_OFFICIAL_M701_ALRM_BITS = {
    0: "GroundFault",
    1: "DCOverVoltage",
    2: "ACDisconnect",
    3: "DCDisconnect",
    4: "GridDisconnect",
    5: "CabinetOpen",
    6: "ManualShutdown",
    7: "OverTemp",
    8: "OverFrequency",
    9: "UnderFrequency",
    10: "ACOverVoltage",
    11: "ACUnderVoltage",
    12: "BlownStringFuse",
    13: "UnderTemp",
    14: "MemoryLoss",
    15: "HwTestFailure",
    16: "ManufacturerAlarm",
}


def test_m701_alrm_bits_matches_official_sunspec_spec():
    assert _M701_ALRM_BITS == _OFFICIAL_M701_ALRM_BITS


def test_m701_alrm_bits_has_no_fabricated_names():
    # These three names appeared in the table before the 2026-07-13 fix and
    # do not exist anywhere in the official 17-bit spec.
    assert "InputOverCurrent" not in _M701_ALRM_BITS.values()
    assert "ArcFault" not in _M701_ALRM_BITS.values()
    assert "ThermalDerate" not in _M701_ALRM_BITS.values()


def test_m701_alrm_severity_sets_are_valid_bit_positions():
    # Every bit referenced by the severity sets must be a real, named bit —
    # catches drift if the bit table changes without updating severity too.
    known_bits = set(_M701_ALRM_BITS.keys())
    assert known_bits >= _FAULT_BITS_M701
    assert known_bits >= _WARN_BITS_M701
    assert _FAULT_BITS_M701.isdisjoint(_WARN_BITS_M701)
    assert known_bits == _FAULT_BITS_M701 | _WARN_BITS_M701


def test_m701_alrm_decode_bit1_is_dc_over_voltage_not_input_over_current():
    # The core regression: bit 1 set should decode to the real name, not the
    # fabricated one from the pre-fix off-by-one table.
    assert _decode_bitfield(0b10, _M701_ALRM_BITS) == ["DCOverVoltage"]


def test_m701_alrm_decode_bit16_is_manufacturer_alarm_not_vendor_bit():
    # Bit 16 is officially MANUFACTURER_ALRM, not unknown vendor territory —
    # the vendor-bit fallback should only kick in from bit 17 onward now.
    assert _decode_bitfield(1 << 16, _M701_ALRM_BITS) == ["ManufacturerAlarm"]
    assert _decode_bitfield(1 << 17, _M701_ALRM_BITS) == ["VendorBit17"]


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


def test_pair_multi_bit_episode_severity_is_per_name_not_blanket():
    # PortOverVoltage (bit 0) and PortUnderVoltage (bit 1) are warning-tier;
    # ContactorFault (bit 5) is fault-tier. AlarmTracker.process_sample()
    # records ONE blanket "fault" severity for the whole transition (worst
    # bit wins, for logging purposes) -- pair_alarm_events() must NOT just
    # copy that blanket value onto every name. Each name gets its own,
    # correct severity independent of what else co-occurred.
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
    by_name = {row["name"]: row for row in paired}
    assert set(by_name) == {"PortOverVoltage", "PortUnderVoltage", "ContactorFault"}
    for row in paired:
        assert row["duration_seconds"] == 18.0
    assert by_name["PortOverVoltage"]["severity"] == "warning"
    assert by_name["PortUnderVoltage"]["severity"] == "warning"
    assert by_name["ContactorFault"]["severity"] == "fault"


def test_pair_unclassified_vendor_bit_defaults_to_info_not_borrowed_severity():
    # A VendorBit* name has no known fault/warning classification at all --
    # it must show as "info" (unknown), not inherit "fault" just because a
    # real fault-tier bit (ContactorFault) happened to be set in the same
    # raw-register transition. This is the exact bug a user caught by eye
    # in the Events table: unrelated/unknown bits were all rendering red.
    events = [
        {
            "ts": 300.0, "gateway_id": "default", "source": "M714_PrtAlrms",
            "value_raw": 12345, "alarms_set": "ContactorFault, VendorBit21",
            "alarms_cleared": "", "severity": "fault",
        },
        {
            "ts": 310.0, "gateway_id": "default", "source": "M714_PrtAlrms",
            "value_raw": 0, "alarms_set": "",
            "alarms_cleared": "ContactorFault, VendorBit21",
            "severity": "info",
        },
    ]
    paired = pair_alarm_events(events)
    by_name = {row["name"]: row for row in paired}
    assert by_name["ContactorFault"]["severity"] == "fault"
    assert by_name["VendorBit21"]["severity"] == "info"


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
