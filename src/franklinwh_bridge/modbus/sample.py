"""Sample dataclass and async Sample Bus."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

#: Default TTL for sticky (explorer-read) points: 5 minutes.
STICKY_TTL_SECONDS = 300.0


@dataclass
class Sample:
    gateway_id: str
    ts: float
    points: dict[str, Any]
    quality: Literal["ok", "stale", "error"]

    @staticmethod
    def now(gateway_id: str, points: dict[str, Any], quality: str = "ok") -> Sample:
        return Sample(gateway_id=gateway_id, ts=time.time(), points=points, quality=quality)  # type: ignore[arg-type]


SampleCallback = Callable[[Sample], Coroutine[Any, Any, None]]


class SampleBus:
    """Async pub/sub bus for Sample records.

    Supports *sticky points* — values injected by the SunSpec Explorer's
    on-demand model reads.  Sticky points are merged under every new
    sample (poll data wins on key conflict) and auto-expire after
    ``STICKY_TTL_SECONDS``.
    """

    def __init__(self) -> None:
        self._subscribers: list[SampleCallback] = []
        self._last_sample: Sample | None = None
        # {key: (value, injected_timestamp)}
        self._sticky_points: dict[str, tuple[Any, float]] = {}

    def subscribe(self, callback: SampleCallback) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: SampleCallback) -> None:
        self._subscribers.remove(callback)

    @property
    def last_sample(self) -> Sample | None:
        return self._last_sample

    # ── Sticky overlay ───────────────────────────────────────

    def inject_sticky(self, points: dict[str, Any]) -> None:
        """Add explorer-read points that survive poll cycles.

        Points auto-expire after ``STICKY_TTL_SECONDS`` (default 5 min).
        If a polled sample already contains the same key, the polled value
        wins (sticky is a low-priority underlay).
        """
        now = time.time()
        for k, v in points.items():
            self._sticky_points[k] = (v, now)
        logger.debug("Injected %d sticky points (%d total)", len(points), len(self._sticky_points))

    def clear_sticky(self, prefix: str | None = None) -> int:
        """Remove sticky points.  If *prefix* is given, only keys starting
        with that prefix are removed.  Returns count removed."""
        if prefix is None:
            n = len(self._sticky_points)
            self._sticky_points.clear()
            return n
        to_remove = [k for k in self._sticky_points if k.startswith(prefix)]
        for k in to_remove:
            del self._sticky_points[k]
        return len(to_remove)

    @property
    def sticky_count(self) -> int:
        """Number of currently-held sticky points (before expiry check)."""
        return len(self._sticky_points)

    def _expire_sticky(self) -> None:
        """Purge sticky points older than TTL."""
        if not self._sticky_points:
            return
        cutoff = time.time() - STICKY_TTL_SECONDS
        expired = [k for k, (_, ts) in self._sticky_points.items() if ts < cutoff]
        for k in expired:
            del self._sticky_points[k]
        if expired:
            logger.debug("Expired %d sticky points", len(expired))

    # ── Publish ──────────────────────────────────────────────

    async def publish(self, sample: Sample) -> None:
        """Store sample, merge sticky underlay, and notify subscribers."""
        self._expire_sticky()

        # Merge: sticky points go under (poll data wins on conflict)
        if self._sticky_points:
            sticky_vals = {k: v for k, (v, _ts) in self._sticky_points.items()}
            # Start with sticky, then overlay poll data on top
            merged = {**sticky_vals, **sample.points}
            sample = Sample(
                gateway_id=sample.gateway_id,
                ts=sample.ts,
                points=merged,
                quality=sample.quality,
            )

        self._last_sample = sample
        tasks = [asyncio.create_task(cb(sample)) for cb in self._subscribers]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
