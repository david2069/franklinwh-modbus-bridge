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
    next_fire,
)
from franklinwh_bridge.store.db import (
    create_schedule,
    delete_schedule,
    get_schedule,
    get_schedule_log,
    get_schedules,
    log_schedule_event,
    update_schedule,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["schedules"])

_ACTIONS = frozenset(
    {
        "force_charge",
        "force_discharge",
        "force_standby",
        "release",
        "reserve_self",
        "reserve_tou",
        "mode",
        "none",  # no battery command — HA-actions-only automation
    }
)


# ── request models ────────────────────────────────────────────


class Window(BaseModel):
    start: str = Field(..., pattern=r"^\d{1,2}:\d{2}$")
    end: str = Field(..., pattern=r"^\d{1,2}:\d{2}$")


class WhenSpec(BaseModel):
    days: list[int] = Field(default_factory=list)  # 0..6, Mon=0; empty = daily
    windows: list[Window] = Field(default_factory=list)
    # One-time entry: exact ISO date. When set, `days` is ignored — the entry
    # fires only on this calendar date and never again.
    date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


_TRIGGER_KIND = r"^(oneoff|daily|weekly|interval|monthly|cron|always)$"
_MISSED_POLICY = r"^(late_fire_remaining|skip|late_fire_always)$"


class HaActionItem(BaseModel):
    """One one-shot HA-entity action. ``when`` selects the edge it runs on:
    ``fire`` (activation) or ``exit`` (when the window/activation ends)."""

    instance_id: str = Field(..., min_length=1)
    entity_id: str = Field(..., min_length=1)
    service: str = Field(..., min_length=1)
    data: dict = Field(default_factory=dict)
    when: str = Field(default="fire", pattern=r"^(fire|exit)$")
    # Optional guard leaf {sensor, op, value}: run only if currently true.
    guard: dict | None = None


class ScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    # Optional so a pure fire-based (trigger) entry needn't send a window spec.
    when_spec: WhenSpec = Field(default_factory=WhenSpec)
    action: str
    params: dict = Field(default_factory=dict)
    target_type: str = Field(default="gateway", pattern=r"^(gateway|service|site)$")
    target_id: str | None = None
    enabled: bool = True
    release: str = Field(default="release", pattern=r"^(release|hold)$")
    conflict: str = Field(default="defer", pattern=r"^(defer|override|wait)$")
    priority: int = Field(default=0, ge=0, le=1000)
    # ── v2 fields (trigger + conditions + policies) ──
    trigger_kind: str | None = Field(default=None, pattern=_TRIGGER_KIND)
    trigger_spec: dict = Field(default_factory=dict)
    entry_conditions: dict | None = None
    exit_conditions: dict | None = None
    duration_s: int | None = Field(default=None, ge=0)
    release_policy: str = Field(default="restore_prior_mode")
    missed_policy: str = Field(default="late_fire_remaining", pattern=_MISSED_POLICY)
    entry_hold_s: int = Field(default=0, ge=0, le=86400)
    ha_actions: list[HaActionItem] = Field(default_factory=list)


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
    trigger_kind: str | None = Field(default=None, pattern=_TRIGGER_KIND)
    trigger_spec: dict | None = None
    entry_conditions: dict | None = None
    exit_conditions: dict | None = None
    duration_s: int | None = Field(default=None, ge=0)
    release_policy: str | None = None
    missed_policy: str | None = Field(default=None, pattern=_MISSED_POLICY)
    entry_hold_s: int | None = Field(default=None, ge=0, le=86400)
    ha_actions: list[HaActionItem] | None = None


def _validate_trigger(kind: str | None, spec: dict | None) -> None:
    """Reject a trigger whose spec would never fire, so the user gets feedback
    at save time instead of a silently dead entry. Only checks the fields a kind
    actually needs (the engine is otherwise defensive)."""
    spec = spec or {}
    if kind == "cron":
        expr = str(spec.get("expr") or "").strip()
        if not expr:
            raise HTTPException(400, "cron trigger requires trigger_spec.expr")
        try:
            from croniter import croniter
        except ImportError as exc:  # pragma: no cover - dep is declared
            raise HTTPException(500, "cron support unavailable (croniter missing)") from exc
        if not croniter.is_valid(expr):
            raise HTTPException(400, f"invalid cron expression: {expr!r}")
    elif kind == "monthly":
        day = spec.get("day", 1)
        try:
            day = int(day)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, "monthly trigger day must be 1..31") from exc
        if not 1 <= day <= 31:
            raise HTTPException(400, "monthly trigger day must be 1..31")
        for m in spec.get("months") or []:
            if not (isinstance(m, int) and 1 <= m <= 12):
                raise HTTPException(400, "monthly trigger months must be 1..12")


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


# ── CRUD audit ────────────────────────────────────────────────
# Config changes (create/edit/enable/disable/delete) were previously logged
# nowhere. Record them to BOTH the app log AND the schedule Activity Log so
# there's a "who changed what, when" trail alongside the dispatch/release rows.

#: Fields worth naming in an "updated" audit line (complex ones are named, not
#: value-dumped, to keep the line short).
_SIMPLE_FIELDS = (
    "name", "action", "enabled", "priority", "duration_s", "release", "conflict",
    "release_policy", "missed_policy", "trigger_kind", "entry_hold_s",
    "target_type", "target_id",
)
_NAMED_FIELDS = (
    "params", "when_spec", "trigger_spec", "entry_conditions", "exit_conditions",
    "ha_actions",
)


def _crud_target_label(request: Request, target_type: str | None, target_id: str | None) -> str:
    """Readable target for a CRUD audit row — mirrors the engine's audit target
    (gateway name + "(mock)" flag) so both surfaces read the same."""
    if target_type == "site":
        return "Site (all gateways)"
    if target_type == "service":
        return f"service:{target_id or '?'}"
    gw_id = target_id or "default"
    registry = getattr(request.app.state, "registry", None)
    inst = registry.get(gw_id) if registry is not None else None
    cfg = getattr(inst, "config", None) if inst is not None else None
    name = getattr(cfg, "name", None) or gw_id
    return f"{name} (mock)" if cfg is not None and getattr(cfg, "mock", False) else str(name)


def _changed_fields(updates: dict) -> list[str]:
    """Human summary of the fields a PATCH changed (simple ones with values)."""
    out: list[str] = []
    for k in _SIMPLE_FIELDS:
        if k in updates:
            out.append(f"{k}={updates[k]}")
    for k in _NAMED_FIELDS:
        if k in updates:
            out.append(k)
    return out


async def _audit_crud(
    request: Request, db, *, schedule_id: str, name: str,
    target_type: str | None, target_id: str | None, result: str, detail: str,
) -> None:
    """Log a schedule CRUD change to the app log + the Activity Log (best-effort;
    never let an audit failure break the CRUD response)."""
    logger.info("Schedule %s: '%s' (%s) — %s", result, name, schedule_id, detail)
    try:
        target = _crud_target_label(request, target_type, target_id)
        await log_schedule_event(db, schedule_id, "config", target, result, detail)
    except Exception as exc:
        logger.debug("Schedule CRUD audit failed: %s", exc)


def _decorate(entry: dict, now: datetime) -> dict:
    """Add live previews (active-now, next-fire) to an entry for the UI.

    Trigger (v2) entries are previewed via the fire-based helpers; legacy
    window entries via the when_spec helpers.
    """
    from franklinwh_bridge.gateway.scheduler import entry_active
    from franklinwh_bridge.gateway.scheduler_triggers import next_fire_at

    entry = dict(entry)
    if entry.get("trigger_kind"):
        spec = dict(entry.get("trigger_spec") or {})
        spec["kind"] = entry["trigger_kind"]
        nf = next_fire_at(spec, now)
    else:
        nf = next_fire(entry.get("when_spec", {}), now)
    entry["active_now"] = entry.get("enabled", False) and entry_active(entry, now)
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
    _validate_trigger(body.trigger_kind, body.trigger_spec)
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
        trigger_kind=body.trigger_kind,
        trigger_spec=body.trigger_spec,
        entry_conditions=body.entry_conditions,
        exit_conditions=body.exit_conditions,
        duration_s=body.duration_s,
        release_policy=body.release_policy,
        entry_hold_s=body.entry_hold_s,
        ha_actions=[a.model_dump() for a in body.ha_actions],
        missed_policy=body.missed_policy,
    )
    await _audit_crud(
        request, db, schedule_id=entry["id"], name=entry["name"],
        target_type=entry.get("target_type"), target_id=entry.get("target_id"),
        result="created",
        detail=f"{entry['action']}, enabled={bool(entry.get('enabled'))}",
    )
    await _reload_engine(request)
    return _decorate(entry, datetime.now())


@router.get("/schedules/log")
async def schedule_log(
    request: Request,
    limit: int = 100,
    schedule_id: str | None = None,
    status: str | None = None,
):
    """Recent schedule dispatch/audit events (newest first).

    Optional filters: ``schedule_id`` (one entry's runs) and ``status`` (the
    audit result: fired/gated/missed/executed/exit_condition_met/…) — this backs
    the FWHAI audit + history views.
    """
    db: aiosqlite.Connection = request.app.state.db
    events = await get_schedule_log(
        db, limit=min(max(limit, 1), 500), schedule_id=schedule_id, status=status
    )
    return {"events": events}


@router.post("/schedules/{schedule_id}/execute")
async def execute_schedule(schedule_id: str, request: Request, force: bool = False):
    """Fire a schedule entry's action now (respects entry_conditions unless
    ``?force=true``). The dispatch auto-releases after the entry's duration."""
    engine = getattr(request.app.state, "schedule_engine", None)
    if engine is None:
        raise HTTPException(503, "Schedule engine not available")
    result = await engine.execute(schedule_id, force=force)
    if result.get("status") == "not_found":
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    return result


@router.get("/schedules/timeline")
async def schedule_timeline(request: Request, day: int | None = None):
    """Return the day's windows per entry for the visual timeline.

    ``day`` is an optional weekday (0=Mon..6=Sun) to preview; defaults to today.
    Each segment is minutes-from-midnight so the UI can lay out a 24h bar.
    """
    db: aiosqlite.Connection = request.app.state.db
    from franklinwh_bridge.gateway.scheduler import entry_date, parse_when
    from franklinwh_bridge.gateway.scheduler_triggers import day_segments

    now = datetime.now()
    weekday = now.weekday() if day is None else max(0, min(int(day), 6))
    segments = []

    def _emit(e, start_min, end_min, is_trigger):
        segments.append(
            {
                "schedule_id": e["id"],
                "name": e["name"],
                "action": e["action"],
                "target_type": e["target_type"],
                "target_id": e.get("target_id"),
                "start_min": start_min,
                "end_min": end_min,
                "wraps_midnight": end_min <= start_min,
                "trigger": is_trigger,
            }
        )

    for e in await get_schedules(db):
        if not e.get("enabled"):
            continue
        if e.get("trigger_kind"):
            # Fire-based (v2) entry: derive segments from its trigger + duration.
            for start_min, end_min in day_segments(
                e["trigger_kind"], e.get("trigger_spec") or {}, e.get("duration_s"), weekday
            ):
                _emit(e, start_min, end_min, True)
            continue
        when = e.get("when_spec", {})
        days, windows = parse_when(when)
        d = entry_date(when)
        if d is not None:
            # One-time entry: only show on the weekday its actual date falls
            # on, as a preview — the real gate is the date, not the weekday.
            if weekday != d.weekday():
                continue
        elif days and weekday not in days:
            continue
        for start_min, end_min in windows:
            _emit(e, start_min, end_min, False)
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
    """Update a schedule entry.

    Uses ``exclude_unset`` so we apply exactly the fields the client sent —
    including explicit nulls. A previous ``if v is not None`` filter silently
    dropped nulls, which made it impossible to CLEAR a field: deleting all
    exit_conditions (client sends ``exit_conditions: null``) never persisted.
    ``update_schedule`` stores None as SQL NULL for the nullable JSON columns.
    """
    db: aiosqlite.Connection = request.app.state.db
    updates = body.model_dump(exclude_unset=True)
    if "action" in updates and updates["action"] is not None:
        _validate_action(updates["action"], updates.get("params") or body.params or {})
    if "trigger_kind" in updates or "trigger_spec" in updates:
        existing = await get_schedule(db, schedule_id)
        kind = updates.get("trigger_kind", (existing or {}).get("trigger_kind"))
        spec = updates.get("trigger_spec", (existing or {}).get("trigger_spec"))
        _validate_trigger(kind, spec)
    result = await update_schedule(db, schedule_id, **updates)
    if result is None:
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    # A PATCH that only flips `enabled` is the common enable/disable action;
    # anything broader is a full "updated" with the changed fields listed.
    if list(updates) == ["enabled"]:
        verb = "enabled" if updates["enabled"] else "disabled"
        detail = verb
    else:
        verb = "updated"
        fields = _changed_fields(updates)
        detail = "changed: " + ", ".join(fields) if fields else "no changes"
    await _audit_crud(
        request, db, schedule_id=schedule_id, name=result["name"],
        target_type=result.get("target_type"), target_id=result.get("target_id"),
        result=verb, detail=detail,
    )
    await _reload_engine(request)
    return _decorate(result, datetime.now())


@router.delete("/schedules/{schedule_id}")
async def remove_schedule(schedule_id: str, request: Request):
    """Delete a schedule entry."""
    db: aiosqlite.Connection = request.app.state.db
    existing = await get_schedule(db, schedule_id)  # capture name/target before delete
    if not await delete_schedule(db, schedule_id):
        raise HTTPException(404, f"Schedule '{schedule_id}' not found")
    if existing is not None:
        await _audit_crud(
            request, db, schedule_id=schedule_id, name=existing.get("name", schedule_id),
            target_type=existing.get("target_type"), target_id=existing.get("target_id"),
            result="deleted", detail=f"deleted '{existing.get('name', schedule_id)}'",
        )
    await _reload_engine(request)
    return {"deleted": True}


@router.get("/schedule/actions")
async def list_schedule_actions():
    """The command vocabulary a schedule entry can dispatch (for the UI form)."""
    return {
        "actions": [
            {
                "id": "force_charge",
                "label": "Force Charge",
                "sustained": True,
                "params": ["power_w", "power_pct", "duration_s", "target_soc"],
            },
            {
                "id": "force_discharge",
                "label": "Force Discharge",
                "sustained": True,
                "params": ["power_w", "power_pct", "duration_s", "target_soc"],
            },
            {
                "id": "force_standby",
                "label": "Force Standby",
                "sustained": True,
                "params": ["duration_s"],
            },
            {"id": "release", "label": "Release", "sustained": True, "params": []},
            {
                "id": "reserve_self",
                "label": "Self-Consumption Reserve %",
                "sustained": False,
                "params": ["pct"],
            },
            {"id": "reserve_tou", "label": "TOU Reserve %", "sustained": False, "params": ["pct"]},
            {"id": "mode", "label": "Operating Mode", "sustained": False, "params": ["mode"]},
            {
                "id": "none",
                "label": "No battery action (HA only)",
                "sustained": False,
                "params": [],
            },
        ],
        # a quick echo so the UI can preview what a given action expands to
        "example_expansion": action_to_commands("force_charge", {"power_w": 1000}),
    }
