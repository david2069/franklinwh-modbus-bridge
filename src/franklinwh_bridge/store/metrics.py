"""Metrics sink/reader — SQLite backend for v1."""

from __future__ import annotations

import csv
import io
import logging
import time
from datetime import UTC, datetime
from typing import Protocol

import aiosqlite

from franklinwh_bridge.modbus.sample import Sample

logger = logging.getLogger(__name__)

DEFAULT_TTL_DAYS = 14
DEFAULT_RETENTION_DAYS = 30
# How long raw, full-resolution samples are kept before being downsampled into
# 5-min buckets. Configurable; must be <= retention. Older raw → archive table.
DEFAULT_RAW_AGE_DAYS = 7

# Power metric point names to extract from poller samples
POWER_METRIC_KEYS: dict[str, str] = {
    "battery_power_w": "battery_w",
    "grid_power_w": "grid_w",
    "total_solar": "solar_w",
    "home_load_ext": "home_w",
    "soc": "soc",
    "ambient_temp_c": "ambient_temp_c",
    "cabinet_temp_c": "cabinet_temp_c",
    "mode_name": "mode_name",
    "self_reserve_pct": "self_reserve_pct",
    "tou_reserve_pct": "tou_reserve_pct",
    "grid_mode": "grid_mode",
}

# Range string -> seconds
RANGE_MAP: dict[str, int] = {
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "8h": 8 * 60 * 60,
    "12h": 12 * 60 * 60,
    "18h": 18 * 60 * 60,
    "24h": 24 * 60 * 60,
    "3d": 3 * 24 * 60 * 60,
    "5d": 5 * 24 * 60 * 60,
    "7d": 7 * 24 * 60 * 60,
    "30d": 30 * 24 * 60 * 60,
}

# Bucket string -> seconds (for user-selectable downsampling)
BUCKET_MAP: dict[str, int] = {
    "1m": 60,
    "5m": 5 * 60,
    "10m": 10 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
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
        cursor = await self._db.execute("DELETE FROM metric_samples WHERE ts < ?", (cutoff,))
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


# Absolute power ceiling for metrics DB writes.  Any value above this is
# physically impossible for residential FranklinWH systems and indicates
# Modbus register corruption (e.g. 0xFFFF sentinel = 65535 or summed
# sentinels = 196605).  This is a last-resort guard — the poller's
# _sanitize_extension_values() should catch these first.
_METRICS_MAX_POWER_W = 15_000
_METRICS_MAX_HOME_W = 50_000  # home can spike during mode transitions

# Modbus uint16 "not available" sentinel
_METRICS_SENTINEL = 65535


async def record_sample(
    db: aiosqlite.Connection,
    points: dict,
    gateway_id: str = "default",
) -> bool:
    """Insert a metrics row from poller sample points.

    Applies a final sanity check: rejects samples where any power metric
    exceeds physical limits or contains Modbus 0xFFFF sentinel values.

    Returns True if a row was written, False if skipped/rejected.
    """
    row = {col: points.get(key) for key, col in POWER_METRIC_KEYS.items()}
    # Only write if at least one metric has a value
    if not any(v is not None for v in row.values()):
        return False

    # Guard: reject rows with physically impossible or sentinel values
    for col, limit in [
        ("solar_w", _METRICS_MAX_POWER_W),
        ("battery_w", _METRICS_MAX_POWER_W),
        ("grid_w", _METRICS_MAX_POWER_W),
        ("home_w", _METRICS_MAX_HOME_W),
    ]:
        val = row.get(col)
        if val is not None and (
            abs(val) > limit or val == _METRICS_SENTINEL or val == -_METRICS_SENTINEL
        ):
            logger.warning(
                "Rejected metrics sample: %s=%s exceeds sanity limit %d "
                "(likely Modbus 0xFFFF corruption)",
                col,
                val,
                limit,
            )
            return False

    await db.execute(
        "INSERT INTO metrics "
        "(ts, battery_w, grid_w, solar_w, home_w, soc, gateway_id, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (time.time(), row["battery_w"], row["grid_w"], row["solar_w"],
         row["home_w"], row["soc"], gateway_id,
         row.get("ambient_temp_c"), row.get("cabinet_temp_c"),
         row.get("mode_name"), row.get("self_reserve_pct"), row.get("tou_reserve_pct"),
         row.get("grid_mode")),
    )
    await db.commit()
    return True


async def query_metrics(db: aiosqlite.Connection, range_seconds: int = 1800, gateway_id: str | None = None) -> list[dict]:
    """Query metrics for given time range, downsampling if needed.

    For ranges <= 6h return raw rows.  For longer ranges, bucket-average
    down to ~MAX_POINTS entries so the chart payload stays small.
    """
    cutoff = time.time() - range_seconds
    six_hours = 6 * 60 * 60
    gw_filter = " AND gateway_id = ?" if gateway_id else ""
    gw_params: tuple = (gateway_id,) if gateway_id else ()

    if range_seconds <= six_hours:
        # Raw data
        rows: list[dict] = []
        async with db.execute(
            "SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode "
            f"FROM metrics WHERE ts >= ?{gw_filter} ORDER BY ts",
            (cutoff,) + gw_params,
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
                        "ambient_temp_c": row[6],
                        "cabinet_temp_c": row[7],
                        "mode_name": row[8],
                        "self_reserve_pct": row[9],
                        "tou_reserve_pct": row[10],
                        "grid_mode": row[11],
                    }
                )
        return rows

    # Downsample: divide the range into MAX_POINTS buckets
    bucket_size = range_seconds / MAX_POINTS
    query = f"""
        SELECT
            CAST((ts - ?) / ? AS INTEGER) AS bucket,
            AVG(battery_w),
            AVG(grid_w),
            AVG(solar_w),
            AVG(home_w),
            AVG(soc),
            AVG(ambient_temp_c),
            AVG(cabinet_temp_c),
            MAX(mode_name),
            MAX(self_reserve_pct),
            MAX(tou_reserve_pct),
            MAX(grid_mode)
        FROM metrics
        WHERE ts >= ?{gw_filter}
        GROUP BY bucket
        ORDER BY bucket
    """
    rows = []
    async with db.execute(query, (cutoff, bucket_size, cutoff) + gw_params) as cursor:
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
                    "ambient_temp_c": round(row[6], 1) if row[6] is not None else None,
                    "cabinet_temp_c": round(row[7], 1) if row[7] is not None else None,
                    "mode_name": row[8],
                    "self_reserve_pct": row[9],
                    "tou_reserve_pct": row[10],
                    "grid_mode": row[11],
                }
            )
    return rows


async def query_metrics_daterange(
    db: aiosqlite.Connection,
    start_ts: float,
    end_ts: float,
    bucket_seconds: int | None = None,
    gateway_id: str | None = None,
) -> list[dict]:
    """Query metrics between absolute timestamps with optional bucket averaging.

    If *bucket_seconds* is given, data is averaged into buckets of that size.
    Otherwise auto-selects: raw if span <= 6h, else auto-bucket to ~MAX_POINTS.
    Transparently unions raw ``metrics`` + ``metrics_archive`` tables.
    """
    span = end_ts - start_ts
    if span <= 0:
        return []

    # Determine bucket size
    if bucket_seconds is not None:
        do_bucket = True
        bsize = bucket_seconds
    elif span <= 6 * 3600:
        do_bucket = False
        bsize = 0
    else:
        do_bucket = True
        bsize = span / MAX_POINTS

    # Check if archive table exists
    has_archive = False
    try:
        async with db.execute("SELECT 1 FROM metrics_archive LIMIT 1"):
            has_archive = True
    except Exception:
        pass

    gw_filter = " AND gateway_id = ?" if gateway_id else ""
    gw_p: list = [gateway_id] if gateway_id else []

    if not do_bucket:
        # Raw data query
        if has_archive:
            query = f"""
                SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode FROM (
                    SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode
                    FROM metrics WHERE ts >= ? AND ts <= ?{gw_filter}
                    UNION ALL
                    SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode
                    FROM metrics_archive WHERE ts >= ? AND ts <= ?{gw_filter}
                ) ORDER BY ts
            """
            params: list = [start_ts, end_ts] + gw_p + [start_ts, end_ts] + gw_p
        else:
            query = f"""
                SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode
                FROM metrics WHERE ts >= ? AND ts <= ?{gw_filter} ORDER BY ts
            """
            params = [start_ts, end_ts] + gw_p

        rows: list[dict] = []
        async with db.execute(query, params) as cursor:
            async for row in cursor:
                rows.append({
                    "ts": row[0],
                    "battery_w": row[1],
                    "grid_w": row[2],
                    "solar_w": row[3],
                    "home_w": row[4],
                    "soc": row[5],
                    "ambient_temp_c": row[6],
                    "cabinet_temp_c": row[7],
                    "mode_name": row[8],
                    "self_reserve_pct": row[9],
                    "tou_reserve_pct": row[10],
                    "grid_mode": row[11],
                })
        return rows

    # Bucketed query
    if has_archive:
        source = f"""(
            SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode
            FROM metrics WHERE ts >= ? AND ts <= ?{gw_filter}
            UNION ALL
            SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode
            FROM metrics_archive WHERE ts >= ? AND ts <= ?{gw_filter}
        )"""
        base_params: list = [start_ts, end_ts] + gw_p + [start_ts, end_ts] + gw_p
    else:
        source = f"metrics WHERE ts >= ? AND ts <= ?{gw_filter}"
        base_params = [start_ts, end_ts] + gw_p

    query = f"""
        SELECT
            CAST((ts - ?) / ? AS INTEGER) AS bucket,
            AVG(battery_w), AVG(grid_w), AVG(solar_w), AVG(home_w), AVG(soc),
            AVG(ambient_temp_c), AVG(cabinet_temp_c), MAX(mode_name),
            MAX(self_reserve_pct), MAX(tou_reserve_pct)
        FROM {source}
        GROUP BY bucket
        ORDER BY bucket
    """
    all_params = [start_ts, bsize] + base_params

    rows = []
    async with db.execute(query, all_params) as cursor:
        async for row in cursor:
            bucket_ts = start_ts + (row[0] + 0.5) * bsize
            rows.append({
                "ts": round(bucket_ts, 1),
                "battery_w": round(row[1], 1) if row[1] is not None else None,
                "grid_w": round(row[2], 1) if row[2] is not None else None,
                "solar_w": round(row[3], 1) if row[3] is not None else None,
                "home_w": round(row[4], 1) if row[4] is not None else None,
                "soc": round(row[5], 1) if row[5] is not None else None,
                "ambient_temp_c": round(row[6], 1) if row[6] is not None else None,
                "cabinet_temp_c": round(row[7], 1) if row[7] is not None else None,
                "mode_name": row[8],
                "self_reserve_pct": row[9],
                "tou_reserve_pct": row[10],
                "grid_mode": row[11],
            })
    return rows


async def purge_old(db: aiosqlite.Connection, retention_days: int = DEFAULT_RETENTION_DAYS) -> int:
    """Delete metrics older than retention (both raw and archive). Return total deleted."""
    cutoff = time.time() - (retention_days * 86400)

    cursor = await db.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
    deleted = cursor.rowcount

    # Also purge archive table
    try:
        cur2 = await db.execute(
            "DELETE FROM metrics_archive WHERE ts < ?", (cutoff,)
        )
        deleted += cur2.rowcount
    except Exception:
        pass  # table may not exist on older schemas

    await db.commit()
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


async def get_raw_age_days(db: aiosqlite.Connection) -> int:
    """Days of raw full-resolution samples kept before 5-min downsampling."""
    async with db.execute(
        "SELECT value FROM app_config WHERE key = ?", ("metrics_raw_age_days",)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return DEFAULT_RAW_AGE_DAYS
    try:
        return int(row[0])
    except (ValueError, TypeError):
        return DEFAULT_RAW_AGE_DAYS


async def set_raw_age_days(db: aiosqlite.Connection, days: int) -> None:
    """Persist the raw full-resolution window (days)."""
    await db.execute(
        "INSERT INTO app_config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("metrics_raw_age_days", str(days)),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# DB Storage stats
# ---------------------------------------------------------------------------

# Tables to report storage info for
_STORAGE_TABLES = (
    "metrics",
    "metrics_archive",
    "metric_samples",
    "control_log",
    "startup_log",
    "operational_stats",
)


async def get_storage_stats(db: aiosqlite.Connection) -> dict:
    """Return row counts and time ranges for data tables.

    Also reports the DB file size and per-table page counts if available.
    """
    tables: list[dict] = []
    for table in _STORAGE_TABLES:
        try:
            async with db.execute(
                f"SELECT COUNT(*) FROM {table}"  # noqa: S608
            ) as cur:
                count = (await cur.fetchone())[0]
        except Exception:
            continue  # table may not exist yet

        info: dict = {"table": table, "rows": count}

        # Time range (for tables with a `ts` column)
        if table in ("metrics", "metrics_archive", "metric_samples",
                      "control_log", "startup_log"):
            try:
                async with db.execute(
                    f"SELECT MIN(ts), MAX(ts) FROM {table}"  # noqa: S608
                ) as cur:
                    row = await cur.fetchone()
                    if row and row[0] is not None:
                        info["oldest_ts"] = row[0]
                        info["newest_ts"] = row[1]
                        info["span_days"] = round(
                            (row[1] - row[0]) / 86400, 1
                        )
            except Exception:
                pass

        tables.append(info)

    # DB file size
    db_size: int | None = None
    try:
        async with db.execute("PRAGMA page_count") as cur:
            page_count = (await cur.fetchone())[0]
        async with db.execute("PRAGMA page_size") as cur:
            page_size = (await cur.fetchone())[0]
        db_size = page_count * page_size
    except Exception:
        pass

    return {
        "tables": tables,
        "db_size_bytes": db_size,
        "db_size_human": _human_bytes(db_size) if db_size else None,
    }


def _human_bytes(n: int) -> str:
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n:.1f} TB"


# ---------------------------------------------------------------------------
# Metrics archive (5-minute rollup)
# ---------------------------------------------------------------------------

#: Number of seconds in one 5-minute bucket
ARCHIVE_BUCKET_S = 300

#: Raw data older than this (seconds) is eligible for archival
ARCHIVE_RAW_AGE_S = 7 * 86400  # 7 days


async def archive_old_metrics(
    db: aiosqlite.Connection,
    raw_age_s: int = ARCHIVE_RAW_AGE_S,
) -> int:
    """Downsample raw metrics older than *raw_age_s* into 5-min buckets.

    Inserts averaged rows into ``metrics_archive`` then deletes the archived
    raw rows.  Returns the number of raw rows archived.

    The cutoff is snapped *down* to a whole 5-minute bucket boundary so the
    bucket straddling the cutoff is never split across runs.  Splitting it was
    the source of two bugs: the same bucket got archived on two consecutive
    runs (duplicate archive rows), and raw rows on the boundary were left
    behind to pile up indefinitely.  The archive insert skips any bucket that
    already exists (idempotent self-heal), and the delete clears *every* raw
    row past the cutoff — including any orphaned by earlier buggy runs — so the
    raw table stays bounded to the retention window.
    """
    # Snap the cutoff down to a complete 5-min bucket so we only ever archive
    # fully-aged buckets (the straddling bucket waits until it is wholly past
    # the cutoff on a later run).
    raw_cutoff = time.time() - raw_age_s
    cutoff = (int(raw_cutoff) // ARCHIVE_BUCKET_S) * ARCHIVE_BUCKET_S

    # Anything aged out yet?
    async with db.execute(
        "SELECT COUNT(*) FROM metrics WHERE ts < ?", (cutoff,)
    ) as cur:
        eligible = (await cur.fetchone())[0]

    if eligible == 0:
        return 0

    # Aggregate complete 5-min buckets and insert into the archive, skipping
    # any bucket already present (guards against re-archiving rows orphaned by
    # earlier runs, which would otherwise create duplicate archive rows).
    await db.execute(
        """
        INSERT INTO metrics_archive (ts, battery_w, grid_w, solar_w, home_w, soc, sample_count, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode, gateway_id)
        SELECT bucket_ts, battery_w, grid_w, solar_w, home_w, soc, sample_count, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode, gateway_id
        FROM (
            SELECT
                (CAST(ts / ? AS INTEGER) * ?) + ? / 2.0 AS bucket_ts,
                ROUND(AVG(battery_w), 1) AS battery_w,
                ROUND(AVG(grid_w), 1) AS grid_w,
                ROUND(AVG(solar_w), 1) AS solar_w,
                ROUND(AVG(home_w), 1) AS home_w,
                ROUND(AVG(soc), 1) AS soc,
                COUNT(*) AS sample_count,
                ROUND(AVG(ambient_temp_c), 1) AS ambient_temp_c,
                ROUND(AVG(cabinet_temp_c), 1) AS cabinet_temp_c,
                MAX(mode_name) AS mode_name,
                MAX(self_reserve_pct) AS self_reserve_pct,
                MAX(tou_reserve_pct) AS tou_reserve_pct,
                MAX(grid_mode) AS grid_mode,
                gateway_id
            FROM metrics
            WHERE ts < ?
            GROUP BY CAST(ts / ? AS INTEGER), gateway_id
        )
        WHERE NOT EXISTS (
            SELECT 1 FROM metrics_archive ma
            WHERE ma.ts = bucket_ts AND ma.gateway_id = gateway_id
        )
        """,
        (ARCHIVE_BUCKET_S, ARCHIVE_BUCKET_S, ARCHIVE_BUCKET_S,
         cutoff, ARCHIVE_BUCKET_S),
    )

    # Delete every raw row past the cutoff (whether just archived or orphaned
    # by an earlier buggy run), keeping the raw table within the retention
    # window with no leftovers.
    cursor = await db.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
    await db.commit()

    archived = cursor.rowcount
    if archived:
        from datetime import datetime
        days = raw_age_s // 86400
        cutoff_str = datetime.fromtimestamp(cutoff).strftime("%Y-%m-%d %H:%M")
        logger.info(
            "Downsampled %d raw rows older than %dd (before %s) into 5-min "
            "buckets; full-resolution raw retained for the last %dd",
            archived, days, cutoff_str, days,
        )
    return archived


async def query_metrics_with_archive(
    db: aiosqlite.Connection,
    range_seconds: int = 1800,
    gateway_id: str | None = None,
) -> list[dict]:
    """Query metrics, transparently using archive for older ranges.

    For ranges within the raw data window, uses ``metrics`` table directly.
    For ranges that span into archived territory, unions raw + archive data.
    """
    now = time.time()
    cutoff = now - range_seconds
    six_hours = 6 * 3600

    # Check if metrics_archive table exists
    has_archive = False
    try:
        async with db.execute(
            "SELECT 1 FROM metrics_archive LIMIT 1"
        ):
            has_archive = True
    except Exception:
        pass

    if range_seconds <= six_hours or not has_archive:
        # Use original query_metrics for short ranges
        return await query_metrics(db, range_seconds, gateway_id=gateway_id)

    # For longer ranges, union raw recent + archived older data
    bucket_size = range_seconds / MAX_POINTS

    # Raw recent data (last 7 days)
    raw_cutoff = now - ARCHIVE_RAW_AGE_S

    gw_filter = " AND gateway_id = ?" if gateway_id else ""
    gw_p: tuple = (gateway_id,) if gateway_id else ()

    query = f"""
        SELECT
            CAST((ts - ?) / ? AS INTEGER) AS bucket,
            AVG(battery_w),
            AVG(grid_w),
            AVG(solar_w),
            AVG(home_w),
            AVG(soc),
            AVG(ambient_temp_c),
            AVG(cabinet_temp_c),
            MAX(mode_name),
            MAX(self_reserve_pct),
            MAX(tou_reserve_pct),
            MAX(grid_mode)
        FROM (
            SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode
            FROM metrics
            WHERE ts >= ?{gw_filter}

            UNION ALL

            SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode
            FROM metrics_archive
            WHERE ts >= ? AND ts < ?{gw_filter}
        )
        WHERE ts >= ?
        GROUP BY bucket
        ORDER BY bucket
    """
    rows: list[dict] = []
    async with db.execute(
        query,
        (cutoff, bucket_size, max(cutoff, raw_cutoff)) + gw_p + (cutoff, raw_cutoff) + gw_p + (cutoff,),
    ) as cursor:
        async for row in cursor:
            bucket_ts = cutoff + (row[0] + 0.5) * bucket_size
            rows.append({
                "ts": round(bucket_ts, 1),
                "battery_w": round(row[1], 1) if row[1] else None,
                "grid_w": round(row[2], 1) if row[2] else None,
                "solar_w": round(row[3], 1) if row[3] else None,
                "home_w": round(row[4], 1) if row[4] else None,
                "soc": round(row[5], 1) if row[5] else None,
                "ambient_temp_c": round(row[6], 1) if row[6] else None,
                "cabinet_temp_c": round(row[7], 1) if row[7] else None,
                "mode_name": row[8],
                "self_reserve_pct": row[9],
                "tou_reserve_pct": row[10],
                "grid_mode": row[11],
            })
    return rows


# ---------------------------------------------------------------------------
# Metrics export / import
# ---------------------------------------------------------------------------

#: CSV column headers for export
_EXPORT_COLUMNS = ("timestamp", "battery_w", "grid_w", "solar_w", "home_w", "soc", "ambient_temp_c", "cabinet_temp_c", "mode_name", "self_reserve_pct", "tou_reserve_pct", "grid_mode")


async def export_metrics(
    db: aiosqlite.Connection,
    range_seconds: int = 86400,
) -> list[dict]:
    """Export raw metrics rows (union of raw + archive) for the given range.

    Returns list of dicts with ISO-8601 timestamps for human readability.
    Unlike ``query_metrics``, this does NOT downsample — it returns every
    stored row so exports are lossless.
    """
    now = time.time()
    cutoff = now - range_seconds

    rows: list[dict] = []

    # Raw metrics
    async with db.execute(
        "SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode "
        "FROM metrics WHERE ts >= ? ORDER BY ts",
        (cutoff,),
    ) as cursor:
        async for row in cursor:
            rows.append(_export_row(row))

    # Archive metrics (older data in 5-min buckets)
    try:
        async with db.execute(
            "SELECT ts, battery_w, grid_w, solar_w, home_w, soc, ambient_temp_c, cabinet_temp_c, mode_name, self_reserve_pct, tou_reserve_pct, grid_mode "
            "FROM metrics_archive WHERE ts >= ? AND ts < ? ORDER BY ts",
            (cutoff, now - ARCHIVE_RAW_AGE_S),
        ) as cursor:
            async for row in cursor:
                rows.append(_export_row(row))
    except Exception:
        pass  # archive table may not exist

    # Sort combined results by timestamp
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def _export_row(row: tuple) -> dict:
    """Convert a DB row to an export dict with ISO timestamp."""
    return {
        "timestamp": datetime.fromtimestamp(row[0], tz=UTC).isoformat(),
        "battery_w": row[1],
        "grid_w": row[2],
        "solar_w": row[3],
        "home_w": row[4],
        "soc": row[5],
        "ambient_temp_c": row[6],
        "cabinet_temp_c": row[7],
        "mode_name": row[8],
        "self_reserve_pct": row[9],
        "tou_reserve_pct": row[10],
        "grid_mode": row[11],
    }


def format_metrics_csv(rows: list[dict]) -> str:
    """Format export rows as CSV string."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=_EXPORT_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


async def import_metrics_csv(
    db: aiosqlite.Connection,
    csv_text: str,
) -> int:
    """Import metrics rows from CSV text.

    Expects columns: timestamp, battery_w, grid_w, solar_w, home_w, soc.
    Timestamp can be ISO-8601 or Unix epoch float.

    Returns the number of rows imported.
    """
    reader = csv.DictReader(io.StringIO(csv_text))
    imported = 0

    for row in reader:
        ts = _parse_timestamp(row.get("timestamp", ""))
        if ts is None:
            continue

        try:
            battery_w = _to_float(row.get("battery_w"))
            grid_w = _to_float(row.get("grid_w"))
            solar_w = _to_float(row.get("solar_w"))
            home_w = _to_float(row.get("home_w"))
            soc = _to_float(row.get("soc"))
        except (ValueError, TypeError):
            continue

        # Skip completely empty rows
        if all(v is None for v in (battery_w, grid_w, solar_w, home_w, soc)):
            continue

        await db.execute(
            "INSERT INTO metrics (ts, battery_w, grid_w, solar_w, home_w, soc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (ts, battery_w, grid_w, solar_w, home_w, soc),
        )
        imported += 1

    if imported:
        await db.commit()
        logger.info("Imported %d metrics rows from CSV", imported)
    return imported


def _parse_timestamp(value: str) -> float | None:
    """Parse a timestamp string as either ISO-8601 or Unix epoch."""
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(value)
        return dt.timestamp()
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    """Convert a string to float, returning None for empty/missing."""
    if value is None or value.strip() == "":
        return None
    return float(value)


async def log_metrics_snapshot(
    db: aiosqlite.Connection,
    event: str = "metrics_snapshot",
) -> None:
    """Write a metrics-state snapshot to the startup_log.

    Captures row counts, time spans, and largest gap for both the raw
    metrics and archive tables.  Called at startup and graceful shutdown
    so operators can confirm data continuity across restarts.
    """
    now = time.time()
    lines: list[str] = []

    # ── Raw metrics ──────────────────────────────────────────────
    async with db.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM metrics"
    ) as cur:
        row = await cur.fetchone()
    raw_count = row[0] or 0
    raw_min, raw_max = row[1], row[2]

    if raw_count:
        oldest = datetime.fromtimestamp(raw_min, tz=UTC).strftime("%Y-%m-%d %H:%M")
        newest = datetime.fromtimestamp(raw_max, tz=UTC).strftime("%Y-%m-%d %H:%M")
        span_h = (raw_max - raw_min) / 3600
        lines.append(
            f"metrics: {raw_count:,} rows | {oldest} → {newest} ({span_h:.1f}h)"
        )
    else:
        lines.append("metrics: 0 rows")

    # ── Archive ──────────────────────────────────────────────────
    try:
        async with db.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM metrics_archive"
        ) as cur:
            row = await cur.fetchone()
        arc_count = row[0] or 0
        arc_min, arc_max = row[1], row[2]

        if arc_count:
            oldest = datetime.fromtimestamp(arc_min, tz=UTC).strftime("%Y-%m-%d %H:%M")
            newest = datetime.fromtimestamp(arc_max, tz=UTC).strftime("%Y-%m-%d %H:%M")
            span_d = (arc_max - arc_min) / 86400
            lines.append(
                f"archive: {arc_count:,} rows | {oldest} → {newest} ({span_d:.1f}d)"
            )
        else:
            lines.append("archive: 0 rows")
    except Exception:
        lines.append("archive: unavailable")

    # ── Largest gap in raw metrics (last 7 days) ─────────────────
    if raw_count >= 2:
        try:
            week_ago = now - 7 * 86400
            async with db.execute(
                """
                SELECT MAX(gap) FROM (
                    SELECT ts - LAG(ts) OVER (ORDER BY ts) AS gap
                    FROM metrics WHERE ts >= ?
                )
                """,
                (week_ago,),
            ) as cur:
                gap_row = await cur.fetchone()
            max_gap_s = gap_row[0] if gap_row and gap_row[0] else 0
            if max_gap_s > 120:
                gap_m = max_gap_s / 60
                lines.append(f"largest gap (7d): {gap_m:.0f}m")
            else:
                lines.append("largest gap (7d): <2m (clean)")
        except Exception:
            pass

    detail = " | ".join(lines)
    logger.info("Metrics snapshot [%s]: %s", event, detail)

    await db.execute(
        "INSERT INTO startup_log (ts, event, detail) VALUES (?, ?, ?)",
        (now, event, detail),
    )
    await db.commit()
