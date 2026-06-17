"""Schedule (SCH1) REST endpoints — CRUD + timeline/next-fire previews.

The REST API is the canonical interface (CLAUDE.md): the Schedule tab UI and any
future consumer drive scheduling through these routes. Mutations reload the live
``ScheduleEngine`` so a change takes effect within a tick.
"""

from __future__ import annotations

import logging
from datetime import datetime

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.gateway.scheduler import (
    action_to_commands,
    entry_active_at,
    next_fire,
)
from franklinwh_bridge.store.db import (
    create_schedule,
    delete_schedule,
    get_schedule,
    get_schedule_log,
    get_schedules,
    update_schedule,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["schedules"])

_ACTIONS = frozenset({
    "force_charge", "force_discharge", "force_standby", "release",
    "reserve_self", "reserve_tou", "mode",
})


# ── request models ────────────────────────────────────────────


class Window(BaseModel):
    start: str = Field(..., pattern=r"^\d{1,2}:\d{2}$")
    end: str = Field(..., pattern=r"^\d{1,2}:\d{2}$")


class WhenSpec(BaseModel):
    days: list[int] = Field(default_factory=list)  # 0..6, Mon=0; empty = daily
    windows: list[Window] = Field(default_factory=list)


class ScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    when_spec: WhenSpec
    action: str
    params: dict = Field(default_factory=dict)
    target_type: str = Field(default="gateway", pattern=r"^(gateway|service|site)$")
    target_id: str | None = None
    enabled: bool = True
    release: str = Field(default="release", pattern=r"^(release|hold)$")
    conflict: str = Field(default="defer", pattern=r"^(defer|override|wait)$")
    priority: int = Field(default=0, ge=0, le=1000)


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    when_spec: WhenSpec | None = None
    action: str | None = None
    params: dict | None = None
    target_type: str | None = Field(default=None, pattern=r"^(gateway|service|site)$")
    target_id: str | None = None
    enabled: bool | None = None
    release: str | None = Field(default=None, pattern=r"^(release|hold)$")
    conflict: str | None = Field(default=None, pattern=r"^(defer|override|wait)$")
    priority: int | None = Field(default=None, ge=0, le=1000)


def _validate_action(action: str, params: dict) -> None:
    if action not in _ACTIONS:
        raise HTTPException(400, f"Unknown action '{action}'. One of {sorted(_ACTIONS)}")
    # mode requires a mode name; reserves require a pct — catch obvious mistakes
    if action == "mode" and not params.get("mode"):
        raise HTTPException(400, "action 'mode' requires params.mode")
    if action in ("reserve_self", "reserve_tou") and "pct" not in params:
        raise HTTPException(400, f"action '{action}' requires params.pct")


async def _reload_engine(request: Request) -> None:
    engine = getattr(request.app.state, "schedule_engine", None)
    if engine is not None:
        try:
            await engine.reload()
        except Exception as exc:
            logger.warning("Schedule engine reload failed: %s", exc)


def _decorate(entry: dict, now: datetime) -> dict:
    """Add live previews (active-now, next-fire) to an entry for the UI."""
    when = entry.get("when_spec", {})
    nf = next_fire(when, now)
    entry = dict(entry)
    entry["active_now"] = entry.get("enabled", False) and entry_active_at(when, now)
    entry["next_fire"] = nf.isoformat() if nf else None
    return entry


# ── CRUD ──────────────────────────────────────────────────────


@router.get("/schedules")
async def list_schedules(request: Request):
    """List all schedule entries with active-now / next-fire previews."""
    db: aiosqlite.Connection = request.app.state.db
    now = datetime.now()
    rows = [_decorate(e, now) for e in await get_schedules(db)]
    return {"schedules": rows}


@router.post("/schedules", status_code=201)
async def add_schedule(body: ScheduleCreate, request: Request):
    """Create a schedule entry."""
    db: aiosqlite.Connection = request.app.state.db
    _validate_action(body.action, body.params)
    entry = await create_schedule(
        db,
        name=body.name,
        when_spec=body.when_spec.model_dump(),
        action=body.action,
        params=body.params,
        target_type=body.target_type,
        target_id=body.target_id or None,
        enabled=body.enabled,
        release=body.release,
        conflict=body.conflict,
        priority=body.priority,
    )
    await _reload_engine(request)
    return _decorate(entry, datetime.now())


@router.get("/schedules/log")
async def schedule_log(request: Request, limit: int = 100):
    """Recent schedule dispatch/audit events (newest first)."""
    db: aiosqlite.Connection = request.app.state.db
    return {"events": await get_schedule_log(db, limit=min(max(limit, 1), 500))}


@router.get("/schedules/timeline")
async def schedule_timeline(request: Request, day: int | None = None):
    """Return the day's windows per entry for the visual timeline.

    ``day`` is an optional weekday (0=Mon..6=Sun) to preview; defaults to today.
    Each segment is minutes-from-midnight so the UI can lay out a 24h bar.
    """
    db: aiosqlite.Connection = request.app.state.db
    from franklinwh_bridge.gateway.scheduler import parse_when

    now = datetime.now()
    weekday = now.weekday() if day is None else max(0, min(int(day), 6))
    segments = []
    for e in await get_schedules(db):
        if not e.get("enabled"):
            continue
        days, windows = parse_when(e.get("when_spec", {}))
        if days and weekday not in days:
            continue
        for start_min, end_min in windows:
            segments.append({
                "schedule_id": e["id"],
                "name": e["name"],
                "action": e["action"],
                "target_type": e["target_type"],
                "target_id": e.get("target_id"),
                "start_min": start_min,
                "end_min": end_min,
                "wraps_midnight": end_min <= start_min,
            })
    return {"weekday": weekday, "now_min": now.hour * 60 + now.minute, "segments": segments}


@router.get("/schedules/{schedule_id}")
async def get_single_schedule(schedule_id: str, request: Request):
    """Get one schedule entry."""
    db: aiosqlite.Connection = request.app.state.db
    entry = await get_schedule(db, schedule_id)
    if entry is None:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    return _decorate(entry, datetime.now())


@router.patch("/schedules/{schedule_id}")
async def patch_schedule(schedule_id: str, body: ScheduleUpdate, request: Request):
    """Update a schedule entry."""
    db: aiosqlite.Connection = request.app.state.db
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "when_spec" in updates and body.when_spec is not None:
        updates["when_spec"] = body.when_spec.model_dump()
    if "action" in updates:
        _validate_action(updates["action"], updates.get("params", body.params or {}))
    result = await update_schedule(db, schedule_id, **updates)
    if result is None:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    await _reload_engine(request)
    return _decorate(result, datetime.now())


@router.delete("/schedules/{schedule_id}")
async def remove_schedule(schedule_id: str, request: Request):
    """Delete a schedule entry."""
    db: aiosqlite.Connection = request.app.state.db
    if not await delete_schedule(db, schedule_id):
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    await _reload_engine(request)
    return {"deleted": True}


@router.get("/schedule/actions")
async def list_schedule_actions():
    """The command vocabulary a schedule entry can dispatch (for the UI form)."""
    return {
        "actions": [
            {"id": "force_charge", "label": "Force Charge", "sustained": True,
             "params": ["power_w", "power_pct", "duration_s", "target_soc"]},
            {"id": "force_discharge", "label": "Force Discharge", "sustained": True,
             "params": ["power_w", "power_pct", "duration_s", "target_soc"]},
            {"id": "force_standby", "label": "Force Standby", "sustained": True,
             "params": ["duration_s"]},
            {"id": "release", "label": "Release", "sustained": True, "params": []},
            {"id": "reserve_self", "label": "Self-Consumption Reserve %",
             "sustained": False, "params": ["pct"]},
            {"id": "reserve_tou", "label": "TOU Reserve %", "sustained": False,
             "params": ["pct"]},
            {"id": "mode", "label": "Operating Mode", "sustained": False,
             "params": ["mode"]},
        ],
        # a quick echo so the UI can preview what a given action expands to
        "example_expansion": action_to_commands("force_charge", {"power_w": 1000}),
    }
