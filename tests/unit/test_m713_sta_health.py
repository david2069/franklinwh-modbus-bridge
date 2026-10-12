"""713.Sta is a health enum (0 OK, 1 Warning, 2 Error), decoded the same way
by the poller and the alarm history; older alarm rows are re-labelled on read."""

import time

from franklinwh_bridge.modbus.sunspec_enums import m713_sta_label, m713_sta_severity
from franklinwh_bridge.store.alarms import AlarmTracker, query_alarm_events
from franklinwh_bridge.store.db import init_db


def test_labels_follow_the_sunspec_enum():
    assert m713_sta_label(0) == "OK"
    assert m713_sta_label(1) == "Warning"
    assert m713_sta_label(2) == "Error"          # was "Unknown (2)": poller had ERROR = 3
    assert m713_sta_label(3) == "Unknown (3)"    # was "Error"
    assert m713_sta_label(400) == "Unknown (400)"
    assert m713_sta_label(None) is None


def test_severity():
    assert m713_sta_severity(0) == "info"
    assert m713_sta_severity(1) == "warning"
    assert m713_sta_severity(2) == "fault"
    assert m713_sta_severity(400) == "warning"


async def test_tracker_records_health_changes(tmp_path):
    db = await init_db(tmp_path / "a.db")
    t = AlarmTracker(db)
    for raw in (0, 2, 0):
        await t.process_sample({"battery_sta": raw}, "default")
    ev = await query_alarm_events(db, 0, time.time() + 1)
    sta = [e for e in ev if e["source"] == "M713_Sta"]
    assert [(e["alarms_cleared"], e["alarms_set"], e["severity"]) for e in sta] == [
        ("OK", "Error", "fault"),
        ("Error", "OK", "info"),
    ]
    await db.close()


async def test_rows_written_with_the_old_activity_labels_read_as_health(tmp_path):
    # The two real rows from 2026-07-05: raw 400 ("State400") then raw 0 ("Idle").
    db = await init_db(tmp_path / "a.db")
    rows = [
        (100.0, "default", "M713_Sta", 400, "State400", "Idle", "info"),
        (114.0, "default", "M713_Sta", 0, "Idle", "State400", "info"),
    ]
    await db.executemany(
        "INSERT INTO alarm_events (ts, gateway_id, source, value_raw, alarms_set, "
        "alarms_cleared, severity) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()
    ev = await query_alarm_events(db, 0, 1000)
    assert [(e["alarms_cleared"], e["alarms_set"], e["severity"]) for e in ev] == [
        ("OK", "Unknown (400)", "warning"),
        ("Unknown (400)", "OK", "info"),
    ]
    await db.close()
