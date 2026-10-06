"""Health and status endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request

from franklinwh_bridge import __version__

router = APIRouter(tags=["health"])

_start_time: float = time.time()
_components: dict[str, Any] = {}


def register_component(name: str, state_fn) -> None:
    """Register a component whose state is surfaced by /api/status."""
    _components[name] = state_fn


@router.get("/api/health")
async def health(request: Request):
    config = getattr(request.app.state, "config", None)
    env = config.environment if config else "unknown"
    return {
        "status": "ok",
        "version": __version__,
        "environment": env,
        "uptime_s": round(time.time() - _start_time, 1),
        # {source: supervisor|configured|none, waiting_for_broker} — drives the
        # "install Mosquitto" prompt on an add-on with no broker yet.
        "mqtt_broker": getattr(request.app.state, "mqtt_broker", None),
    }


@router.get("/api/health/connectivity")
async def connectivity(request: Request):
    """Outage-aware connectivity: per-gateway connected/last-good-poll state plus
    recent outages. Powers the scheduler's outage view (WiFi→4G drops etc.)."""
    monitor = getattr(request.app.state, "connectivity", None)
    if monitor is None:
        return {"connected": True, "gateways": {}, "recent_outages": []}
    return await monitor.snapshot()


@router.get("/api/status")
async def status(request: Request):
    config = getattr(request.app.state, "config", None)
    env = config.environment if config else "unknown"

    states = {}
    for name, state_fn in _components.items():
        try:
            result = state_fn()
            if hasattr(result, "__await__"):
                result = await result
            states[name] = result
        except Exception as exc:
            states[name] = {"error": str(exc)}

    return {
        "status": "ok",
        "version": __version__,
        "environment": env,
        "uptime_s": round(time.time() - _start_time, 1),
        "components": states,
    }
