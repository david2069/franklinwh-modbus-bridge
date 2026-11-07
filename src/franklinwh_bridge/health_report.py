"""Scheduled health check (#296 item 51, owner 2026-11-07), phase 1.

Two things, both read-only (nothing here talks to the aGate or writes to a battery):

- **Daily snapshot** (table ``health_daily``): one row per gateway per site day, read
  in the first pass after midnight with fresh readings from that gateway, plus a row
  for the bridge itself (gateway ``''``) with its log counts. A gateway with no fresh
  reading all the next day gets no row for that day. Kept for good, so the weekly
  battery report (phase 2) has a trend longer than the 30-day purges allow.
- **Daily bridge-health report** (table ``health_reports``) at 07:00 site time: green,
  amber or red, the reason on the first line. It looks at the last 24 hours against
  the 7 days before: workers, the gateway link, alerts, log patterns, housekeeping.

Where the report goes: ``GET /api/health/report``; one Home Assistant notification,
replacing the last (``fwh_mb_health_daily``); and the phones, only when it is amber or
red. Both follow the Push notifications switch (``alerts.ha_channel``). The report
itself has its own switch, ``health.daily`` ("on" by default).

If the bridge was down at 07:00, the day's report runs on the first pass after it
starts. Rules for the colours are in :func:`grade`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from franklinwh_bridge import workers
from franklinwh_bridge.alert_delivery import NOTIFICATION_TITLE, ha_channel_on, site_time
from franklinwh_bridge.store import alerts as alert_store
from franklinwh_bridge.store import link_stats as link_stats_store
from franklinwh_bridge.store.db import (
    gateway_is_unconfigured,
    get_app_config,
    get_gateways,
    set_app_config,
)

logger = logging.getLogger(__name__)

DAILY_KEY = "health.daily"
DAILY_AT = (7, 0)  # site time
INTERVAL_S = 60
WORKER_NAME = "health_report"
NOTIFICATION_ID = "fwh_mb_health_daily"
REPORT_RETENTION_DAYS = 400
#: Readings older than this are not current: the gateway's row waits for a fresh one.
FRESH_S = 300
SEND_TIMEOUT_S = 10.0

GREEN, AMBER, RED = "green", "amber", "red"
_RANK = {GREEN: 0, AMBER: 1, RED: 2}

# Log patterns: a pattern this often in 24 h counts for the colour.
PATTERN_MIN = 10
#: Growing: today more than this many times the 7-day daily average.
GROWING_FACTOR = 2.0
#: Repeating all day: seen in at least this many of the 24 hours.
ALL_DAY_HOURS = 20
#: Link: failed requests at or above this share (of at least LINK_MIN_REQUESTS).
LINK_FAIL_SHARE = 0.05
LINK_MIN_REQUESTS = 100
TOP_PATTERNS = 15

# Snapshot columns ← point keys (the poller's).
_COUNTERS = {
    "soh": "soh",
    "wh_rating": "wh_rating",
    "wh_available": "wh_available",
    "soc": "soc",
    "battery_charged_wh": "dc_energy_charged_wh",
    "battery_discharged_wh": "dc_energy_discharged_wh",
    "grid_import_wh": "grid_import_wh",
    "grid_export_wh": "grid_export_wh",
    "solar_wh": "pv_energy_total_wh",
    "inverter_out_wh": "701.TotWhInj",
    "inverter_in_wh": "701.TotWhAbs",
}
_SNAP_COLS = ("gateway_id", "day", "ts", *_COUNTERS, "alerts", "alarms",
              "log_warnings", "log_errors")

#: A secret in a log line (the generated admin password, a token) is never copied into a
#: report, which is kept far longer than the log and may be sent to phones.
_SECRET = re.compile(r"(?i)\b(password|passwd|token|secret|api[ _-]?key)"
                     r"(\b[^:=\n]{0,20}?[:=]\s*|\s+)(?!<hidden>)\S+")
_ADDR = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b|https?://\S+")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_NUM = re.compile(r"\b0x[0-9a-f]+\b|(?<![\w.])[-+]?\d+(?:\.\d+)?", re.I)
_SPACE = re.compile(r"\s+")


def redact(message: str) -> str:
    """The message with any password, token or key after its keyword hidden."""
    return _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<hidden>", message or "")


def pattern(message: str) -> str:
    """A log message with its numbers, addresses and ids taken out, so repeats of
    one message group together: "Server error '500' … in 4.0 s" → "Server error '#' … in # s"."""
    s = _ADDR.sub("<addr>", redact(message))
    s = _UUID.sub("<id>", s)
    s = _NUM.sub("#", s)
    return _SPACE.sub(" ", s).strip()[:200]


def _day(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def _midnight(dt: datetime) -> datetime:
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


async def daily_on(db: aiosqlite.Connection) -> bool:
    return (await get_app_config(db, DAILY_KEY, "on")) != "off"


async def set_daily(db: aiosqlite.Connection, on: bool) -> None:
    await set_app_config(db, DAILY_KEY, "on" if on else "off")
    logger.info("Daily health report switched %s", "on" if on else "off")


# ── snapshot ──────────────────────────────────────────────────


async def _count(db: aiosqlite.Connection, sql: str, args: tuple) -> int:
    async with db.execute(sql, args) as cur:
        row = await cur.fetchone()
    return int(row[0] or 0)


async def take_snapshot(db: aiosqlite.Connection, day: str, start: float, end: float,
                        gateways: list[str], points_fn: Callable[[str], dict],
                        now: float) -> int:
    """Write ``day``'s rows (``start``..``end`` = that day's span) if missing. Returns
    rows written. A gateway's row waits until its points are fresh (just after a
    restart they aren't yet), so its counters are never left empty by timing."""
    written = 0
    async with db.execute("SELECT gateway_id FROM health_daily WHERE day = ?", (day,)) as cur:
        have = {r[0] for r in await cur.fetchall()}
    rows = []
    if "" not in have:
        rows.append({
            "gateway_id": "", "day": day, "ts": now,
            "alerts": await _count(db, "SELECT COUNT(*) FROM alerts WHERE opened_ts >= ? "
                                   "AND opened_ts < ?", (start, end)),
            "alarms": await _count(db, "SELECT COUNT(*) FROM alarm_events WHERE ts >= ? "
                                   "AND ts < ? AND alarms_set != ''", (start, end)),
            "log_warnings": await _count(db, "SELECT COUNT(*) FROM logs WHERE ts >= ? AND "
                                         "ts < ? AND level = 'WARNING'", (start, end)),
            "log_errors": await _count(db, "SELECT COUNT(*) FROM logs WHERE ts >= ? AND "
                                       "ts < ? AND level IN ('ERROR', 'CRITICAL')", (start, end)),
        })
    for gw in gateways:
        if gw in have:
            continue
        pts = {}
        with contextlib.suppress(Exception):
            pts = points_fn(gw) or {}
        sample_ts = pts.get("_sample_ts")
        if sample_ts is None or now - float(sample_ts) > FRESH_S:
            continue
        row: dict[str, Any] = {"gateway_id": gw, "day": day, "ts": now}
        for col, key in _COUNTERS.items():
            v = pts.get(key)
            row[col] = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
        row["alerts"] = await _count(db, "SELECT COUNT(*) FROM alerts WHERE gateway_id = ? "
                                     "AND opened_ts >= ? AND opened_ts < ?", (gw, start, end))
        row["alarms"] = await _count(db, "SELECT COUNT(*) FROM alarm_events WHERE "
                                     "gateway_id = ? AND ts >= ? AND ts < ? AND "
                                     "alarms_set != ''", (gw, start, end))
        row["log_warnings"] = await _count(db, "SELECT COUNT(*) FROM logs WHERE gateway_id = ? "
                                           "AND ts >= ? AND ts < ? AND level = 'WARNING'",
                                           (gw, start, end))
        row["log_errors"] = await _count(db, "SELECT COUNT(*) FROM logs WHERE gateway_id = ? "
                                         "AND ts >= ? AND ts < ? AND level IN ('ERROR', "
                                         "'CRITICAL')", (gw, start, end))
        rows.append(row)
    for row in rows:
        await db.execute(
            f"INSERT OR IGNORE INTO health_daily ({', '.join(_SNAP_COLS)}) "
            f"VALUES ({', '.join('?' * len(_SNAP_COLS))})",
            tuple(row.get(c) for c in _SNAP_COLS))
        written += 1
    if rows:
        await db.commit()
    return written


async def snapshots(db: aiosqlite.Connection, gateway_id: str | None = None,
                    days: int = 31) -> list[dict]:
    """Newest first; ``gateway_id`` '' is the bridge's own row."""
    sql, args = "SELECT * FROM health_daily", []
    if gateway_id is not None:
        sql += " WHERE gateway_id = ?"
        args.append(gateway_id)
    sql += " ORDER BY day DESC, gateway_id LIMIT ?"
    args.append(int(days) * (1 if gateway_id is not None else 50))
    async with db.execute(sql, args) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ── the daily report ──────────────────────────────────────────


async def _log_patterns(db: aiosqlite.Connection, now: float) -> list[dict]:
    """Warnings and errors of the last 24 h, grouped by pattern, each against its
    own count over the 7 days before."""
    day_start, week_start = now - 86400, now - 8 * 86400
    groups: dict[tuple[str, str], dict] = {}
    async with db.execute(
        "SELECT ts, level, name, message FROM logs WHERE ts >= ? AND ts <= ? AND level IN "
        "('WARNING', 'ERROR', 'CRITICAL') ORDER BY ts LIMIT 100000", (week_start, now),
    ) as cur:
        rows = await cur.fetchall()
    for ts, level, name, message in rows:
        level = "ERROR" if level == "CRITICAL" else level
        message = redact(message)
        key = (level, pattern(message))
        g = groups.get(key)
        if g is None:
            g = groups[key] = {"level": level, "pattern": key[1], "logger": name or "",
                               "example": message, "count": 0, "prior_7d": 0,
                               "first_ts": None, "last_ts": None, "_hours": set()}
        if ts >= day_start:
            g["count"] += 1
            g["first_ts"] = g["first_ts"] or ts
            g["last_ts"] = ts
            g["example"] = message
            g["_hours"].add(int((ts - day_start) // 3600))
        else:
            g["prior_7d"] += 1
    out = []
    for g in groups.values():
        if not g["count"]:
            continue
        hours = len(g.pop("_hours"))
        avg = g["prior_7d"] / 7
        g["hours"] = hours
        g["per_hour"] = round(g["count"] / max(hours, 1), 1)
        flags = []
        if not g["prior_7d"]:
            flags.append("new")
        elif g["count"] > GROWING_FACTOR * avg:
            flags.append("growing")
        if hours >= ALL_DAY_HOURS:
            flags.append("all day")
        g["flags"] = flags
        out.append(g)
    out.sort(key=lambda g: (g["level"] != "ERROR", -g["count"]))
    return out


def _sum_link(rows: list[dict]) -> dict:
    t = defaultdict(int)
    for r in rows:
        if r["kind"] == "link":
            for c in ("reconnects", "reconnects_failed", "lib_retries", "polls", "cache_polls"):
                t[c] += int(r.get(c) or 0)
            t["max_consec_fail"] = max(t["max_consec_fail"], int(r.get("max_consec_fail") or 0))
        else:
            t["requests"] += int(r.get("count") or 0)
            t["empty"] += int(r.get("empty") or 0)
            t["failed"] += sum(int(r.get(c) or 0)
                               for c in ("timeout_no_reply", "timeout_closed", "error"))
    return dict(t)


async def _link(db: aiosqlite.Connection, gw: str, now: float) -> dict:
    rows = [r for r in await link_stats_store.query(db, gw, now - 8 * 86400)
            if r["hour_start"] < now]
    with contextlib.suppress(Exception):
        rows += link_stats_store.open_hour(gw)
    day = _sum_link([r for r in rows if r["hour_start"] >= now - 86400])
    prior = _sum_link([r for r in rows if r["hour_start"] < now - 86400])
    req = day.get("requests", 0)
    return {
        **{k: day.get(k, 0) for k in ("requests", "failed", "empty", "reconnects",
                                      "reconnects_failed", "lib_retries", "polls",
                                      "cache_polls", "max_consec_fail")},
        "failed_share": round(day.get("failed", 0) / req, 4) if req else None,
        "prior_7d_failed_per_day": round(prior.get("failed", 0) / 7, 1),
    }


def grade(report: dict) -> tuple[str, list[str]]:
    """``(level, reasons)``, worst first.

    - **Red:** a worker stalled or crashed; a critical alert open now.
    - **Amber:** a warning alert open now; any error in the log; a warning pattern
      that is new, growing or all day and seen at least ``PATTERN_MIN`` times; a
      gateway whose failed requests are 5 % or more.
    - **Green:** none of those.
    """
    found: list[tuple[str, str]] = []
    w = report["workers"]
    for name in w["broken"]:
        found.append((RED, f"worker {name} crashed"))
    for name in w["stale"]:
        found.append((RED, f"worker {name} stopped beating"))
    a = report["alerts"]
    for al in a["open"]:
        sev = RED if al["severity"] == "critical" else AMBER
        where = f" ({al['gateway']})" if al.get("gateway") else ""
        found.append((sev, f"alert open: {al['title']}{where}"))
    for p in report["logs"]["patterns"]:
        if p["level"] == "ERROR":
            found.append((AMBER, f"{p['count']} × error: {p['example'][:120]}"))
        elif p["flags"] and p["count"] >= PATTERN_MIN:
            found.append((AMBER, f"{p['count']} × warning ({', '.join(p['flags'])}): "
                                 f"{p['example'][:120]}"))
    for g in report["link"]:
        share = g.get("failed_share")
        if share is not None and g["requests"] >= LINK_MIN_REQUESTS and share >= LINK_FAIL_SHARE:
            found.append((AMBER, f"{g['name']}: {share:.0%} of Modbus requests failed"))
    found.sort(key=lambda f: -_RANK[f[0]])
    level = found[0][0] if found else GREEN
    return level, [r for _, r in found]


def _headline(level: str, reasons: list[str], report: dict) -> str:
    if reasons:
        more = f" (+{len(reasons) - 1} more)" if len(reasons) > 1 else ""
        return f"{level.capitalize()}: {reasons[0]}{more}"
    logs = report["logs"]
    return (f"Green: all {report['workers']['count']} workers running, no alert open, "
            f"{logs['warnings']} warnings in 24 h")


async def build_daily(db: aiosqlite.Connection, now: float,
                      backups: Callable[[], list] | None = None,
                      db_path: Path | None = None) -> dict:
    """The daily bridge-health report as a dict (not stored)."""
    since = now - 86400
    wsnap = workers.REGISTRY.snapshot(now)
    async with db.execute(
        "SELECT ts, event, detail FROM startup_log WHERE event = 'startup' AND ts >= ? "
        "ORDER BY ts", (since,)) as cur:
        starts = [{"ts": r[0], "detail": r[2]} for r in await cur.fetchall()]

    # Not the placeholder gateway with no address: it never polls.
    gws = [g for g in await get_gateways(db)
           if g.get("enabled", 1) and not gateway_is_unconfigured(g)]
    link = []
    for g in gws:
        link.append({"gateway": g["id"], "name": g.get("name") or g["id"],
                     **await _link(db, g["id"], now)})

    raised: dict[str, dict] = {}
    for al in await alert_store.list_alerts(db, since=since, limit=1000):
        if al["opened_ts"] < since and al["last_ts"] < since:
            continue
        r = raised.setdefault(al["code"], {"code": al["code"], "title": al["title"],
                                           "severity": al["severity"], "alerts": 0,
                                           "repeats": 0})
        r["alerts"] += 1
        r["repeats"] += int(al["count"] or 1)
    open_now = [{"id": al["id"], "code": al["code"], "title": al["title"],
                 "severity": al["severity"], "gateway": al["gateway_id"],
                 "opened_ts": al["opened_ts"]}
                for al in await alert_store.list_alerts(db, open_only=True, limit=200)]

    patterns = await _log_patterns(db, now)
    logs = {
        "warnings": sum(p["count"] for p in patterns if p["level"] == "WARNING"),
        "errors": sum(p["count"] for p in patterns if p["level"] == "ERROR"),
        "pattern_count": len(patterns),
        "patterns": patterns[:TOP_PATTERNS],
    }

    house: dict[str, Any] = {"db_bytes": None, "log_rows": None, "last_backup_ts": None}
    if db_path is not None:
        with contextlib.suppress(OSError):
            house["db_bytes"] = sum(p.stat().st_size for p in
                                    (db_path, Path(f"{db_path}-wal")) if p.exists())
    house["log_rows"] = await _count(db, "SELECT COUNT(*) FROM logs", ())
    if backups is not None:
        with contextlib.suppress(Exception):
            bl = backups()
            house["last_backup_ts"] = bl[0].created_at if bl else None

    report = {
        "kind": "daily",
        "window": {"from": since, "to": now},
        "workers": {"count": len(wsnap["workers"]), "broken": wsnap["broken"],
                    "stale": wsnap["stale"], "restarts": len(starts), "starts": starts},
        "link": link,
        "alerts": {"raised": sorted(raised.values(), key=lambda r: -r["repeats"]),
                   "open": open_now},
        "logs": logs,
        "housekeeping": house,
    }
    level, reasons = grade(report)
    report["level"] = level
    report["reasons"] = reasons
    report["headline"] = _headline(level, reasons, report)
    return report


async def store_report(db: aiosqlite.Connection, report: dict, period: str, now: float) -> int:
    cur = await db.execute(
        "INSERT INTO health_reports (kind, period, ts, level, headline, body) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (report["kind"], period, now, report["level"], report["headline"], json.dumps(report)))
    await db.execute("DELETE FROM health_reports WHERE ts < ?",
                     (now - REPORT_RETENTION_DAYS * 86400,))
    await db.commit()
    return int(cur.lastrowid)


def _row(r: aiosqlite.Row, body: bool) -> dict:
    d = {k: r[k] for k in ("id", "kind", "period", "ts", "level", "headline")}
    if body:
        d["report"] = json.loads(r["body"])
    return d


async def latest_report(db: aiosqlite.Connection, kind: str = "daily") -> dict | None:
    async with db.execute("SELECT * FROM health_reports WHERE kind = ? ORDER BY ts DESC, "
                          "id DESC LIMIT 1", (kind,)) as cur:
        r = await cur.fetchone()
    return _row(r, True) if r else None


async def get_report(db: aiosqlite.Connection, report_id: int) -> dict | None:
    async with db.execute("SELECT * FROM health_reports WHERE id = ?", (report_id,)) as cur:
        r = await cur.fetchone()
    return _row(r, True) if r else None


async def list_reports(db: aiosqlite.Connection, kind: str | None = None,
                       limit: int = 60) -> list[dict]:
    sql, args = "SELECT * FROM health_reports", []
    if kind:
        sql += " WHERE kind = ?"
        args.append(kind)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(int(limit))
    async with db.execute(sql, args) as cur:
        return [_row(r, False) for r in await cur.fetchall()]


def notification_text(report: dict) -> tuple[str, str]:
    """``(title, message)``: the headline, then the other reasons."""
    level = report["level"]
    title = f"{NOTIFICATION_TITLE}: health {level}"
    lines = [report["headline"]]
    lines += [f"- {r}" for r in report["reasons"][1:6]]
    if len(report["reasons"]) > 6:
        lines.append(f"- and {len(report['reasons']) - 6} more")
    lines.append("Open the bridge → Health for the full report.")
    return title, "\n".join(lines)


# ── the job ───────────────────────────────────────────────────


class HealthReporter:
    """The built-in job: snapshot after midnight, the daily report at 07:00."""

    def __init__(self, db: aiosqlite.Connection, *, gateways_fn: Callable[[], list[str]],
                 points_fn: Callable[[str], dict], ha_registry: Any = None,
                 actionable: Any = None, backups: Callable[[], list] | None = None,
                 db_path: Path | None = None, now_fn: Callable[[], float] = time.time,
                 interval_s: float = INTERVAL_S) -> None:
        self._db = db
        self._gateways = gateways_fn
        self._points = points_fn
        self._ha = ha_registry
        self._act = actionable
        self._backups = backups
        self._db_path = db_path
        self._now = now_fn
        self._interval = interval_s
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())
            workers.register(WORKER_NAME, "Health check", self._interval, task=self._task)

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        workers.unregister(WORKER_NAME)

    async def _loop(self) -> None:
        while True:
            workers.beat(WORKER_NAME)
            try:
                await self.tick()
            except Exception as exc:  # the next pass tries again
                logger.warning("Health check pass failed: %s", exc)
            await asyncio.sleep(self._interval)

    async def tick(self) -> dict | None:
        """One pass: yesterday's snapshot if missing; today's report if it is due."""
        now = self._now()
        local = datetime.fromtimestamp(now)
        today = _midnight(local)
        yesterday = today - timedelta(days=1)
        await take_snapshot(self._db, _day(yesterday), yesterday.timestamp(),
                            today.timestamp(), list(self._gateways()), self._points, now)
        due = today.replace(hour=DAILY_AT[0], minute=DAILY_AT[1])
        if local < due or not await daily_on(self._db):
            return None
        async with self._db.execute(
            "SELECT 1 FROM health_reports WHERE kind = 'daily' AND period = ?",
            (_day(local),)) as cur:
            if await cur.fetchone():
                return None
        return await self.run(notify=True)

    async def run(self, *, notify: bool) -> dict:
        """Build, store and (when ``notify``) send today's daily report."""
        async with self._lock:
            now = self._now()
            report = await build_daily(self._db, now, self._backups, self._db_path)
            report_id = await store_report(self._db, report, _day(datetime.fromtimestamp(now)),
                                           now)
            logger.info("Daily health report: %s", report["headline"])
            if notify:
                await self._deliver(report, now)
            return {"id": report_id, "ts": now, "level": report["level"],
                    "headline": report["headline"], "report": report}

    async def _deliver(self, report: dict, now: float) -> None:
        """HA notification (replacing the last) and, for amber or red, the phones.
        Follows the Push notifications switch. Never raises."""
        title, message = notification_text(report)
        try:
            if not await ha_channel_on(self._db):
                logger.info("Health report not sent (push notifications off)")
                return
            inst = self._ha.primary() if self._ha is not None else None
            if inst is not None:
                res = await asyncio.wait_for(
                    inst.call_action("persistent_notification", "create", {
                        "notification_id": NOTIFICATION_ID, "title": title,
                        "message": f"{message}\n\n{site_time(now)}"}),
                    SEND_TIMEOUT_S)
                if not res["ok"]:
                    logger.info("Health report not sent to Home Assistant: %s", res["error"])
            if report["level"] != GREEN and self._act is not None:
                res = await asyncio.wait_for(self._act.notify_health(title, message),
                                             SEND_TIMEOUT_S)
                logger.info("Health report to phones: %s", getattr(res, "summary", res))
        except Exception as exc:  # a report must never break the job
            logger.info("Health report not sent: %s", str(exc) or type(exc).__name__)
