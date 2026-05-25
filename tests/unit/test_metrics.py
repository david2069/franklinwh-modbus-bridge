"""Tests for SQLite metrics sink and reader."""

import time

import pytest

from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.store.db import init_db
from franklinwh_bridge.store.metrics import (
    DEFAULT_RETENTION_DAYS,
    SQLiteMetrics,
    get_retention_days,
    purge_old,
    query_metrics,
    record_sample,
    set_retention_days,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


@pytest.fixture
def metrics(db):
    return SQLiteMetrics(db)


async def test_write_and_read_latest(metrics):
    sample = Sample(
        gateway_id="gw1", ts=1000.0, points={"soc": 85, "power": -1200}, quality="ok"
    )
    await metrics.write_sample(sample)
    latest = await metrics.read_latest("gw1")
    assert latest["soc"] == 85
    assert latest["power"] == -1200


async def test_write_skips_non_numeric(metrics):
    sample = Sample(
        gateway_id="gw1",
        ts=1000.0,
        points={"soc": 85, "mode": "self_consumption"},
        quality="ok",
    )
    await metrics.write_sample(sample)
    latest = await metrics.read_latest("gw1")
    assert "soc" in latest
    assert "mode" not in latest


async def test_read_history(metrics):
    for i in range(5):
        sample = Sample(
            gateway_id="gw1",
            ts=1000.0 + i,
            points={"soc": 80 + i},
            quality="ok",
        )
        await metrics.write_sample(sample)

    history = await metrics.read_history("gw1", "soc")
    assert len(history) == 5
    assert history[0][0] == 1004.0  # most recent first
    assert history[0][1] == 84


async def test_read_history_with_time_range(metrics):
    for i in range(5):
        sample = Sample(
            gateway_id="gw1", ts=1000.0 + i, points={"soc": 80 + i}, quality="ok"
        )
        await metrics.write_sample(sample)

    history = await metrics.read_history("gw1", "soc", from_ts=1002.0, to_ts=1003.0)
    assert len(history) == 2


async def test_prune_removes_old_data(metrics):
    old_ts = time.time() - (15 * 86400)  # 15 days ago
    recent_ts = time.time() - (1 * 86400)  # 1 day ago

    await metrics.write_sample(
        Sample(gateway_id="gw1", ts=old_ts, points={"soc": 50}, quality="ok")
    )
    await metrics.write_sample(
        Sample(gateway_id="gw1", ts=recent_ts, points={"soc": 90}, quality="ok")
    )

    pruned = await metrics.prune(ttl_days=14)
    assert pruned == 1

    latest = await metrics.read_latest("gw1")
    assert latest["soc"] == 90


async def test_prune_respects_ttl(metrics):
    recent_ts = time.time() - (1 * 86400)
    await metrics.write_sample(
        Sample(gateway_id="gw1", ts=recent_ts, points={"soc": 90}, quality="ok")
    )
    pruned = await metrics.prune(ttl_days=14)
    assert pruned == 0


# ---------------------------------------------------------------------------
# Dashboard metrics (v5 table) tests
# ---------------------------------------------------------------------------


async def test_record_sample_writes_row(db):
    points = {
        "battery_power_w": 700,
        "grid_power_w": 1,
        "total_solar": 0,
        "home_load_ext": 678,
        "soc": 30,
    }
    await record_sample(db, points)
    async with db.execute("SELECT COUNT(*) FROM metrics") as cur:
        count = (await cur.fetchone())[0]
    assert count == 1


async def test_record_sample_skips_empty(db):
    await record_sample(db, {"some_other_point": 42})
    async with db.execute("SELECT COUNT(*) FROM metrics") as cur:
        count = (await cur.fetchone())[0]
    assert count == 0


async def test_query_metrics_raw(db):
    now = time.time()
    for i in range(5):
        await db.execute(
            "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now - 60 + i * 10, 100 + i, 200 + i, 300 + i, 400 + i, 50 + i),
        )
    await db.commit()

    rows = await query_metrics(db, range_seconds=1800)
    assert len(rows) == 5
    assert rows[0]["battery_w"] == 100
    assert rows[-1]["soc"] == 54


async def test_query_metrics_downsampled(db):
    now = time.time()
    # Insert 1000 points spanning 8 hours
    for i in range(1000):
        ts = now - 8 * 3600 + i * 28.8  # ~28.8s apart
        await db.execute(
            "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, float(i), 0, 0, 0, 50),
        )
    await db.commit()

    rows = await query_metrics(db, range_seconds=8 * 3600)
    # Should be downsampled to <= 360 buckets
    assert len(rows) <= 360
    assert len(rows) > 0
    # Each row should have the expected keys
    assert "ts" in rows[0]
    assert "battery_w" in rows[0]


async def test_purge_old_removes_expired(db):
    now = time.time()
    old_ts = now - 31 * 86400  # 31 days ago
    recent_ts = now - 1 * 86400  # 1 day ago
    await db.execute(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (old_ts, 100, 200, 300, 400, 50),
    )
    await db.execute(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (recent_ts, 100, 200, 300, 400, 50),
    )
    await db.commit()

    deleted = await purge_old(db, retention_days=30)
    assert deleted == 1

    async with db.execute("SELECT COUNT(*) FROM metrics") as cur:
        count = (await cur.fetchone())[0]
    assert count == 1


async def test_retention_days_default(db):
    days = await get_retention_days(db)
    assert days == DEFAULT_RETENTION_DAYS


async def test_retention_days_set_and_get(db):
    await set_retention_days(db, 7)
    days = await get_retention_days(db)
    assert days == 7

    await set_retention_days(db, 90)
    days = await get_retention_days(db)
    assert days == 90
