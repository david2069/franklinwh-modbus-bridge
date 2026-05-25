"""Metrics sink/reader — SQLite backend for v1."""

from __future__ import annotations

import logging
import time
from typing import Protocol

import aiosqlite

from franklinwh_bridge.modbus.sample import Sample

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 14
DEFAULT_RETENTION_DAYS = 30

# Power metric point names to extract from poller samples
POWER_METRIC_KEYS: dict[str, str] = {
    "battery_power_w": "battery_w",
    "grid_power_w": "grid_w",
    "total_solar": "solar_w",
    "home_load_ext": "home_w",
    "soc": "soc",
}

# Range string -> seconds
RANGE_MAP: dict[str, int] = {
    "30m": 30 * 60,
    "1h": 60 * 60,
    "6h": 6 * 60 * 60,
    "24h": 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}

# Maximum raw points before downsampling kicks in (> 6h ranges)
MAX_POINTS = 360


class MetricsSink(Protocol):
    async def write_sample(self, sample: Sample) -> None: ...
    async def prune(self, ttl_days: int = DEFAULT_TTL_DAYS) -> int: ...


class MetricsReader(Protocol):
    async def read_latest(self, gateway_id: str) -> dict[str, float | int | str | None]: ...

    async def read_history(
        self,
        gateway_id: str,
        point_id: str,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 1000,
    ) -> list[tuple[float, float | None, str]]: ...


class SQLiteMetrics:
    """SQLite-backed metrics sink and reader."""

    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def write_sample(self, sample: Sample) -> None:
        rows = [
            (sample.gateway_id, point_id, sample.ts, value, sample.quality)
            for point_id, value in sample.points.items()
            if isinstance(value, (int, float))
        ]
        if rows:
            await self._db.executemany(
                "INSERT INTO metric_samples (gateway_id, point_id, ts, value, quality) "
                "VALUES (?, ?, ?, ?, ?)",
                rows,
            )
            await self._db.commit()

    async def prune(self, ttl_days: int = DEFAULT_TTL_DAYS) -> int:
        cutoff = time.time() - (ttl_days * 86400)
        cursor = await self._db.execute(
            "DELETE FROM metric_samples WHERE ts < ?", (cutoff,)
        )
        await self._db.commit()
        pruned = cursor.rowcount
        if pruned:
            logger.info("Pruned %d metric rows older than %d days", pruned, ttl_days)
        return pruned

    async def read_latest(self, gateway_id: str) -> dict[str, float | int | str | None]:
        query = """
            SELECT point_id, value
            FROM metric_samples
            WHERE gateway_id = ?
            AND ts = (SELECT MAX(ts) FROM metric_samples WHERE gateway_id = ?)
        """
        result: dict[str, float | int | str | None] = {}
        async with self._db.execute(query, (gateway_id, gateway_id)) as cursor:
            async for row in cursor:
                result[row[0]] = row[1]
        return result

    async def read_history(
        self,
        gateway_id: str,
        point_id: str,
        from_ts: float | None = None,
        to_ts: float | None = None,
        limit: int = 1000,
    ) -> list[tuple[float, float | None, str]]:
        conditions = ["gateway_id = ?", "point_id = ?"]
        params: list = [gateway_id, point_id]
        if from_ts is not None:
            conditions.append("ts >= ?")
            params.append(from_ts)
        if to_ts is not None:
            conditions.append("ts <= ?")
            params.append(to_ts)

        query = (
            f"SELECT ts, value, quality FROM metric_samples "
            f"WHERE {' AND '.join(conditions)} "
            f"ORDER BY ts DESC LIMIT ?"
        )
        params.append(limit)

        rows: list[tuple[float, float | None, str]] = []
        async with self._db.execute(query, params) as cursor:
            async for row in cursor:
                rows.append((row[0], row[1], row[2]))
        return rows


# ---------------------------------------------------------------------------
# Dashboard metrics (power time-series stored in the `metrics` table, v5)
# ---------------------------------------------------------------------------


async def record_sample(db: aiosqlite.Connection, points: dict) -> None:
    """Insert a metrics row from poller sample points."""
    row = {col: points.get(key) for key, col in POWER_METRIC_KEYS.items()}
    # Only write if at least one metric has a value
    if not any(v is not None for v in row.values()):
        return
    await db.execute(
        "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (time.time(), row["battery_w"], row["grid_w"], row["solar_w"], row["home_w"], row["soc"]),
    )
    await db.commit()


async def query_metrics(db: aiosqlite.Connection, range_seconds: int = 1800) -> list[dict]:
    """Query metrics for given time range, downsampling if needed.

    For ranges <= 6h return raw rows.  For longer ranges, bucket-average
    down to ~MAX_POINTS entries so the chart payload stays small.
    """
    cutoff = time.time() - range_seconds
    six_hours = 6 * 60 * 60

    if range_seconds <= six_hours:
        # Raw data
        rows: list[dict] = []
        async with db.execute(
            "SELECT ts, battery_w, grid_w, solar_w, home_w, soc "
            "FROM metrics WHERE ts >= ? ORDER BY ts",
            (cutoff,),
        ) as cursor:
            async for row in cursor:
                rows.append(
                    {
                        "ts": row[0],
                        "battery_w": row[1],
                        "grid_w": row[2],
                        "solar_w": row[3],
                        "home_w": row[4],
                        "soc": row[5],
                    }
                )
        return rows

    # Downsample: divide the range into MAX_POINTS buckets
    bucket_size = range_seconds / MAX_POINTS
    query = """
        SELECT
            CAST((ts - ?) / ? AS INTEGER) AS bucket,
            AVG(battery_w),
            AVG(grid_w),
            AVG(solar_w),
            AVG(home_w),
            AVG(soc)
        FROM metrics
        WHERE ts >= ?
        GROUP BY bucket
        ORDER BY bucket
    """
    rows = []
    async with db.execute(query, (cutoff, bucket_size, cutoff)) as cursor:
        async for row in cursor:
            # Reconstruct approximate timestamp from bucket midpoint
            bucket_ts = cutoff + (row[0] + 0.5) * bucket_size
            rows.append(
                {
                    "ts": round(bucket_ts, 1),
                    "battery_w": round(row[1], 1) if row[1] is not None else None,
                    "grid_w": round(row[2], 1) if row[2] is not None else None,
                    "solar_w": round(row[3], 1) if row[3] is not None else None,
                    "home_w": round(row[4], 1) if row[4] is not None else None,
                    "soc": round(row[5], 1) if row[5] is not None else None,
                }
            )
    return rows


async def purge_old(db: aiosqlite.Connection, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete metrics older than retention. Return count deleted."""
    cutoff = time.time() - (retention_days * 86400)
    cursor = await db.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
    await db.commit()
    deleted = cursor.rowcount
    if deleted:
        logger.info("Purged %d metrics rows older than %d days", deleted, retention_days)
    return deleted


async def get_retention_days(db: aiosqlite.Connection) -> int:
    """Read metrics retention from app_config, defaulting to DEFAULT_RETENTION_DAYS."""
    async with db.execute(
        "SELECT value FROM app_config WHERE key = ?", ("metrics_retention_days",)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return DEFAULT_RETENTION_DAYS
    try:
        return int(row[0])
    except (ValueError, TypeError):
        return DEFAULT_RETENTION_DAYS


async def set_retention_days(db: aiosqlite.Connection, days: int) -> None:
    """Persist metrics retention setting."""
    await db.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("metrics_retention_days", str(days)),
    )
    await db.commit()
