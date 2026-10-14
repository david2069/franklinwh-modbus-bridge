"""Persistence for :mod:`franklinwh_bridge.gateway.link_stats` (Phase 0b).

The collector keeps hourly buckets in memory; :func:`flush` writes the hours
that have closed (and, at shutdown, the partial current hour). Rows are only
ever inserted, never updated, so a flush can't corrupt an earlier hour.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import aiosqlite

from franklinwh_bridge.gateway import link_stats as collect

logger = logging.getLogger(__name__)

#: Design §5.4: kept 90 days.
RETENTION_DAYS = 90

_REQ_COLS = (
    "count", "ok", "empty", "timeout_no_reply", "timeout_closed", "error",
    "error_classes", "lat_p50_ms", "lat_p95_ms", "lat_max_ms",
)
_LINK_COLS = (
    "conns_opened", "reconnects", "reconnects_failed", "rescans", "rescans_failed",
    "rescan_max_ms",
    "lib_retries", "error_classes", "max_consec_fail", "newest_good_age_s",
    "polls", "cache_polls", "overlaps", "overlap_pairs", "max_in_flight",
    "max_established",
)
_JSON_COLS = ("error_classes", "conns_opened", "overlap_pairs")


def _rows(hour: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten one collector hour into table rows (JSON columns still dicts)."""
    base = {
        "gateway_id": hour["gateway_id"],
        "hour_start": hour["hour_start"],
        "partial": 1 if hour["partial"] else 0,
    }
    out = []
    for (kind, path), r in sorted(hour["requests"].items()):
        out.append({**base, "kind": kind, "path": path, **{c: r[c] for c in _REQ_COLS}})
    link = hour["link"]
    out.append({**base, "kind": "link", "path": "", **{c: link[c] for c in _LINK_COLS}})
    return out


async def write_hours(db: aiosqlite.Connection, hours: list[dict[str, Any]]) -> int:
    n = 0
    for hour in hours:
        for row in _rows(hour):
            cols = list(row)
            vals = [json.dumps(row[c], sort_keys=True) if c in _JSON_COLS else row[c]
                    for c in cols]
            await db.execute(
                f"INSERT INTO link_stats ({', '.join(cols)}) "
                f"VALUES ({', '.join('?' * len(cols))})",
                vals,
            )
            n += 1
    await db.commit()
    return n


async def flush(db: aiosqlite.Connection, final: bool = False) -> int:
    """Write every collector's closed hours (and the open one when *final*).

    Never raises: a failed write puts the hours back for the next attempt.
    """
    total = 0
    for c in collect.collectors():
        hours = c.drain(final=final)
        if not hours:
            continue
        try:
            total += await write_hours(db, hours)
        except Exception as exc:
            logger.warning("link_stats flush failed for %s: %s", c.gateway_id, exc)
            c.restore(hours)
    return total


async def purge(db: aiosqlite.Connection, retention_days: int = RETENTION_DAYS) -> int:
    cur = await db.execute(
        "DELETE FROM link_stats WHERE hour_start < ?",
        (time.time() - retention_days * 86400,),
    )
    await db.commit()
    return cur.rowcount or 0


async def query(db: aiosqlite.Connection, gateway_id: str, since: float) -> list[dict]:
    """Stored rows for a gateway since *since*, oldest first, JSON decoded."""
    async with db.execute(
        "SELECT * FROM link_stats WHERE gateway_id = ? AND hour_start >= ? "
        "ORDER BY hour_start, kind, path, id",
        (gateway_id, since),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    for r in rows:
        for c in _JSON_COLS:
            if r.get(c):
                r[c] = json.loads(r[c])
    return rows


def open_hour(gateway_id: str) -> list[dict[str, Any]]:
    """The hour still being collected, in the shape of stored rows."""
    c = collect.find(gateway_id)
    return [r for hour in c.peek() for r in _rows(hour)] if c else []
