"""Multiple Home Assistant instances → one Bridge (inbound entity access).

An `HaRegistry` holds N `HaInstance` clients (one flagged default). Each instance
polls its HA's REST `GET /api/states` on an interval and caches entity states;
the states are exposed as `ha:<instance_id>:<entity_id>` automation condition
sensors (merged into the sensor snapshot the engine + /api/sensors evaluate).

This is the inbound direction (read HA entity states), distinct from the existing
outbound MQTT discovery. REST-poll to start (simple, fully testable); a WebSocket
`state_changed` subscription is the planned live-update enhancement
(docs/multi-ha-entity-access-design.md).

State coercion: HA states are strings. `unavailable`/`unknown`/empty → None (so
conditions fail closed on a stale entity); `on`/`off` (+ true/false) → bool;
numeric → float; otherwise the raw string (enum) — mirroring the sensor
evaluator's own coercion.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from franklinwh_bridge.store.db import get_ha_instances

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 30
_UNAVAILABLE = frozenset({"unavailable", "unknown", "none", ""})
_TRUE = frozenset({"on", "true", "home", "open", "locked"})
_FALSE = frozenset({"off", "false", "away", "closed", "unlocked"})


def coerce_state(state: Any) -> float | bool | str | None:
    """HA string state → number / bool / str, or None if unavailable."""
    if state is None:
        return None
    s = str(state).strip()
    low = s.lower()
    if low in _UNAVAILABLE:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    return s


class HaInstance:
    """One HA connection: polls /api/states into an entity-state cache."""

    def __init__(self, cfg: dict, *, timeout: float = 10.0) -> None:
        self.id: str = cfg["id"]
        self.name: str = cfg["name"]
        self.base_url: str = str(cfg["base_url"]).rstrip("/")
        self.token: str | None = cfg.get("token")
        self._timeout = timeout
        self.connected = False
        self.last_error: str | None = None
        # entity_id -> {"state": str, "attributes": {...}}
        self._states: dict[str, dict] = {}

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def refresh(self) -> None:
        """Pull /api/states into the cache. Sets connected/last_error."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(f"{self.base_url}/api/states", headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
            self._states = {
                e["entity_id"]: {"state": e.get("state"), "attributes": e.get("attributes", {})}
                for e in data
                if isinstance(e, dict) and "entity_id" in e
            }
            self.connected = True
            self.last_error = None
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc) or type(exc).__name__
            logger.debug("HA %s refresh failed: %s", self.name, self.last_error)

    def values(self) -> dict[str, Any]:
        """Cached entity states as `ha:<id>:<entity_id>` → coerced value."""
        return {
            f"ha:{self.id}:{eid}": coerce_state(s.get("state")) for eid, s in self._states.items()
        }

    def catalog(self) -> list[dict]:
        """Sensor metadata for each cached entity (for /api/sensors dropdowns)."""
        out = []
        for eid, s in self._states.items():
            attrs = s.get("attributes") or {}
            val = coerce_state(s.get("state"))
            kind = (
                "number"
                if isinstance(val, (int, float)) and not isinstance(val, bool)
                else ("bool" if isinstance(val, bool) else "enum")
            )
            fn = attrs.get("friendly_name") or eid
            out.append(
                {
                    "id": f"ha:{self.id}:{eid}",
                    "label": f"{self.name}: {fn}",
                    "unit": attrs.get("unit_of_measurement"),
                    "kind": kind,
                    "value": val,
                    "source": "ha",
                    "instance": self.id,
                }
            )
        return out


class HaRegistry:
    """Holds the configured HA instances; polls them; exposes merged entity values."""

    def __init__(self, db: Any, *, poll_interval_s: int = DEFAULT_POLL_INTERVAL_S) -> None:
        self._db = db
        self._interval = poll_interval_s
        self._instances: dict[str, HaInstance] = {}
        self._task: asyncio.Task | None = None

    async def load(self) -> None:
        """(Re)build the instance set from the ha_instances table (enabled only)."""
        rows = await get_ha_instances(self._db)
        self._instances = {r["id"]: HaInstance(r) for r in rows if r.get("enabled")}

    async def reload(self) -> None:
        """Reload config + refresh immediately (call after a CRUD change)."""
        await self.load()
        await self._refresh_all()

    async def start(self) -> None:
        await self.load()
        await self._refresh_all()
        self._task = asyncio.create_task(self._loop())
        logger.info("HA registry started (%d instance(s))", len(self._instances))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval)
                await self._refresh_all()
        except asyncio.CancelledError:
            pass

    async def _refresh_all(self) -> None:
        if not self._instances:
            return
        await asyncio.gather(*(inst.refresh() for inst in self._instances.values()))

    def entity_values(self) -> dict[str, Any]:
        """Merged `ha:<inst>:<entity>` → value across all instances."""
        merged: dict[str, Any] = {}
        for inst in self._instances.values():
            merged.update(inst.values())
        return merged

    def catalog(self) -> list[dict]:
        out: list[dict] = []
        for inst in self._instances.values():
            out.extend(inst.catalog())
        return out

    def status(self) -> list[dict]:
        return [
            {
                "id": i.id,
                "name": i.name,
                "base_url": i.base_url,
                "connected": i.connected,
                "last_error": i.last_error,
                "entity_count": len(i._states),
            }
            for i in self._instances.values()
        ]
