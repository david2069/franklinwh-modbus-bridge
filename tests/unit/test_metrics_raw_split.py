"""Charts, export and Archive Now follow the 'keep raw' setting (#480).

The archive job moves raw rows at ``metrics_raw_age_days``; charts and export used a fixed
7 days, so a setting of 3 or 14 days left a hole in the history. The Storage card's size
now counts the -wal file, as the health report does.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from franklinwh_bridge.api import admin
from franklinwh_bridge.store.db import init_db
from franklinwh_bridge.store.metrics import (
    archive_old_metrics,
    export_metrics,
    get_storage_stats,
    query_metrics_with_archive,
    set_raw_age_days,
)

DAY = 86400


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


async def _hourly(db, start_days_ago: float, end_days_ago: float) -> None:
    """One raw row an hour from ``start_days_ago`` to ``end_days_ago``."""
    now = time.time()
    ts = now - start_days_ago * DAY
    while ts < now - end_days_ago * DAY:
        await db.execute(
            "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
            "VALUES (?, 100, 200, 300, 400, 50)", (ts,))
        ts += 3600
    await db.commit()


def _largest_gap_h(points: list[dict]) -> float:
    """The longest stretch with no chart point, in hours (a 20-day chart's buckets are 80 min;
    the rows are hourly, so every bucket has one)."""
    ts = sorted(p["ts"] for p in points if p["soc"] is not None)
    return max(b - a for a, b in zip(ts, ts[1:], strict=False)) / 3600


@pytest.mark.parametrize("raw_days", [3, 14])
async def test_chart_has_no_gap_whatever_the_raw_age(db, raw_days):
    await _hourly(db, 20, 0)
    await archive_old_metrics(db, raw_age_s=raw_days * DAY)  # as the daily job does
    points = await query_metrics_with_archive(db, 20 * DAY)
    assert len(points) > 300 and _largest_gap_h(points) < 3


async def test_chart_has_no_gap_when_the_daily_job_last_ran_hours_ago(db):
    await _hourly(db, 20, 0)
    await archive_old_metrics(db, raw_age_s=7 * DAY + 20 * 3600)  # 20 h ago, at 7 days
    points = await query_metrics_with_archive(db, 20 * DAY)
    assert len(points) > 300 and _largest_gap_h(points) < 3


async def test_export_has_no_gap_with_a_short_raw_age(db):
    await _hourly(db, 10, 0)
    await archive_old_metrics(db, raw_age_s=3 * DAY)
    rows = await export_metrics(db, 10 * DAY)
    now = time.time()
    days = {int((now - datetime.fromisoformat(r["timestamp"]).timestamp()) // DAY) for r in rows}
    assert days == set(range(10))


async def test_archive_now_keeps_the_configured_raw_age(db):
    await set_raw_age_days(db, 14)
    await _hourly(db, 10, 0)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))
    assert (await admin.run_archive(request)) == {"archived_rows": 0}
    await set_raw_age_days(db, 3)
    assert (await admin.run_archive(request))["archived_rows"] > 0
    async with db.execute("SELECT MIN(ts) FROM metrics") as cur:
        assert (await cur.fetchone())[0] >= time.time() - 3 * DAY - 300


async def test_storage_size_counts_the_wal(db, tmp_path):
    await _hourly(db, 2, 0)  # writes land in the -wal until a checkpoint
    path = tmp_path / "test.db"
    wal = tmp_path / "test.db-wal"
    assert wal.exists() and wal.stat().st_size > 0
    stats = await get_storage_stats(db)
    assert stats["db_size_bytes"] == os.path.getsize(path) + os.path.getsize(wal)
