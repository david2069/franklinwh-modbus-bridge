"""Sample dataclass and async Sample Bus."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Literal


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
    """Async pub/sub bus for Sample records."""

    def __init__(self) -> None:
        self._subscribers: list[SampleCallback] = []
        self._last_sample: Sample | None = None

    def subscribe(self, callback: SampleCallback) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback: SampleCallback) -> None:
        self._subscribers.remove(callback)

    @property
    def last_sample(self) -> Sample | None:
        return self._last_sample

    async def publish(self, sample: Sample) -> None:
        self._last_sample = sample
        tasks = [asyncio.create_task(cb(sample)) for cb in self._subscribers]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
