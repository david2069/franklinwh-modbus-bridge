"""Health and status endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["health"])

_start_time: float = time.time()
_components: dict[str, Any] = {}


def register_component(name: str, state_fn) -> None:
    """Register a component whose state is surfaced by /api/status."""
    _components[name] = state_fn


@router.get("/api/health")
async def health():
    return {"status": "ok", "uptime_s": round(time.time() - _start_time, 1)}


@router.get("/api/status")
async def status():
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
        "uptime_s": round(time.time() - _start_time, 1),
        "components": states,
    }
