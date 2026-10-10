"""Reserve points under the documented 15508/15509 quirk (#34).

Both registers carry the ACTIVE mode's reserve, so the bridge publishes that as
the active reserve and a per-mode reserve only while its mode is active.
"""

from __future__ import annotations

import logging
from datetime import datetime

from franklinwh_bridge.gateway.mock_gateway import synthetic_points
from franklinwh_bridge.gateway.scheduler_sensors import _reserve_current
from franklinwh_bridge.modbus.reserves import ActiveReserveReader, apply_active_reserve
from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES


def _poll(mode, r15508, r15509=None, r16001=None):
    pts = {"mode_name": mode, "self_reserve_pct": r15508,
           "tou_reserve_pct": r15508 if r15509 is None else r15509}
    if r16001 is not None:
        pts["vreg_16001"] = r16001
    ActiveReserveReader("default").apply(pts)
    return pts


def test_self_consumption_does_not_claim_a_tou_reserve():
    """The reference-site case: in Self-Consumption, 15509 read 5 while the
    app's TOU reserve was 15. TOU must be unknown, not 5."""
    pts = _poll("Self-Consumption", 5, r16001=5)
    assert pts["active_reserve_pct"] == 5
    assert pts["self_reserve_pct"] == 5
    assert pts["tou_reserve_pct"] is None


def test_tou_mode_reports_tou_reserve_only():
    pts = _poll("TOU", 20)
    assert pts["active_reserve_pct"] == 20
    assert pts["tou_reserve_pct"] == 20
    assert pts["self_reserve_pct"] is None


def test_backup_mode_leaves_both_per_mode_reserves_unknown():
    pts = _poll("Emergency Backup", 100)
    assert pts["active_reserve_pct"] == 100
    assert pts["self_reserve_pct"] is None and pts["tou_reserve_pct"] is None


def test_16001_disagreement_warns_once(caplog):
    r = ActiveReserveReader("gw")
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            r.apply({"mode_name": "TOU", "self_reserve_pct": 20,
                     "tou_reserve_pct": 20, "vreg_16001": 25})
    warnings = [m for m in caplog.messages if "disagrees with 16001" in m]
    assert len(warnings) == 1


def test_unread_reserve_adds_nothing():
    """No reserve read this poll: add no keys, so an empty poll stays empty and
    MQTT keeps the last value rather than resetting it."""
    pts = {"mode_name": "TOU"}
    ActiveReserveReader("gw").apply(pts)
    assert pts == {"mode_name": "TOU"}


def test_time_of_use_spelling_counts_as_tou():
    pts = _poll("Time of Use", 20)
    assert pts["tou_reserve_pct"] == 20 and pts["self_reserve_pct"] is None


def test_mock_gateway_reports_reserves_like_real_hardware():
    pts = synthetic_points("m", 0, controls={"mode_name": "TOU", "self_reserve_pct": 20,
                                             "tou_reserve_pct": 30})
    assert pts["active_reserve_pct"] == 30
    assert pts["tou_reserve_pct"] == 30 and pts["self_reserve_pct"] is None


def test_scheduler_current_reserve_uses_active_reserve():
    pts = {}
    apply_active_reserve(pts, 15)
    pts["mode_name"] = "Self-Consumption"
    assert _reserve_current(pts, datetime(2026, 10, 9)) == 15


def test_per_mode_entities_reset_to_unknown_and_active_entity_exists():
    by_slug = {e.slug: e for e in BRIDGE_ENTITIES}
    assert by_slug["self_reserve_pct"].reset_when_unknown
    assert by_slug["tou_reserve_pct"].reset_when_unknown
    active = by_slug["active_reserve_pct"]
    assert active.stat_key == "active_reserve_pct" and not active.is_control


async def _queued(points):
    from franklinwh_bridge.modbus.sample import Sample
    from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher

    pub = MqttPublisher(gateway_id="gw1")
    pub.set_device_info(DeviceInfo(serial="10060006A02F00000001"))
    await pub.queue_sample(Sample.now("gw1", points))
    out = {}
    while not pub._queue.empty():
        m = pub._queue.get_nowait()
        out[m.topic.rsplit("/", 1)[-1]] = m.payload
    return out


async def test_mqtt_resets_an_unknown_reserve_instead_of_keeping_a_stale_one():
    pts = {"mode_name": "Self-Consumption", "self_reserve_pct": 5, "tou_reserve_pct": 5}
    ActiveReserveReader("gw1").apply(pts)
    out = await _queued(pts)
    assert out["tou_reserve_pct"] == "None"  # HA: unknown, not a retained 5
    assert out["self_reserve_pct"] == "5"
    assert out["active_reserve_pct"] == "5"


async def test_mqtt_keeps_last_value_when_the_reserve_was_not_read():
    out = await _queued({"soc": 80})
    assert "tou_reserve_pct" not in out and "active_reserve_pct" not in out


# ── History keeps the active reserve (own metrics column, migration 54) ──


async def test_history_keeps_the_active_reserve_in_backup_mode(tmp_path):
    """Backup mode has no per-mode reserve, but history must still carry the
    active one — through the raw read, the 5-min archive and the export."""
    import time

    from franklinwh_bridge.store.db import init_db
    from franklinwh_bridge.store.metrics import (
        archive_old_metrics,
        export_metrics,
        query_metrics,
        query_metrics_daterange,
        record_sample,
    )

    db = await init_db(tmp_path / "t.db")
    try:
        points = {"battery_power_w": 100, "soc": 60, "mode_name": "Emergency Backup"}
        apply_active_reserve(points, 20)
        assert points["self_reserve_pct"] is None and points["tou_reserve_pct"] is None
        await record_sample(db, points)

        rows = await query_metrics(db, range_seconds=600)
        assert rows[-1]["active_reserve_pct"] == 20

        # Age the row out so it is archived, then read it back from the archive.
        await db.execute("UPDATE metrics SET ts = ts - 9 * 86400")
        await db.commit()
        assert await archive_old_metrics(db) == 1
        now = time.time()
        archived = await query_metrics_daterange(db, now - 10 * 86400, now)
        assert [r["active_reserve_pct"] for r in archived if r["soc"]] == [20]
        assert [r["active_reserve_pct"] for r in await export_metrics(db, 10 * 86400)] == [20]
    finally:
        await db.close()


async def test_migration_54_backfills_active_reserve_from_per_mode_columns():
    import aiosqlite

    from franklinwh_bridge.store import db as dbmod

    db = await aiosqlite.connect(":memory:")
    try:
        await db.executescript("CREATE TABLE schema_version (version INTEGER, applied_at REAL);")
        all_migrations = dbmod.MIGRATIONS
        try:
            dbmod.MIGRATIONS = {k: v for k, v in all_migrations.items() if k <= 53}
            await dbmod.run_migrations(db)
            await db.executemany(
                "INSERT INTO metrics (ts, soc, self_reserve_pct, tou_reserve_pct) "
                "VALUES (?, 50, ?, ?)",
                [(1.0, 15, 15), (2.0, None, 30), (3.0, 10, None), (4.0, None, None)],
            )
            await db.commit()
        finally:
            dbmod.MIGRATIONS = all_migrations
        await dbmod.run_migrations(db)
        async with db.execute("SELECT active_reserve_pct FROM metrics ORDER BY ts") as cur:
            assert [r[0] for r in await cur.fetchall()] == [15, 30, 10, None]
    finally:
        await db.close()
