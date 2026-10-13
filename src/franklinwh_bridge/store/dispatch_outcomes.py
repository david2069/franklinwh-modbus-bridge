"""Truthful record of every battery command (design PR #57 §5.1, §5.4).

One row per command sent to a gateway. ``state`` moves through:

- ``sent``          the write was accepted and WSetEna read back as 1
- ``failed``        the write failed, or WSetEna did not read back as 1
- ``verified``      the battery was seen moving the commanded way
- ``not_effective`` setpoint accepted but the battery did not move (#26)
- ``reasserted``    the aGate dropped the setpoint and it was re-sent
- ``released``      the command was released (``release_unverified`` if
                    WSetEna still read 1 afterwards)
- ``superseded``    replaced by a newer command

The scheduler and the command handler write it; nothing reads it back to make
decisions, so a write failure is logged and never blocks a dispatch.
"""

from __future__ import annotations

import logging
import time
import uuid

import aiosqlite

logger = logging.getLogger(__name__)

FINAL_STATES = frozenset(
    {"failed", "not_effective", "released", "release_unverified", "superseded"}
)


async def create(
    db: aiosqlite.Connection,
    *,
    gateway_id: str,
    requester: str,
    action: str,
    requested_w: int,
    state: str,
    reason: str | None = None,
    client_ref: str | None = None,
) -> str | None:
    """Insert a row; return its id (None if the write failed)."""
    did = f"dsp_{uuid.uuid4().hex[:12]}"
    now = time.time()
    try:
        await db.execute(
            "INSERT INTO dispatch_outcomes (id, gateway_id, requester, client_ref, action, "
            "requested_w, created_at, updated_at, state, reason, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, gateway_id, requester, client_ref, action, int(requested_w), now, now,
             state, reason, now if state in FINAL_STATES else None),
        )
        await db.commit()
        return did
    except Exception as exc:  # never block a dispatch on bookkeeping
        logger.warning("dispatch_outcomes insert failed: %s", exc)
        return None


async def update(
    db: aiosqlite.Connection,
    dispatch_id: str | None,
    state: str,
    *,
    reason: str | None = None,
    actual_w: float | None = None,
) -> None:
    """Move a row to ``state``. A no-op for an unknown or None id."""
    if not dispatch_id:
        return
    now = time.time()
    try:
        await db.execute(
            "UPDATE dispatch_outcomes SET state = ?, updated_at = ?, "
            "reason = COALESCE(?, reason), actual_w = COALESCE(?, actual_w), "
            "verified_at = CASE WHEN ? = 'verified' THEN ? ELSE verified_at END, "
            "ended_at = CASE WHEN ? THEN ? ELSE ended_at END "
            "WHERE id = ?",
            (state, now, reason, actual_w, state, now,
             1 if state in FINAL_STATES else 0, now, dispatch_id),
        )
        await db.commit()
    except Exception as exc:
        logger.warning("dispatch_outcomes update failed: %s", exc)


async def recent(
    db: aiosqlite.Connection, gateway_id: str | None = None, limit: int = 100
) -> list[dict]:
    """Newest first, optionally for one gateway."""
    db.row_factory = aiosqlite.Row
    q = "SELECT * FROM dispatch_outcomes"
    args: tuple = ()
    if gateway_id:
        q += " WHERE gateway_id = ?"
        args = (gateway_id,)
    q += " ORDER BY created_at DESC LIMIT ?"
    async with db.execute(q, (*args, max(1, min(limit, 1000)))) as cur:
        return [dict(r) for r in await cur.fetchall()]
