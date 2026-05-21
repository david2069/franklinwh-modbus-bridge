"""Tests for SQLite metrics sink and reader."""

import time

import pytest

from franklinwh_bridge.modbus.sample import Sample
from franklinwh_bridge.store.db import init_db
from franklinwh_bridge.store.metrics import SQLiteMetrics


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
