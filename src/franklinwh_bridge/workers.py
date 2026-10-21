"""Worker health (#324 §11 C): the bridge's background loops, each proving life by beat().

A worker is a loop that should keep running: a gateway's poller, the schedule
engine, the health checker, the log flush, the alert monitor. Each registers
with its cadence and calls :func:`beat` once per pass. ``beat()`` is the only
proof of life: a loop blocked in a read that never returns, or one that died,
stops beating.

A worker is stale when it hasn't beaten for **max(4 × cadence, 30 s)** (spec
v1.1). :meth:`WorkerRegistry.check` turns that, and a task that ended with an
exception, into state transitions; the alert monitor raises WORKER_STALLED on
them. Shape shared with the DC bridge's ``workers.py``.

Detection only for now: restarting a stalled worker is a later step.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

STALE_FLOOR_S = 30.0

RUNNING = "RUNNING"
UNRESPONSIVE = "UNRESPONSIVE"
CRASHED = "CRASHED"
#: A worker whose task ended without an error (a clean stop). Not a fault.
STOPPED = "STOPPED"
BROKEN = frozenset({UNRESPONSIVE, CRASHED})


def stale_after(cadence_s: float) -> float:
    """Seconds without a beat before a worker counts as stalled."""
    return max(4 * cadence_s, STALE_FLOOR_S)


@dataclass
class Worker:
    name: str
    concern: str
    cadence_s: float
    scope: str = ""  # a gateway id, or "" for the bridge itself
    state: str = RUNNING
    last_beat_ts: float = field(default_factory=time.time)
    last_error: str | None = None
    task: asyncio.Task | None = None

    def to_dict(self, now: float) -> dict[str, Any]:
        return {
            "name": self.name, "concern": self.concern, "scope": self.scope,
            "cadence_s": self.cadence_s, "stale_after_s": stale_after(self.cadence_s),
            "state": self.state, "last_beat_ts": self.last_beat_ts,
            "age_s": round(now - self.last_beat_ts, 1), "last_error": self.last_error,
        }


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}

    def register(self, name: str, concern: str, cadence_s: float, *, scope: str = "",
                 task: asyncio.Task | None = None) -> Worker:
        """Add (or replace) a worker. It counts as beating from now."""
        w = Worker(name, concern, float(cadence_s), scope, task=task)
        self._workers[name] = w
        return w

    def unregister(self, name: str) -> None:
        self._workers.pop(name, None)

    def beat(self, name: str, now: float | None = None) -> None:
        """Proof of life. A name that isn't registered is ignored."""
        w = self._workers.get(name)
        if w is not None:
            w.last_beat_ts = time.time() if now is None else now

    def workers(self) -> list[Worker]:
        return list(self._workers.values())

    def check(self, now: float | None = None) -> list[tuple[Worker, str, str]]:
        """Recompute every worker's state; the ``(worker, old, new)`` that changed."""
        now = time.time() if now is None else now
        changes = []
        for w in self.workers():
            new = self._state_of(w, now)
            if new != w.state:
                changes.append((w, w.state, new))
                w.state = new
        return changes

    @staticmethod
    def _state_of(w: Worker, now: float) -> str:
        t = w.task
        if t is not None and t.done():
            if t.cancelled():
                return STOPPED
            exc = t.exception()
            if exc is None:
                return STOPPED
            w.last_error = f"{type(exc).__name__}: {exc}"
            return CRASHED
        if now - w.last_beat_ts > stale_after(w.cadence_s):
            return UNRESPONSIVE
        return RUNNING

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        """``{ok, counts, stale[], broken[], workers[]}`` for /api/health/workers."""
        now = time.time() if now is None else now
        rows = [w.to_dict(now) for w in sorted(self.workers(), key=lambda w: w.name)]
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["state"]] = counts.get(r["state"], 0) + 1
        return {
            "ok": not any(r["state"] in BROKEN for r in rows),
            "counts": counts,
            "stale": [r["name"] for r in rows if r["state"] == UNRESPONSIVE],
            "broken": [r["name"] for r in rows if r["state"] == CRASHED],
            "workers": rows,
        }


#: The bridge's one registry. Loops beat it by name, so nothing needs plumbing.
REGISTRY = WorkerRegistry()
register = REGISTRY.register
unregister = REGISTRY.unregister
beat = REGISTRY.beat
