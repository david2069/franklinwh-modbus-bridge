"""Tests for persistent operational statistics."""

import pytest

from franklinwh_bridge.store.db import init_db
from franklinwh_bridge.store.stats import OperationalStats, _format_duration


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


async def test_load_creates_fresh_stats(db):
    stats = await OperationalStats.load(db)
    snap = stats.snapshot
    assert snap.polls_total == 0
    assert snap.polls_ok == 0
    assert snap.samples_recorded == 0
    assert snap.to_dict()["data_quality_pct"] == 100.0


async def test_record_poll_ok(db):
    stats = await OperationalStats.load(db)
    stats.record_poll("ok")
    stats.record_poll("ok")
    stats.record_poll("ok")
    snap = stats.snapshot
    assert snap.polls_ok == 3
    assert snap.polls_total == 3
    assert snap.to_dict()["data_quality_pct"] == 100.0


async def test_record_poll_mixed_quality(db):
    stats = await OperationalStats.load(db)
    for _ in range(8):
        stats.record_poll("ok")
    stats.record_poll("stale")
    stats.record_poll("error")
    snap = stats.snapshot
    assert snap.polls_ok == 8
    assert snap.polls_stale == 1
    assert snap.polls_error == 1
    assert snap.polls_total == 10
    assert snap.to_dict()["data_quality_pct"] == 80.0


async def test_record_sanitization(db):
    stats = await OperationalStats.load(db)
    stats.record_sanitization()
    stats.record_sanitization()
    assert stats.snapshot.sanitizations == 2


async def test_record_samples(db):
    stats = await OperationalStats.load(db)
    stats.record_sample_recorded()
    stats.record_sample_recorded()
    stats.record_sample_rejected()
    snap = stats.snapshot
    assert snap.samples_recorded == 2
    assert snap.samples_rejected == 1


async def test_record_conn_events(db):
    stats = await OperationalStats.load(db)
    stats.record_conn_drop()
    stats.record_conn_drop()
    stats.record_conn_recovery()
    snap = stats.snapshot
    assert snap.conn_drops == 2
    assert snap.conn_recoveries == 1


async def test_record_mqtt(db):
    stats = await OperationalStats.load(db)
    stats.record_mqtt_sent(5)
    stats.record_mqtt_sent(3)
    assert stats.snapshot.mqtt_sent == 8


async def test_record_error(db):
    stats = await OperationalStats.load(db)
    stats.record_error("connection timeout")
    snap = stats.snapshot
    assert snap.polls_error == 1
    assert snap.last_error == "connection timeout"


async def test_flush_persists_to_db(db):
    stats = await OperationalStats.load(db)
    stats.record_poll("ok")
    stats.record_poll("ok")
    stats.record_sanitization()
    stats.record_sample_recorded()
    await stats.flush()

    # Verify directly in DB
    async with db.execute(
        "SELECT polls_ok, sanitizations, samples_recorded FROM operational_stats WHERE id = 1"
    ) as cur:
        row = await cur.fetchone()
    assert row[0] == 2
    assert row[1] == 1
    assert row[2] == 1


async def test_stats_survive_reload(db):
    """Stats accumulated in one session are visible after reload."""
    stats1 = await OperationalStats.load(db)
    stats1.record_poll("ok")
    stats1.record_poll("ok")
    stats1.record_poll("stale")
    stats1.record_sanitization()
    stats1.record_sample_recorded()
    stats1.record_conn_drop()
    stats1.record_mqtt_sent(10)
    await stats1.flush()

    # Simulate restart — load fresh
    stats2 = await OperationalStats.load(db)
    snap = stats2.snapshot
    assert snap.polls_ok == 2
    assert snap.polls_stale == 1
    assert snap.sanitizations == 1
    assert snap.samples_recorded == 1
    assert snap.conn_drops == 1
    assert snap.mqtt_sent == 10


async def test_stats_accumulate_across_sessions(db):
    """New session adds to previous session's counters."""
    stats1 = await OperationalStats.load(db)
    stats1.record_poll("ok")
    stats1.record_poll("ok")
    await stats1.flush()

    stats2 = await OperationalStats.load(db)
    stats2.record_poll("ok")
    stats2.record_poll("ok")
    stats2.record_poll("ok")
    snap = stats2.snapshot
    assert snap.polls_ok == 5  # 2 from session 1 + 3 from session 2


async def test_snapshot_to_dict(db):
    stats = await OperationalStats.load(db)
    stats.record_poll("ok")
    d = stats.snapshot.to_dict()
    assert "uptime_s" in d
    assert "uptime_human" in d
    assert "polls_ok" in d
    assert "polls_total" in d
    assert "data_quality_pct" in d
    assert "samples_recorded" in d
    assert "sanitizations" in d
    assert "conn_drops" in d
    assert "mqtt_sent" in d
    assert d["polls_ok"] == 1
    assert d["polls_total"] == 1
    assert d["data_quality_pct"] == 100.0


async def test_maybe_flush_respects_interval(db):
    stats = await OperationalStats.load(db)
    stats.record_poll("ok")
    # Force last flush to appear recent
    stats._last_flush_ts = 99999999999.0
    await stats.maybe_flush()
    # Should NOT have flushed since interval hasn't elapsed
    assert stats._dirty is True


def test_format_duration_minutes():
    assert _format_duration(90) == "1m"


def test_format_duration_hours():
    assert _format_duration(7200) == "2h 0m"


def test_format_duration_days():
    assert _format_duration(90000) == "1d 1h 0m"


def test_format_duration_complex():
    # 3 days, 4 hours, 12 minutes
    s = 3 * 86400 + 4 * 3600 + 12 * 60
    assert _format_duration(s) == "3d 4h 12m"
