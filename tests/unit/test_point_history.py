"""Per-point history — the writer, reader and its cost controls.

`metrics` keeps thirteen fixed columns, so voltage, current, frequency, power
factor and the DC-side readings had no history at all and the charts could only
show the present. This is the table that fixes that — and because it is far
larger per day than `metrics` (measured 97 bytes/row, ~270MB for every point at
10s over 21 days against a ~23MB database), most of these tests are about it
staying off and staying bounded unless asked otherwise.
"""

from __future__ import annotations

import pytest

from franklinwh_bridge.store.db import init_db
from franklinwh_bridge.store.point_history import (
    DEFAULTS,
    HISTORISED_POINTS,
    get_config,
    project_bytes,
    purge_points,
    query_points,
    record_points,
    set_config,
    storage_stats,
)

SAMPLE = {
    "voltage_v": 246.2, "current_a": 2.3, "frequency_hz": 49.97,
    "power_factor": 0.001, "grid_va": 573.0, "grid_var": -517.0,
    "soc": 31.0, "dc_power_w": 200.0,
}


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "ph.db")
    yield conn
    await conn.close()


# ── Cost control ──────────────────────────────────────────────


def test_it_is_off_by_default():
    """This can add hundreds of MB to a ~20MB database. Upgrading the bridge
    must not start consuming that on someone's SD card."""
    assert DEFAULTS["enabled"] is False


@pytest.mark.asyncio
async def test_defaults_are_returned_before_anything_is_configured(db):
    cfg = await get_config(db)

    assert cfg["enabled"] is False
    assert cfg["interval_s"] == 10
    assert cfg["retention_days"] == 21
    assert set(cfg["points"]) == set(HISTORISED_POINTS)


@pytest.mark.asyncio
async def test_settings_round_trip(db):
    await set_config(db, {"enabled": True, "interval_s": 60, "retention_days": 7})
    cfg = await get_config(db)

    assert (cfg["enabled"], cfg["interval_s"], cfg["retention_days"]) == (True, 60, 7)


@pytest.mark.asyncio
async def test_an_absurd_interval_is_clamped_not_honoured(db):
    """A 1s interval across every point is ~1.4M rows/day, and 0 would spin."""
    assert (await set_config(db, {"interval_s": 1}))["interval_s"] == 5
    assert (await set_config(db, {"interval_s": 0}))["interval_s"] == 5
    assert (await set_config(db, {"interval_s": 999999}))["interval_s"] == 3600


@pytest.mark.asyncio
async def test_retention_is_clamped(db):
    assert (await set_config(db, {"retention_days": 0}))["retention_days"] == 1
    assert (await set_config(db, {"retention_days": 10_000}))["retention_days"] == 365


@pytest.mark.asyncio
async def test_unknown_points_are_dropped_from_config(db):
    """A stale or hand-edited config must not make the writer emit rows for a
    point that doesn't exist."""
    cfg = await set_config(db, {"points": ["voltage_v", "not_a_point"]})

    assert cfg["points"] == ["voltage_v"]


def test_the_projection_matches_the_measured_row_cost():
    """Settings shows this before the user commits to a configuration."""
    mb = project_bytes(15, 10, 21) / 1048576

    assert 230 < mb < 290, mb
    # Halving the rate halves the storage — the relationship users reason with.
    assert project_bytes(15, 20, 21) == pytest.approx(project_bytes(15, 10, 21) / 2, rel=0.01)


# ── Writing ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_sample_writes_one_row_per_point(db):
    written = await record_points(db, SAMPLE, ts=1000.0)

    assert written == len(SAMPLE)


@pytest.mark.asyncio
async def test_only_the_requested_points_are_written(db):
    written = await record_points(db, SAMPLE, ts=1000.0,
                                  point_ids=["voltage_v", "soc"])

    assert written == 2


@pytest.mark.asyncio
async def test_absent_and_non_numeric_points_are_skipped_not_nulled(db):
    """A gap in a series is honest; a null row is something every reader has
    to filter forever."""
    written = await record_points(
        db, {"voltage_v": 246.2, "current_a": None, "frequency_hz": "n/a"},
        ts=1000.0,
    )

    assert written == 1


@pytest.mark.asyncio
async def test_out_of_range_readings_are_rejected(db):
    """Modbus sentinels (0xFFFF → 65535) would otherwise stretch the axis of
    every future chart that includes them."""
    written = await record_points(
        db, {"voltage_v": 65535.0, "soc": 31.0, "power_factor": 12.0}, ts=1000.0,
    )

    assert written == 1  # only soc survives


@pytest.mark.asyncio
async def test_a_sample_with_nothing_useful_writes_no_rows(db):
    assert await record_points(db, {"unrelated": 5}, ts=1000.0) == 0


# ── Reading ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_series_reads_back_in_time_order(db):
    for i in range(5):
        await record_points(db, {"voltage_v": 240 + i}, ts=1000.0 + i * 10)

    series = await query_points(db, ["voltage_v"], 990.0, 1100.0)

    assert [p["value"] for p in series["voltage_v"]] == [240, 241, 242, 243, 244]


@pytest.mark.asyncio
async def test_several_points_come_back_keyed_separately(db):
    await record_points(db, {"voltage_v": 246.0, "soc": 31.0}, ts=1000.0)

    series = await query_points(db, ["voltage_v", "soc"], 990.0, 1010.0)

    assert series["voltage_v"][0]["value"] == 246.0
    assert series["soc"][0]["value"] == 31.0


@pytest.mark.asyncio
async def test_a_point_with_no_data_returns_an_empty_series_not_a_missing_key(db):
    """The chart iterates the keys it asked for; a missing one would throw."""
    series = await query_points(db, ["voltage_v", "soh"], 0.0, 2000.0)

    assert series["soh"] == []


@pytest.mark.asyncio
async def test_bucketing_averages_and_reduces_the_point_count(db):
    """Six hours at 10s is 2,160 points per series; no screen has that many
    pixels, so a long range has to downsample."""
    for i in range(60):
        await record_points(db, {"voltage_v": 240.0 + (i % 2)}, ts=1000.0 + i)

    raw = await query_points(db, ["voltage_v"], 999.0, 1060.0)
    bucketed = await query_points(db, ["voltage_v"], 999.0, 1060.0, bucket_seconds=10)

    assert len(raw["voltage_v"]) == 60
    assert len(bucketed["voltage_v"]) < 10
    assert all(240.0 <= p["value"] <= 241.0 for p in bucketed["voltage_v"])


@pytest.mark.asyncio
async def test_another_gateways_data_is_not_returned(db):
    await record_points(db, {"voltage_v": 100.0}, gateway_id="other", ts=1000.0)

    series = await query_points(db, ["voltage_v"], 0.0, 2000.0, gateway_id="default")

    assert series["voltage_v"] == []


@pytest.mark.asyncio
async def test_an_inverted_range_returns_nothing_rather_than_scanning(db):
    assert await query_points(db, ["voltage_v"], 2000.0, 1000.0) == {}


# ── Retention ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_purge_drops_only_what_is_past_retention(db):
    import time

    now = time.time()
    await record_points(db, {"voltage_v": 1.0}, ts=now - 30 * 86400)   # old
    await record_points(db, {"voltage_v": 2.0}, ts=now - 60)           # recent

    deleted = await purge_points(db, retention_days=21)

    assert deleted == 1
    remaining = await query_points(db, ["voltage_v"], 0.0, now + 10)
    assert [p["value"] for p in remaining["voltage_v"]] == [2.0]


@pytest.mark.asyncio
async def test_storage_stats_report_what_is_held(db):
    await record_points(db, {"voltage_v": 1.0, "soc": 50.0}, ts=1000.0)
    await record_points(db, {"voltage_v": 2.0}, ts=1000.0 + 86400)

    stats = await storage_stats(db)

    assert stats["rows"] == 3
    assert stats["points"] == 2
    assert stats["span_days"] == pytest.approx(1.0, abs=0.01)
