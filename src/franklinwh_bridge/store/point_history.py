"""Per-point history — the electrical readings ``metrics`` doesn't keep.

``metrics`` stores a fixed thirteen columns (the four power channels, SoC,
temperatures, mode). Everything else the poller reads — voltage, current,
frequency, power factor, VA, var, the DC-side figures — existed only as a live
value, so the AC and DC charts could show the present and nothing else. Asking
them for "the last six hours" had no data to answer with.

``metric_samples (gateway_id, point_id, ts, value, quality)`` has been in the
schema, indexed, and unused since the beginning; this is the writer and reader
it was waiting for.

**Deliberate small duplication.** A few points recorded here (``soc``,
``grid_power_w``, the temperatures) are already columns in ``metrics``. Keeping
them lets the history API answer for *every* series a chart offers from one
table, instead of the caller stitching two sources with different shapes and
bucket semantics. Three extra points is a cheap price for one code path.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiosqlite

from franklinwh_bridge.store.db import get_app_config, set_app_config

logger = logging.getLogger(__name__)

_CONFIG_KEY = "point_history"

#: Bytes per row including its index entry, measured rather than guessed
#: (200k rows into an identical schema). Used to project storage in Settings
#: so the cost of a choice is visible before it is made, not after.
BYTES_PER_ROW = 97

#: Points recorded to ``metric_samples``. Per-phase L1/L2/L3 variants are
#: deliberately excluded — they triple the row count and are redundant on a
#: single-phase site. Add them here if a split/three-phase install needs them.
HISTORISED_POINTS: tuple[str, ...] = (
    # AC
    "voltage_v",
    "current_a",
    "frequency_hz",
    "power_factor",
    "grid_va",
    "grid_var",
    "grid_power_w",
    # DC / inverter
    "dc_power_w",
    "battery_dc_power_w",
    "battery_1_voltage_v",
    "battery_current_a",
    "soh",
    "soc",
    "cabinet_temp_c",
    "ambient_temp_c",
)

DEFAULTS: dict[str, Any] = {
    # Off by default. This can add hundreds of MB to a database that is
    # otherwise ~20MB, and a bridge running as an HA add-on on an SD card
    # should not start consuming that because it was upgraded.
    "enabled": False,
    "points": list(HISTORISED_POINTS),
    "interval_s": 10,
    "retention_days": 21,
}


async def get_config(db: aiosqlite.Connection) -> dict:
    """User settings for point history, merged over the defaults."""
    raw = await get_app_config(db, _CONFIG_KEY, None)
    cfg = dict(DEFAULTS)
    if raw:
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict):
                cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (ValueError, TypeError):
            logger.warning("Ignoring unreadable %s config", _CONFIG_KEY)
    # Guard rails: a 1s interval across every point is ~1.4M rows/day, and a
    # zero or negative one would busy-write.
    #
    # `x or default` is wrong here: it maps an explicit 0 to the DEFAULT rather
    # than to the minimum, so typing 0 would quietly give 10s instead of the 5s
    # floor. Absent means default; present-but-silly means clamped.
    def _bounded(value: Any, default: int, lo: int, hi: int) -> int:
        if value is None or value == "":
            value = default
        try:
            return max(lo, min(int(float(value)), hi))
        except (TypeError, ValueError):
            return default

    cfg["interval_s"] = _bounded(cfg["interval_s"], 10, 5, 3600)
    cfg["retention_days"] = _bounded(cfg["retention_days"], 21, 1, 365)
    known = set(HISTORISED_POINTS)
    cfg["points"] = [p for p in (cfg["points"] or []) if p in known]
    return cfg


async def set_config(db: aiosqlite.Connection, updates: dict) -> dict:
    """Patch the config (only known keys) and return the effective result."""
    cfg = await get_config(db)
    cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
    await set_app_config(db, _CONFIG_KEY, json.dumps(cfg))
    return await get_config(db)


def project_bytes(point_count: int, interval_s: float, retention_days: float) -> int:
    """Projected storage for a configuration, so the UI can show it live."""
    if interval_s <= 0:
        return 0
    rows = (86400.0 / interval_s) * point_count * retention_days
    return int(rows * BYTES_PER_ROW)


#: Rejects Modbus sentinels and impossible readings before they reach storage,
#: where they would otherwise stretch every future chart's axis. Bounds are
#: generous — this is a sanity filter, not a spec check.
_LIMITS: dict[str, tuple[float, float]] = {
    "voltage_v": (0.0, 1000.0),
    "current_a": (-1000.0, 1000.0),
    "frequency_hz": (0.0, 100.0),
    "power_factor": (-1.0, 1.0),
    "soh": (0.0, 100.0),
    "soc": (0.0, 100.0),
    "cabinet_temp_c": (-40.0, 120.0),
    "ambient_temp_c": (-40.0, 120.0),
    "battery_1_voltage_v": (0.0, 1500.0),
    "battery_current_a": (-1000.0, 1000.0),
}
_DEFAULT_LIMIT = (-100_000.0, 100_000.0)   # watts / VA / var


def _clean(point_id: str, value: Any) -> float | None:
    """Coerce to a storable float, or None to skip this point this tick."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):   # NaN / inf
        return None
    lo, hi = _LIMITS.get(point_id, _DEFAULT_LIMIT)
    return v if lo <= v <= hi else None


async def record_points(
    db: aiosqlite.Connection,
    points: dict,
    gateway_id: str = "default",
    ts: float | None = None,
    point_ids: list[str] | None = None,
) -> int:
    """Append one row per historised point present in *points*.

    Returns the number of rows written. A point that is absent, non-numeric or
    out of range is skipped rather than stored as null — a gap in the series is
    honest, a null row is a value that has to be filtered by every reader.
    """
    stamp = time.time() if ts is None else ts
    rows = []
    for pid in (point_ids if point_ids is not None else HISTORISED_POINTS):
        v = _clean(pid, points.get(pid))
        if v is not None:
            rows.append((gateway_id, pid, stamp, v, "ok"))

    if not rows:
        return 0

    await db.executemany(
        "INSERT INTO metric_samples (gateway_id, point_id, ts, value, quality) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    await db.commit()
    return len(rows)


async def query_points(
    db: aiosqlite.Connection,
    point_ids: list[str],
    start_ts: float,
    end_ts: float,
    bucket_seconds: float | None = None,
    gateway_id: str = "default",
) -> dict[str, list[dict]]:
    """Read series for *point_ids* between two timestamps.

    Returns ``{point_id: [{"ts": float, "value": float}, ...]}``. With
    *bucket_seconds* the values are averaged into buckets, which is what makes
    a multi-day range drawable — six hours at 10s is 2,160 points per series,
    a week is 60,000, and no screen has that many pixels.
    """
    if not point_ids or end_ts <= start_ts:
        return {}

    placeholders = ",".join("?" for _ in point_ids)
    out: dict[str, list[dict]] = {pid: [] for pid in point_ids}

    if bucket_seconds and bucket_seconds > 0:
        query = f"""
            SELECT point_id,
                   CAST((ts - ?) / ? AS INTEGER) AS bucket,
                   AVG(value)
            FROM metric_samples
            WHERE point_id IN ({placeholders})
              AND gateway_id = ? AND ts >= ? AND ts <= ?
            GROUP BY point_id, bucket
            ORDER BY bucket
        """
        params = [start_ts, bucket_seconds, *point_ids, gateway_id, start_ts, end_ts]
        async with db.execute(query, params) as cur:
            async for pid, bucket, avg in cur:
                out[pid].append({
                    "ts": round(start_ts + (bucket + 0.5) * bucket_seconds, 1),
                    "value": round(avg, 4) if avg is not None else None,
                })
        return out

    query = f"""
        SELECT point_id, ts, value
        FROM metric_samples
        WHERE point_id IN ({placeholders})
          AND gateway_id = ? AND ts >= ? AND ts <= ?
        ORDER BY ts
    """
    async with db.execute(query, [*point_ids, gateway_id, start_ts, end_ts]) as cur:
        async for pid, ts, value in cur:
            out[pid].append({"ts": round(ts, 1), "value": value})
    return out


async def purge_points(db: aiosqlite.Connection, retention_days: int) -> int:
    """Delete samples older than retention. Returns rows deleted."""
    cutoff = time.time() - (retention_days * 86400)
    cursor = await db.execute("DELETE FROM metric_samples WHERE ts < ?", (cutoff,))
    deleted = cursor.rowcount
    await db.commit()
    if deleted:
        logger.info(
            "Purged %d point-history rows older than %d days", deleted, retention_days
        )
    return deleted


async def storage_stats(db: aiosqlite.Connection) -> dict:
    """Row count and span, for the Settings storage panel."""
    async with db.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts), COUNT(DISTINCT point_id) FROM metric_samples"
    ) as cur:
        rows, oldest, newest, points = await cur.fetchone()
    return {
        "rows": rows or 0,
        "points": points or 0,
        "oldest_ts": oldest,
        "newest_ts": newest,
        "span_days": round((newest - oldest) / 86400, 2) if rows and oldest else 0.0,
    }
