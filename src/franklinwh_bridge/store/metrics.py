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
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "18h": 18 * 60 * 60,
    "24h": 24 * 60 * 60,
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
        "(ts, battery_w, grid_w, solar_w, home_w, soc, gateway_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), row["battery_w"], row["grid_w"], row["solar_w"],
         row["home_w"], row["soc"], gateway_id),
    )
    await db.commit()
    return True


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


async def query_metrics_daterange(
    db: aiosqlite.Connection,
    start_ts: float,
    end_ts: float,
    bucket_seconds: int | None = None,
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

    if not do_bucket:
        # Raw data query
        if has_archive:
            query = """
                SELECT ts, battery_w, grid_w, solar_w, home_w, soc FROM (
                    SELECT ts, battery_w, grid_w, solar_w, home_w, soc
                    FROM metrics WHERE ts >= ? AND ts <= ?
                    UNION ALL
                    SELECT ts, battery_w, grid_w, solar_w, home_w, soc
                    FROM metrics_archive WHERE ts >= ? AND ts <= ?
                ) ORDER BY ts
            """
            params: list = [start_ts, end_ts, start_ts, end_ts]
        else:
            query = """
                SELECT ts, battery_w, grid_w, solar_w, home_w, soc
                FROM metrics WHERE ts >= ? AND ts <= ? ORDER BY ts
            """
            params = [start_ts, end_ts]

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
                })
        return rows

    # Bucketed query
    if has_archive:
        source = """(
            SELECT ts, battery_w, grid_w, solar_w, home_w, soc
            FROM metrics WHERE ts >= ? AND ts <= ?
            UNION ALL
            SELECT ts, battery_w, grid_w, solar_w, home_w, soc
            FROM metrics_archive WHERE ts >= ? AND ts <= ?
        )"""
        base_params: list = [start_ts, end_ts, start_ts, end_ts]
    else:
        source = "metrics"
        base_params = []

    where_clause = "WHERE ts >= ? AND ts <= ?" if not has_archive else ""
    extra_params = [start_ts, end_ts] if not has_archive else []

    query = f"""
        SELECT
            CAST((ts - ?) / ? AS INTEGER) AS bucket,
            AVG(battery_w), AVG(grid_w), AVG(solar_w), AVG(home_w), AVG(soc)
        FROM {source}
        {where_clause}
        GROUP BY bucket
        ORDER BY bucket
    """
    all_params = [start_ts, bsize] + base_params + extra_params

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
        INSERT INTO metrics_archive (ts, battery_w, grid_w, solar_w, home_w, soc, sample_count)
        SELECT bucket_ts, battery_w, grid_w, solar_w, home_w, soc, sample_count
        FROM (
            SELECT
                (CAST(ts / ? AS INTEGER) * ?) + ? / 2.0 AS bucket_ts,
                ROUND(AVG(battery_w), 1) AS battery_w,
                ROUND(AVG(grid_w), 1) AS grid_w,
                ROUND(AVG(solar_w), 1) AS solar_w,
                ROUND(AVG(home_w), 1) AS home_w,
                ROUND(AVG(soc), 1) AS soc,
                COUNT(*) AS sample_count
            FROM metrics
            WHERE ts < ?
            GROUP BY CAST(ts / ? AS INTEGER)
        )
        WHERE bucket_ts NOT IN (SELECT ts FROM metrics_archive)
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
        logger.info(
            "Archived %d raw metric rows into 5-min buckets (cutoff=%.0f)",
            archived, cutoff,
        )
    return archived


async def query_metrics_with_archive(
    db: aiosqlite.Connection,
    range_seconds: int = 1800,
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
        return await query_metrics(db, range_seconds)

    # For longer ranges, union raw recent + archived older data
    bucket_size = range_seconds / MAX_POINTS

    # Raw recent data (last 7 days)
    raw_cutoff = now - ARCHIVE_RAW_AGE_S

    query = """
        SELECT
            CAST((ts - ?) / ? AS INTEGER) AS bucket,
            AVG(battery_w),
            AVG(grid_w),
            AVG(solar_w),
            AVG(home_w),
            AVG(soc)
        FROM (
            SELECT ts, battery_w, grid_w, solar_w, home_w, soc
            FROM metrics
            WHERE ts >= ?

            UNION ALL

            SELECT ts, battery_w, grid_w, solar_w, home_w, soc
            FROM metrics_archive
            WHERE ts >= ? AND ts < ?
        )
        WHERE ts >= ?
        GROUP BY bucket
        ORDER BY bucket
    """
    rows: list[dict] = []
    async with db.execute(
        query,
        (cutoff, bucket_size, max(cutoff, raw_cutoff),
         cutoff, raw_cutoff, cutoff),
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
            })
    return rows


# ---------------------------------------------------------------------------
# Metrics export / import
# ---------------------------------------------------------------------------

#: CSV column headers for export
_EXPORT_COLUMNS = ("timestamp", "battery_w", "grid_w", "solar_w", "home_w", "soc")


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
        "SELECT ts, battery_w, grid_w, solar_w, home_w, soc "
        "FROM metrics WHERE ts >= ? ORDER BY ts",
        (cutoff,),
    ) as cursor:
        async for row in cursor:
            rows.append(_export_row(row))

    # Archive metrics (older data in 5-min buckets)
    try:
        async with db.execute(
            "SELECT ts, battery_w, grid_w, solar_w, home_w, soc "
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
