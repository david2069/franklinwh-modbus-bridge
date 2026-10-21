"""Modbus write audit trail (#342 part 1): every SunSpec write, as sent and as answered.

One row per write the bridge's SunSpec device makes: when (ms), register (PDU
address), the raw register values, the aGate's reply (``ok`` or the error, e.g.
"Modbus exception 3"), and who asked (``user:<name>``, ``schedule:<id>``,
``sequencer:user:<name>``, ...). Always on.

On 2026-10-21 the evidence for #323 (V12 refusing WSetPct above raw 100) had to
be rebuilt from screenshots and a second Modbus client; this is that record.

The write happens in a worker thread (pysunspec2 is blocking), so the hook only
queues the row; ``flush`` writes the queue to SQLite from the event loop.
Writes the library makes on its own raw socket (the 155xx extension writes) do
not pass through the SunSpec device and are not captured here.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import json
import logging
import struct
import threading
import time
from collections.abc import Iterator
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

#: Rows kept: 90 days, and at most this many (about 20 MB).
RETENTION_S = 90 * 86400
MAX_ROWS = 200_000
#: Rows waiting for ``flush`` if the database is slow; the oldest go first.
QUEUE_MAX = 10_000

#: Who is writing, for the rows made in this context: {"gateway_id", "initiator"}.
#: ``asyncio.to_thread`` copies the context, so a value set in the async caller
#: reaches the worker thread that makes the write.
AUDIT_CONTEXT: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "modbus_audit_context", default=None)

_queue: collections.deque[dict[str, Any]] = collections.deque(maxlen=QUEUE_MAX)
_lock = threading.Lock()


@contextlib.contextmanager
def writing_as(gateway_id: str, initiator: str) -> Iterator[None]:
    """Attribute the writes made inside this block (and its to_thread calls)."""
    token = AUDIT_CONTEXT.set({"gateway_id": gateway_id or "", "initiator": initiator or ""})
    try:
        yield
    finally:
        AUDIT_CONTEXT.reset(token)


def _words(data: bytes | bytearray | None) -> list[int]:
    if not data:
        return []
    n = len(data) // 2
    return list(struct.unpack(f">{n}h", bytes(data[: n * 2])))


def record_write(address: int, data: bytes | bytearray | None, error: BaseException | None,
                 *, now: float | None = None) -> None:
    """Queue one write and log it. Called from the device's ``write``; never raises."""
    try:
        ctx = AUDIT_CONTEXT.get() or {}
        row = {
            "ts": time.time() if now is None else now,
            "gateway_id": ctx.get("gateway_id", ""),
            "initiator": ctx.get("initiator") or "library",
            "address": int(address),
            "values": _words(data),
            "result": "ok" if error is None else "error",
            "detail": None if error is None else (str(error) or type(error).__name__),
        }
        with _lock:
            _queue.append(row)
        log = logger.info if error is None else logger.warning
        log("Modbus write @%d %s -> %s (%s)", row["address"], row["values"],
            row["detail"] or "ok", row["initiator"])
    except Exception as exc:  # the audit must never break a write
        logger.debug("Modbus write not audited: %s", exc)


async def flush(db: aiosqlite.Connection) -> int:
    """Write queued rows to the database; the number written."""
    with _lock:
        rows = list(_queue)
        _queue.clear()
    if not rows:
        return 0
    try:
        await db.executemany(
            "INSERT INTO modbus_writes (ts, gateway_id, initiator, address, \"values\", "
            "result, detail) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(r["ts"], r["gateway_id"], r["initiator"], r["address"], json.dumps(r["values"]),
              r["result"], r["detail"]) for r in rows])
        await db.commit()
    except Exception as exc:
        logger.warning("Modbus write audit: %d row(s) not saved: %s", len(rows), exc)
        return 0
    return len(rows)


async def prune(db: aiosqlite.Connection, now: float | None = None) -> int:
    """Drop rows older than 90 days, then the oldest beyond MAX_ROWS."""
    now = time.time() if now is None else now
    cur = await db.execute("DELETE FROM modbus_writes WHERE ts < ?", (now - RETENTION_S,))
    n = cur.rowcount
    cur = await db.execute(
        "DELETE FROM modbus_writes WHERE id NOT IN "
        "(SELECT id FROM modbus_writes ORDER BY id DESC LIMIT ?)", (MAX_ROWS,))
    await db.commit()
    return n + cur.rowcount


async def query(db: aiosqlite.Connection, *, since: float | None = None,
                until: float | None = None, gateway_id: str | None = None,
                limit: int = 1000) -> list[dict[str, Any]]:
    """Rows oldest first within the range."""
    where, args = [], []
    if since is not None:
        where.append("ts >= ?")
        args.append(since)
    if until is not None:
        where.append("ts <= ?")
        args.append(until)
    if gateway_id:
        where.append("gateway_id = ?")
        args.append(gateway_id)
    sql = ("SELECT id, ts, gateway_id, initiator, address, \"values\", result, detail "
           "FROM modbus_writes")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    async with db.execute(sql, args) as cur:
        rows = await cur.fetchall()
    out = [{"id": r[0], "ts": r[1], "gateway_id": r[2], "initiator": r[3], "address": r[4],
            "values": json.loads(r[5] or "[]"), "result": r[6], "detail": r[7]} for r in rows]
    out.reverse()
    return out
