"""Connectivity monitor — first-class per-gateway outage tracking (scheduler v2).

The aGate drops off the LAN when it fails WiFi→4G, so the Bridge silently loses
local Modbus reach and a scheduled window can pass unnoticed. This turns that
into an observable event: an *outage* opens when no successful poll has arrived
within ``outage_threshold_s`` and closes on the next good poll, at which point a
recovery callback (the catch-up pass) runs.

Signal source: the global ``SampleBus``. The poller publishes a ``Sample`` every
cycle with ``quality`` ``ok``/``stale``/``error``; an ``ok`` sample is a
confirmed successful read at ``sample.ts``. During a hard outage the poller stops
emitting ``ok`` samples entirely, so a time-based staleness check (``tick``) is
what actually opens the outage — ``on_sample`` alone can't, since no sample may
arrive. Recovery is edge-triggered by the first ``ok`` sample.

This is intentionally separate from ``HealthChecker`` (which does active TCP
probes for the UI badge): this monitor exists to drive schedule catch-up and to
give the scheduler an outage-aware health view, keyed off the same poll data the
engine already consumes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from franklinwh_bridge.store.db import (
    close_outage,
    create_outage,
    get_recent_outages,
)

logger = logging.getLogger(__name__)

DEFAULT_OUTAGE_THRESHOLD_S = 60

# Recovery callback: (gateway_id, outage_id, start_ts, end_ts) -> awaitable.
# Wired to the catch-up pass in a later slice; None until then.
RecoverCallback = Callable[[str, str, float, float], Awaitable[None]]


class ConnectivityMonitor:
    """Tracks per-gateway last-good-poll time and opens/closes outage records."""

    def __init__(
        self,
        db: Any,
        *,
        outage_threshold_s: int = DEFAULT_OUTAGE_THRESHOLD_S,
        on_recover: RecoverCallback | None = None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._db = db
        self._threshold = outage_threshold_s
        self._on_recover = on_recover
        self._now = now_fn or time.time
        # gw_id -> ts of the last quality="ok" sample seen
        self._last_ok: dict[str, float] = {}
        # gw_id -> {"id": outage_id, "start_ts": ts} for currently-open outages
        self._open: dict[str, dict] = {}

    def set_on_recover(self, callback: RecoverCallback | None) -> None:
        """Set the recovery callback after construction (resolves the
        monitor↔engine wiring order in the app lifespan)."""
        self._on_recover = callback

    # ── signal intake ────────────────────────────────────────

    async def on_sample(self, sample: Any) -> None:
        """SampleBus subscriber. An ``ok`` sample refreshes liveness and, if the
        gateway was in an outage, closes it (edge-triggered recovery)."""
        if getattr(sample, "quality", None) != "ok":
            return
        gw_id = sample.gateway_id
        self._last_ok[gw_id] = sample.ts
        open_rec = self._open.pop(gw_id, None)
        if open_rec is not None:
            await self._close(gw_id, open_rec, sample.ts)

    async def tick(self, now: float | None = None) -> None:
        """Time-based staleness check — opens an outage for any known gateway
        whose last good poll is older than the threshold. Call each engine tick."""
        now = now if now is not None else self._now()
        for gw_id, last_ok in list(self._last_ok.items()):
            if gw_id in self._open:
                continue
            if now - last_ok > self._threshold:
                await self._open_outage(gw_id, last_ok)

    # ── outage lifecycle ─────────────────────────────────────

    async def _open_outage(self, gw_id: str, start_ts: float) -> None:
        outage_id = await create_outage(self._db, gw_id, start_ts, reason="stale_reads")
        self._open[gw_id] = {"id": outage_id, "start_ts": start_ts}
        logger.warning(
            "Connectivity: gateway %s outage opened (%s) — no successful poll for >%ds",
            gw_id,
            outage_id,
            self._threshold,
        )

    async def _close(self, gw_id: str, open_rec: dict, end_ts: float) -> None:
        outage_id = open_rec["id"]
        start_ts = open_rec["start_ts"]
        await close_outage(self._db, outage_id, end_ts)
        logger.info(
            "Connectivity: gateway %s recovered (%s) — outage lasted %.0fs",
            gw_id,
            outage_id,
            max(0.0, end_ts - start_ts),
        )
        if self._on_recover is not None:
            try:
                await self._on_recover(gw_id, outage_id, start_ts, end_ts)
            except Exception as exc:  # catch-up must never break recovery
                logger.warning("Connectivity: recovery callback failed: %s", exc)

    # ── read model ───────────────────────────────────────────

    def is_connected(self, gw_id: str) -> bool:
        """True unless the gateway currently has an open outage."""
        return gw_id not in self._open

    def last_ok_ts(self, gw_id: str) -> float | None:
        return self._last_ok.get(gw_id)

    async def snapshot(self, recent_limit: int = 20) -> dict:
        """Outage-aware health for ``/api/health/connectivity``."""
        gateways = {}
        for gw_id, last_ok in self._last_ok.items():
            open_rec = self._open.get(gw_id)
            gateways[gw_id] = {
                "connected": gw_id not in self._open,
                "last_ok_ts": last_ok,
                "current_outage_id": open_rec["id"] if open_rec else None,
                "current_outage_start_ts": open_rec["start_ts"] if open_rec else None,
            }
        recent = await get_recent_outages(self._db, limit=recent_limit)
        return {
            "connected": all(g["connected"] for g in gateways.values()) if gateways else True,
            "gateways": gateways,
            "recent_outages": recent,
        }
