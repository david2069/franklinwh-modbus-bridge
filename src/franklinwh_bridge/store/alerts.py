"""Critical alerts: the stored record behind the modal, HA notifications and pushes (#324).

Shared spec with the DC bridge (its #271): field names, codes and endpoints are
the same on both bridges, so HA automations work on either. Change a name here
only after telling the DC bridge.

Two kinds:

- ``event``      something happened (a command refused). Open until acknowledged.
- ``condition``  something is wrong now (live data missing). Active until
                 resolved; acknowledging stops the modal, not the indicator.

An alert is written before anything tries to deliver it, so a failed push
never loses one. Writes never raise: bookkeeping must not break the caller.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Code:
    kind: str  # event | condition
    severity: str  # default: warning | critical
    title: str


#: The shared code catalogue (#324 spec §2). Kind and default severity are fixed.
CODES: dict[str, Code] = {
    "GATEWAY_UNREACHABLE": Code("condition", "critical", "aGate unreachable"),
    "LIVE_DATA_MISSING": Code("condition", "critical", "Live data missing"),
    "DATA_MISSING_PARTIAL": Code("condition", "warning", "Some live data missing"),
    "MQTT_DOWN": Code("condition", "warning", "MQTT broker unreachable"),
    "HA_UNREACHABLE": Code("condition", "warning", "Home Assistant unreachable"),
    "WORKER_STALLED": Code("condition", "critical", "A bridge task stopped"),
    "COMMAND_REFUSED": Code("event", "critical", "Battery command refused"),
    "COMMAND_UNCONFIRMED": Code("event", "critical", "Battery command not confirmed"),
    "WRITE_IGNORED": Code("event", "critical", "Write ignored by the aGate"),
    "SCHEDULE_FAILED": Code("event", "critical", "Scheduled action failed"),
    "RESTART_DURING_COMMAND": Code("event", "critical", "Bridge restarted during a command"),
    "EXTERNAL_SETPOINT": Code("event", "warning", "Setpoint changed by another tool"),
    "INTERNAL_ERROR": Code("event", "warning", "Internal error"),
}

#: Repeats of the same open event within this window merge into one alert.
MERGE_WINDOW_S = 600
#: Resolved or acknowledged alerts are kept this long; open ones are never pruned.
RETENTION_S = 90 * 86400

_COLUMNS = ("id, gateway_id, kind, severity, code, title, message, opened_ts, "
            "last_ts, count, resolved_ts, acked_by, acked_ts")
#: Open = still needs someone: an unacknowledged alert, or an active condition.
_OPEN = "(acked_ts IS NULL OR (kind = 'condition' AND resolved_ts IS NULL))"


def _code(code: str) -> Code:
    if code not in CODES:
        raise ValueError(f"unknown alert code: {code}")
    return CODES[code]


async def raise_event(
    db: aiosqlite.Connection, code: str, message: str, *,
    gateway_id: str = "", severity: str | None = None, now: float | None = None,
) -> int | None:
    """Record an event; merge into an open one of the same code within 10 min."""
    c = _code(code)
    if c.kind != "event":
        raise ValueError(f"{code} is a condition, not an event")
    now = time.time() if now is None else now
    try:
        async with db.execute(
            "SELECT id FROM alerts WHERE gateway_id = ? AND code = ? AND acked_ts IS NULL "
            "AND last_ts >= ? ORDER BY id DESC LIMIT 1",
            (gateway_id, code, now - MERGE_WINDOW_S),
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute(
                "UPDATE alerts SET count = count + 1, last_ts = ?, message = ? WHERE id = ?",
                (now, message, row[0]))
            await db.commit()
            return int(row[0])
        return await _insert(db, gateway_id, c, code, message, severity, now)
    except Exception as exc:
        logger.warning("Alert %s not recorded: %s", code, exc)
        return None


async def open_condition(
    db: aiosqlite.Connection, code: str, message: str, *,
    gateway_id: str = "", severity: str | None = None, now: float | None = None,
) -> int | None:
    """Open a condition, or refresh the one already active for (gateway, code)."""
    c = _code(code)
    if c.kind != "condition":
        raise ValueError(f"{code} is an event, not a condition")
    now = time.time() if now is None else now
    try:
        async with db.execute(
            "SELECT id FROM alerts WHERE gateway_id = ? AND code = ? AND resolved_ts IS NULL "
            "ORDER BY id DESC LIMIT 1", (gateway_id, code),
        ) as cur:
            row = await cur.fetchone()
        if row:
            await db.execute("UPDATE alerts SET last_ts = ?, message = ? WHERE id = ?",
                             (now, message, row[0]))
            await db.commit()
            return int(row[0])
        return await _insert(db, gateway_id, c, code, message, severity, now)
    except Exception as exc:
        logger.warning("Alert %s not recorded: %s", code, exc)
        return None


async def resolve_condition(
    db: aiosqlite.Connection, code: str, *, gateway_id: str = "", now: float | None = None,
) -> int | None:
    """Resolve the active condition for (gateway, code); its id, or None if none."""
    now = time.time() if now is None else now
    try:
        async with db.execute(
            "SELECT id FROM alerts WHERE gateway_id = ? AND code = ? AND resolved_ts IS NULL",
            (gateway_id, code),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await db.execute("UPDATE alerts SET resolved_ts = ?, last_ts = ? WHERE id = ?",
                         (now, now, row[0]))
        await db.commit()
        return int(row[0])
    except Exception as exc:
        logger.warning("Alert %s not resolved: %s", code, exc)
        return None


async def ack(db: aiosqlite.Connection, alert_id: int, acked_by: str,
              now: float | None = None) -> bool:
    """Acknowledge for everyone. False if no such alert or it was already acknowledged."""
    now = time.time() if now is None else now
    cur = await db.execute(
        "UPDATE alerts SET acked_by = ?, acked_ts = ? WHERE id = ? AND acked_ts IS NULL",
        (acked_by, now, alert_id))
    await db.commit()
    return cur.rowcount > 0


async def list_alerts(
    db: aiosqlite.Connection, *, open_only: bool = False, kind: str | None = None,
    since: float | None = None, limit: int = 200,
) -> list[dict[str, Any]]:
    """Newest first, each with its ``deliveries``."""
    where, args = [], []
    if open_only:
        where.append(_OPEN)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if since is not None:
        where.append("last_ts >= ?")
        args.append(since)
    sql = f"SELECT {_COLUMNS} FROM alerts"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_ts DESC, id DESC LIMIT ?"
    args.append(int(limit))
    names = [c.strip() for c in _COLUMNS.split(",")]
    async with db.execute(sql, args) as cur:
        rows = [dict(zip(names, r, strict=True)) for r in await cur.fetchall()]
    if rows:
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        deliveries: dict[int, list[dict[str, Any]]] = {}
        async with db.execute(
            "SELECT alert_id, channel, status, attempts, delivered_ts, last_error "
            f"FROM alert_deliveries WHERE alert_id IN ({marks})", ids,
        ) as cur:
            for a, ch, st, n, ts, err in await cur.fetchall():
                deliveries.setdefault(a, []).append({
                    "channel": ch, "status": st, "attempts": n,
                    "delivered_ts": ts, "last_error": err})
        for r in rows:
            r["deliveries"] = deliveries.get(r["id"], [])
    return rows


async def summary(db: aiosqlite.Connection) -> dict[str, Any]:
    """``{level, open, latest_id, changed_ts}`` for /api/status (spec §4, v1.1)."""
    async with db.execute(
        f"SELECT COUNT(*), SUM(severity = 'critical') FROM alerts WHERE {_OPEN}"
    ) as cur:
        n_open, n_crit = await cur.fetchone()
    async with db.execute(
        "SELECT MAX(id), MAX(MAX(last_ts, COALESCE(resolved_ts, 0), COALESCE(acked_ts, 0))) "
        "FROM alerts"
    ) as cur:
        latest_id, changed_ts = await cur.fetchone()
    n_open = int(n_open or 0)
    level = "critical" if n_crit else "warning" if n_open else "ok"
    return {"level": level, "open": n_open, "latest_id": latest_id, "changed_ts": changed_ts}


async def prune(db: aiosqlite.Connection, now: float | None = None) -> int:
    """Delete closed alerts older than 90 days. Open ones are never pruned."""
    now = time.time() if now is None else now
    cur = await db.execute(
        f"DELETE FROM alerts WHERE NOT {_OPEN} AND last_ts < ?", (now - RETENTION_S,))
    await db.commit()
    return cur.rowcount


async def _insert(db: aiosqlite.Connection, gateway_id: str, c: Code, code: str,
                  message: str, severity: str | None, now: float) -> int:
    cur = await db.execute(
        "INSERT INTO alerts (gateway_id, kind, severity, code, title, message, opened_ts, "
        "last_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (gateway_id, c.kind, severity or c.severity, code, c.title, message, now, now))
    await db.commit()
    log = logger.warning if (severity or c.severity) == "critical" else logger.info
    log("Alert %s%s: %s", code, f" [{gateway_id}]" if gateway_id else "", message)
    return int(cur.lastrowid)
