"""Unit tests for the scheduler v2 ConnectivityMonitor + outage persistence."""

import pytest

from franklinwh_bridge.gateway.connectivity import ConnectivityMonitor
from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.store.db import (
    create_outage,
    get_open_outage,
    get_recent_outages,
    init_db,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "conn.db")
    yield conn
    await conn.close()


def sample(gw_id, ts, quality="ok"):
    return Sample(gateway_id=gw_id, ts=ts, points={"soc": 50}, quality=quality)


# ── outage store round-trip ───────────────────────────────────


async def test_create_and_close_outage(db):
    from franklinwh_bridge.store.db import close_outage

    oid = await create_outage(db, "default", start_ts=1000.0)
    assert oid.startswith("out_")
    assert (await get_open_outage(db, "default"))["id"] == oid

    closed = await close_outage(db, oid, end_ts=1090.0)
    assert closed["end_ts"] == 1090.0
    assert closed["duration_s"] == 90.0
    assert closed["missed_job_ids"] == []
    assert await get_open_outage(db, "default") is None  # no longer open


# ── monitor: open on staleness, close on recovery ─────────────


async def test_no_outage_while_polls_are_fresh(db):
    mon = ConnectivityMonitor(db, outage_threshold_s=60)
    await mon.on_sample(sample("default", 1000.0))
    await mon.tick(now=1030.0)  # 30s < 60s threshold
    assert mon.is_connected("default")
    assert await get_open_outage(db, "default") is None


async def test_outage_opens_after_threshold(db):
    mon = ConnectivityMonitor(db, outage_threshold_s=60)
    await mon.on_sample(sample("default", 1000.0))
    await mon.tick(now=1075.0)  # 75s > 60s → outage
    assert not mon.is_connected("default")
    open_row = await get_open_outage(db, "default")
    assert open_row is not None
    assert open_row["start_ts"] == 1000.0  # started at the last good poll


async def test_outage_opens_only_once(db):
    mon = ConnectivityMonitor(db, outage_threshold_s=60)
    await mon.on_sample(sample("default", 1000.0))
    await mon.tick(now=1075.0)
    await mon.tick(now=1200.0)  # still stale — must not open a second outage
    assert len(await get_recent_outages(db, gateway_id="default")) == 1


async def test_recovery_closes_outage_and_fires_callback(db):
    recovered: list = []

    async def on_recover(gw_id, outage_id, start_ts, end_ts):
        recovered.append((gw_id, outage_id, start_ts, end_ts))

    mon = ConnectivityMonitor(db, outage_threshold_s=60, on_recover=on_recover)
    await mon.on_sample(sample("default", 1000.0))
    await mon.tick(now=1075.0)  # open outage
    assert not mon.is_connected("default")

    await mon.on_sample(sample("default", 1120.0))  # good poll → recover
    assert mon.is_connected("default")
    assert await get_open_outage(db, "default") is None
    assert len(recovered) == 1
    gw_id, _oid, start_ts, end_ts = recovered[0]
    assert gw_id == "default" and start_ts == 1000.0 and end_ts == 1120.0


async def test_non_ok_sample_does_not_refresh_liveness(db):
    mon = ConnectivityMonitor(db, outage_threshold_s=60)
    await mon.on_sample(sample("default", 1000.0, quality="ok"))
    await mon.on_sample(sample("default", 1050.0, quality="error"))  # ignored
    await mon.tick(now=1075.0)  # 75s since last OK → outage
    assert not mon.is_connected("default")


async def test_error_sample_before_any_ok_is_ignored(db):
    mon = ConnectivityMonitor(db, outage_threshold_s=60)
    await mon.on_sample(sample("gw", 1000.0, quality="error"))
    await mon.tick(now=2000.0)  # gateway never seen ok → not monitored yet
    assert await get_open_outage(db, "gw") is None


# ── multi-gateway independence ────────────────────────────────


async def test_gateways_tracked_independently(db):
    mon = ConnectivityMonitor(db, outage_threshold_s=60)
    await mon.on_sample(sample("a", 1000.0))
    await mon.on_sample(sample("b", 1000.0))
    await mon.on_sample(sample("b", 1070.0))  # b keeps polling
    await mon.tick(now=1075.0)  # a is stale (75s), b is fresh (5s)
    assert not mon.is_connected("a")
    assert mon.is_connected("b")


# ── snapshot read model ───────────────────────────────────────


async def test_snapshot_shape(db):
    mon = ConnectivityMonitor(db, outage_threshold_s=60)
    await mon.on_sample(sample("default", 1000.0))
    await mon.tick(now=1075.0)  # open outage
    snap = await mon.snapshot()
    assert snap["connected"] is False
    assert snap["gateways"]["default"]["connected"] is False
    assert snap["gateways"]["default"]["last_ok_ts"] == 1000.0
    assert snap["gateways"]["default"]["current_outage_id"] is not None
    assert len(snap["recent_outages"]) == 1


# ── recovery → catch-up → outage linkage (A1 + B end-to-end) ──


async def test_recovery_runs_catchup_and_links_outage(db):
    import time as _time
    from datetime import datetime

    from franklinwh_bridge.gateway.scheduler import ScheduleEngine
    from franklinwh_bridge.store.db import create_schedule, get_outage, set_outage_catchup

    now_dt = datetime(2026, 6, 15, 10, 30)
    since_ts = _time.mktime(datetime(2026, 6, 15, 9, 0).timetuple())
    recover_ts = _time.mktime(now_dt.timetuple())

    # A oneoff that fired at 09:30 for 30 min → fully passed (missed) by 10:30.
    row = await create_schedule(
        db, name="peak", when_spec={}, action="force_discharge",
        target_type="gateway", target_id="default",
    )
    # Trigger columns aren't in the CRUD helper's field set — set them directly.
    await db.execute(
        "UPDATE schedules SET trigger_kind='oneoff', "
        "trigger_spec='{\"fire_at\": \"2026-06-15T09:30:00\"}', duration_s=1800 "
        "WHERE id=?",
        (row["id"],),
    )
    await db.commit()

    engine = ScheduleEngine(
        db, resolver=lambda tt, tid: [("default", object())], now_fn=lambda: now_dt
    )
    await engine.load()

    async def on_recover(gw, oid, start, end):
        missed = await engine.catchup(gw, start)
        if missed:
            await set_outage_catchup(db, oid, missed, [])

    mon = ConnectivityMonitor(db, outage_threshold_s=60, on_recover=on_recover)
    await mon.on_sample(sample("default", since_ts))       # last good poll 09:00
    await mon.tick(now=since_ts + 120)                      # outage opens
    open_row = await get_open_outage(db, "default")
    assert open_row is not None
    await mon.on_sample(sample("default", recover_ts))      # recovery → catch-up

    outage = await get_outage(db, open_row["id"])
    assert outage["end_ts"] == recover_ts
    assert outage["missed_job_ids"] == [row["id"]]
