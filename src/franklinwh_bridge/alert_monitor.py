"""The alert monitor (#324 §11 C): conditions that open and resolve on their own.

Every 15 s it looks at each running gateway and at the worker registry, and
opens or resolves conditions in the alert store:

========================  ==========================================  ===========
Code                      Bad when                                    Open / good
========================  ==========================================  ===========
GATEWAY_UNREACHABLE       the health checker's TCP probe fails        5 / 2 min
LIVE_DATA_MISSING         no poll has read anything for 3 polls       2 / 1 min
DATA_MISSING_PARTIAL      the 15500-15509 block, seen before, is      2 / 1 min
                          missing from the reads that do work
WORKER_STALLED            a worker in that scope is UNRESPONSIVE or   at once /
                          CRASHED (``workers.py`` does the waiting)   at once
========================  ==========================================  ===========

A condition already open when the bridge starts is picked up as open, so a
fault that ended while the bridge was down resolves on the first good minute.
Events (a refused command) are raised at their source, not here.

It is not the scheduler, the poller or any worker it watches; it runs as its
own task and is itself a worker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite

from franklinwh_bridge import workers
from franklinwh_bridge.publish import bridge_device
from franklinwh_bridge.store import alerts as alert_store

if TYPE_CHECKING:
    from franklinwh_bridge.gateway.instance import GatewayInstance
    from franklinwh_bridge.gateway.registry import GatewayRegistry

logger = logging.getLogger(__name__)

INTERVAL_S = 15
WORKER_NAME = "alert_monitor"
#: Polls without a read before live data counts as missing (then 2 min to open).
LIVE_DATA_POLLS = 3
#: The "Bridge heartbeat" sensor is refreshed this often (spec v1.1).
HEARTBEAT_S = 60
#: An INTERNAL_ERROR from a failed pass at most this often.
INTERNAL_ERROR_EVERY_S = 3600

#: (open after, resolve after) in seconds, from the shared catalogue (spec §2).
THRESHOLDS: dict[str, tuple[float, float]] = {
    "GATEWAY_UNREACHABLE": (300, 120),
    "LIVE_DATA_MISSING": (120, 60),
    "DATA_MISSING_PARTIAL": (120, 60),
}


@dataclass
class Debounce:
    """Opens after ``open_after_s`` of bad, resolves after ``resolve_after_s`` of good."""

    open_after_s: float
    resolve_after_s: float
    active: bool = False
    _bad_since: float | None = None
    _good_since: float | None = None

    def update(self, bad: bool, now: float) -> str | None:
        """``"open"``, ``"resolve"`` or None (no change)."""
        if bad:
            self._good_since = None
            if self._bad_since is None:
                self._bad_since = now
            if not self.active and now - self._bad_since >= self.open_after_s:
                self.active = True
                return "open"
        else:
            self._bad_since = None
            if self._good_since is None:
                self._good_since = now
            if self.active and now - self._good_since >= self.resolve_after_s:
                self.active = False
                return "resolve"
        return None


def gateway_signals(inst: GatewayInstance, now: float) -> dict[str, bool]:
    """Which of the gateway's conditions are bad right now."""
    poller = inst.poller
    state = getattr(poller, "state", None)
    interval = max(float(inst.config.poll_interval or 10), 1.0)
    # The mock poller has no last_good_ts: every one of its polls succeeds.
    last_good = getattr(state, "last_good_ts", getattr(state, "last_poll_ts", None))
    live_bad = last_good is None or now - last_good > max(LIVE_DATA_POLLS * interval, 30)
    return {
        "GATEWAY_UNREACHABLE": inst.status.health == "unreachable",
        "LIVE_DATA_MISSING": live_bad,
        # With nothing read at all, LIVE_DATA_MISSING already says it.
        "DATA_MISSING_PARTIAL": not live_bad and bool(getattr(state, "block_missing", False)),
    }


def _message(code: str, inst: GatewayInstance) -> str:
    name = inst.config.name or inst.gateway_id
    where = f"{name} ({inst.config.host}:{inst.config.port})"
    if code == "GATEWAY_UNREACHABLE":
        return (f"{where} hasn't answered for 5 minutes. Commands and schedules can't "
                f"reach it. Check that the aGate has power and is on the network.")
    if code == "LIVE_DATA_MISSING":
        return (f"No live data from {where} for over 2 minutes: every read is failing. "
                f"Values in the bridge and Home Assistant are not current.")
    return (f"{where}: registers 15500-15509 (solar, home load, mode, reserves) have "
            f"been missing for over 2 minutes. Other readings still arrive.")


class AlertMonitor:
    """Watches gateways and workers; opens and resolves conditions."""

    def __init__(self, db: aiosqlite.Connection, registry: GatewayRegistry,
                 interval_s: float = INTERVAL_S,
                 worker_registry: workers.WorkerRegistry | None = None,
                 publisher: Callable[[], Any] | None = None) -> None:
        self._db = db
        #: Returns the MQTT publisher (or None): it is replaced on reconfigure.
        self._publisher = publisher or (lambda: None)
        self._last_heartbeat = 0.0
        self._registry = registry
        self._interval = interval_s
        self._workers = worker_registry or workers.REGISTRY
        self._debounce: dict[tuple[str, str], Debounce] = {}
        #: (scope, code) of the conditions open in the store.
        self._open: set[tuple[str, str]] = set()
        self._last_internal_ts = 0.0
        #: The WORKER_STALLED message last written per scope; rewritten only on change.
        self._stalled_msg: dict[str, str] = {}
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        await self.load_open()
        self._task = asyncio.create_task(self._loop())
        self._workers.register(WORKER_NAME, "Alert monitor", self._interval, task=self._task)
        logger.info("Alert monitor started (interval=%ds)", self._interval)

    async def stop(self) -> None:
        self._workers.unregister(WORKER_NAME)
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def load_open(self) -> None:
        """Pick up conditions left open by the last run, so they can resolve."""
        try:
            rows = await alert_store.list_alerts(self._db, kind="condition", open_only=True,
                                                 limit=1000)
        except Exception as exc:
            logger.warning("Alert monitor: open conditions not loaded: %s", exc)
            return
        for r in rows:
            if r["resolved_ts"] is not None:
                continue
            key = (r["gateway_id"], r["code"])
            self._open.add(key)
            if r["code"] in THRESHOLDS:
                self._debounce[key] = Debounce(*THRESHOLDS[r["code"]], active=True)

    async def _loop(self) -> None:
        try:
            while True:
                self._workers.beat(WORKER_NAME)
                try:
                    await self.check()
                except Exception as exc:  # one bad pass must not end the monitor
                    logger.exception("Alert monitor pass failed")
                    await self._internal_error(exc)
                await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            pass

    async def check(self, now: float | None = None) -> None:
        """One pass: gateway conditions, then worker health."""
        now = time.time() if now is None else now
        running: set[str] = set()
        for gw_id, inst in list(self._registry.instances.items()):
            # A started gateway, polling or not: one that never connected is
            # exactly the one to report (spec v1.2).
            if not inst.config.enabled:
                continue
            running.add(gw_id)
            for code, bad in gateway_signals(inst, now).items():
                d = self._debounce.setdefault((gw_id, code), Debounce(*THRESHOLDS[code]))
                change = d.update(bad, now)
                if change == "open":
                    await self._open_condition(code, _message(code, inst), gw_id, now)
                elif change == "resolve":
                    await self._resolve(code, gw_id, now)
        # A gateway that was stopped, disabled or removed has no fault to report.
        for gw_id, code in list(self._open):
            if code in THRESHOLDS and gw_id not in running:
                self._debounce.pop((gw_id, code), None)
                await self._resolve(code, gw_id, now)
        await self._check_workers(now)
        await self._publish_bridge_sensors(now)

    async def _publish_bridge_sensors(self, now: float) -> None:
        """The bridge device's "Bridge problem" (on change) and heartbeat (60 s)."""
        pub = self._publisher()
        if pub is None:
            return
        summary = await alert_store.summary(self._db)
        open_alerts = await alert_store.list_alerts(
            self._db, open_only=True, limit=bridge_device.MAX_ALERTS)
        pub.publish_bridge_problem(bridge_device.problem_payload(summary, open_alerts))
        # Counted only once sent: the first pass after a start runs before MQTT
        # connects, and HA showed "unknown" for a minute (G1 0.2.37).
        if now - self._last_heartbeat >= HEARTBEAT_S and pub.publish_bridge_heartbeat(now):
            self._last_heartbeat = now

    async def _check_workers(self, now: float) -> None:
        for w, old, new in self._workers.check(now):
            log = logger.warning if new in workers.BROKEN else logger.info
            log("Worker %s: %s → %s", w.name, old, new)
        broken: dict[str, list[workers.Worker]] = {}
        for w in self._workers.workers():
            if w.state in workers.BROKEN:
                broken.setdefault(w.scope, []).append(w)
        for scope, ws in broken.items():
            # No live seconds in the text: it would change every pass.
            parts = [
                f"{w.concern} ({w.name}) stopped with an error: {w.last_error}"
                if w.state == workers.CRASHED else
                f"{w.concern} ({w.name}) has not run for over "
                f"{workers.stale_after(w.cadence_s):.0f} s"
                for w in ws]
            msg = "; ".join(parts) + "."
            if self._stalled_msg.get(scope) != msg or (scope, "WORKER_STALLED") not in self._open:
                self._stalled_msg[scope] = msg
                await self._open_condition("WORKER_STALLED", msg, scope, now)
        for scope, code in list(self._open):
            if code == "WORKER_STALLED" and scope not in broken:
                self._stalled_msg.pop(scope, None)
                await self._resolve(code, scope, now)

    async def _open_condition(self, code: str, message: str, gw_id: str, now: float) -> None:
        if (gw_id, code) in self._open and code != "WORKER_STALLED":
            return
        # WORKER_STALLED refreshes its message as workers recover or stall.
        if await alert_store.open_condition(self._db, code, message, gateway_id=gw_id,
                                            now=now) is not None:
            self._open.add((gw_id, code))

    async def _resolve(self, code: str, gw_id: str, now: float) -> None:
        await alert_store.resolve_condition(self._db, code, gateway_id=gw_id, now=now)
        self._open.discard((gw_id, code))
        logger.info("Alert %s%s resolved", code, f" [{gw_id}]" if gw_id else "")

    async def _internal_error(self, exc: Exception) -> None:
        now = time.time()
        if now - self._last_internal_ts < INTERNAL_ERROR_EVERY_S:
            return
        self._last_internal_ts = now
        await alert_store.raise_event(
            self._db, "INTERNAL_ERROR",
            f"The alert monitor hit an error and carried on: {type(exc).__name__}: {exc}")
