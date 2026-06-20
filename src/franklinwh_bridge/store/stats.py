"""Persistent operational statistics for the bridge.

Tracks uptime, poll counts, error rates, data quality metrics, and
connection health across restarts.  Counters accumulate in-memory and
flush to SQLite periodically (every ``FLUSH_INTERVAL_S`` seconds) to
avoid per-poll DB writes.

Usage::

    stats = await OperationalStats.load(db)
    stats.record_poll("ok")
    stats.record_sanitization()
    ...
    await stats.flush()       # explicit flush (also called periodically)
    snapshot = stats.snapshot  # read current state
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

logger = logging.getLogger(__name__)

#: Seconds between automatic DB flushes.
FLUSH_INTERVAL_S = 60


@dataclass
class StatsSnapshot:
    """Immutable point-in-time view of operational stats."""

    started_at: float
    uptime_s: float
    polls_ok: int
    polls_stale: int
    polls_error: int
    polls_total: int
    samples_recorded: int
    samples_rejected: int
    sanitizations: int
    conn_drops: int
    conn_recoveries: int
    mqtt_sent: int
    last_poll_ts: float | None
    last_error: str | None

    def to_dict(self) -> dict:
        """Serialise for REST/JSON responses."""
        return {
            "started_at": self.started_at,
            "uptime_s": round(self.uptime_s, 1),
            "uptime_human": _format_duration(self.uptime_s),
            "polls_ok": self.polls_ok,
            "polls_stale": self.polls_stale,
            "polls_error": self.polls_error,
            "polls_total": self.polls_total,
            "samples_recorded": self.samples_recorded,
            "samples_rejected": self.samples_rejected,
            "sanitizations": self.sanitizations,
            "conn_drops": self.conn_drops,
            "conn_recoveries": self.conn_recoveries,
            "mqtt_sent": self.mqtt_sent,
            "last_poll_ts": self.last_poll_ts,
            "last_error": self.last_error,
            "data_quality_pct": (
                round(100 * self.polls_ok / self.polls_total, 1)
                if self.polls_total > 0
                else 100.0
            ),
        }


def _format_duration(seconds: float) -> str:
    """Human-readable duration like '3d 4h 12m'."""
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, _ = divmod(s, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


class OperationalStats:
    """In-memory counters with periodic SQLite persistence."""

    def __init__(self, db: aiosqlite.Connection, *, started_at: float) -> None:
        self._db = db
        self._started_at = started_at

        # Counters (accumulated from DB + in-session increments)
        self._polls_ok: int = 0
        self._polls_stale: int = 0
        self._polls_error: int = 0
        self._samples_recorded: int = 0
        self._samples_rejected: int = 0
        self._sanitizations: int = 0
        self._conn_drops: int = 0
        self._conn_recoveries: int = 0
        self._mqtt_sent: int = 0
        self._last_poll_ts: float | None = None
        self._last_error: str | None = None

        self._last_flush_ts: float = time.time()
        self._dirty: bool = False

    # ── Factory ─────────────────────────────────────────────

    @classmethod
    async def load(cls, db: aiosqlite.Connection) -> OperationalStats:
        """Load persisted stats or initialise a fresh row."""
        now = time.time()
        async with db.execute(
            "SELECT * FROM operational_stats WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()

        stats = cls(db, started_at=now)

        if row is not None:
            # Accumulate from previous session
            stats._polls_ok = row["polls_ok"]
            stats._polls_stale = row["polls_stale"]
            stats._polls_error = row["polls_error"]
            stats._samples_recorded = row["samples_recorded"]
            stats._samples_rejected = row["samples_rejected"]
            stats._sanitizations = row["sanitizations"]
            stats._conn_drops = row["conn_drops"]
            stats._conn_recoveries = row["conn_recoveries"]
            stats._mqtt_sent = row["mqtt_sent"]
            stats._last_poll_ts = row["last_poll_ts"]
            stats._last_error = row["last_error"]
            logger.info(
                "Loaded operational stats: %d polls, %d errors from previous session",
                stats._polls_ok + stats._polls_stale + stats._polls_error,
                stats._polls_error,
            )

        # Update started_at to current session
        await stats._upsert_row()
        return stats

    # ── Recording ───────────────────────────────────────────

    def record_poll(self, quality: str) -> None:
        """Record a poll result by quality."""
        if quality == "ok":
            self._polls_ok += 1
        elif quality == "stale":
            self._polls_stale += 1
        else:
            self._polls_error += 1
        self._last_poll_ts = time.time()
        self._dirty = True

    def record_error(self, error: str) -> None:
        """Record a poll-loop exception (not a quality='error' poll)."""
        self._polls_error += 1
        self._last_error = error
        self._last_poll_ts = time.time()
        self._dirty = True

    def record_sample_recorded(self) -> None:
        """A metrics sample was successfully written to DB."""
        self._samples_recorded += 1
        self._dirty = True

    def record_sample_rejected(self) -> None:
        """A metrics sample was rejected by the sanity guard."""
        self._samples_rejected += 1
        self._dirty = True

    def record_sanitization(self) -> None:
        """An extension value was sanitized (0xFFFF or out-of-range)."""
        self._sanitizations += 1
        self._dirty = True

    def record_conn_drop(self) -> None:
        """Modbus connection was lost."""
        self._conn_drops += 1
        self._dirty = True

    def record_conn_recovery(self) -> None:
        """Modbus connection was recovered."""
        self._conn_recoveries += 1
        self._dirty = True

    def record_mqtt_sent(self, count: int = 1) -> None:
        """MQTT messages published."""
        self._mqtt_sent += count
        self._dirty = True

    # ── Snapshot ────────────────────────────────────────────

    @property
    def snapshot(self) -> StatsSnapshot:
        """Current stats as an immutable snapshot."""
        return StatsSnapshot(
            started_at=self._started_at,
            uptime_s=time.time() - self._started_at,
            polls_ok=self._polls_ok,
            polls_stale=self._polls_stale,
            polls_error=self._polls_error,
            polls_total=self._polls_ok + self._polls_stale + self._polls_error,
            samples_recorded=self._samples_recorded,
            samples_rejected=self._samples_rejected,
            sanitizations=self._sanitizations,
            conn_drops=self._conn_drops,
            conn_recoveries=self._conn_recoveries,
            mqtt_sent=self._mqtt_sent,
            last_poll_ts=self._last_poll_ts,
            last_error=self._last_error,
        )

    # ── Persistence ─────────────────────────────────────────

    async def flush(self) -> None:
        """Write current counters to DB if dirty."""
        if not self._dirty:
            return
        await self._upsert_row()
        self._dirty = False
        self._last_flush_ts = time.time()

    async def maybe_flush(self) -> None:
        """Flush if enough time has elapsed since last flush."""
        if self._dirty and (time.time() - self._last_flush_ts) >= FLUSH_INTERVAL_S:
            await self.flush()

    async def _upsert_row(self) -> None:
        """Insert or update the singleton stats row."""
        await self._db.execute(
            """
            INSERT INTO operational_stats (
                id, started_at,
                polls_ok, polls_stale, polls_error,
                samples_recorded, samples_rejected, sanitizations,
                conn_drops, conn_recoveries, mqtt_sent,
                last_poll_ts, last_error, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                started_at = excluded.started_at,
                polls_ok = excluded.polls_ok,
                polls_stale = excluded.polls_stale,
                polls_error = excluded.polls_error,
                samples_recorded = excluded.samples_recorded,
                samples_rejected = excluded.samples_rejected,
                sanitizations = excluded.sanitizations,
                conn_drops = excluded.conn_drops,
                conn_recoveries = excluded.conn_recoveries,
                mqtt_sent = excluded.mqtt_sent,
                last_poll_ts = excluded.last_poll_ts,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (
                self._started_at,
                self._polls_ok,
                self._polls_stale,
                self._polls_error,
                self._samples_recorded,
                self._samples_rejected,
                self._sanitizations,
                self._conn_drops,
                self._conn_recoveries,
                self._mqtt_sent,
                self._last_poll_ts,
                self._last_error,
                time.time(),
            ),
        )
        await self._db.commit()
