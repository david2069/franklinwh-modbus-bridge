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
from pydantic import BaseModel, Field, model_validator

from franklinwh_bridge.gateway.scheduler import (
    action_to_commands,
    next_fire,
)
from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog
from franklinwh_bridge.store.db import (
    create_schedule,
    delete_schedule,
    get_gateways,
    get_ha_instances,
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
    """One one-shot HA action. ``when`` selects the edge it runs on: ``fire``
    (activation) or ``exit`` (when the window/activation ends).

    Two kinds:
    - ``entity`` (default) — a service call on an entity (turn_on, select_option…).
    - ``notify`` — a one-way message via an HA ``notify.*`` service. It has no
      entity: ``service`` is the notify target name and title/message carry the
      content, so entity_id is not required for it.
    """

    kind: str = Field(default="entity", pattern=r"^(entity|notify)$")
    instance_id: str = Field(..., min_length=1)
    entity_id: str = ""          # required for kind=entity; unused for notify
    service: str = Field(..., min_length=1)
    data: dict = Field(default_factory=dict)
    # "both" = run on activation AND on window end (added 2026-09-15).
    when: str = Field(default="fire", pattern=r"^(fire|exit|both)$")
    # Optional guard leaf {sensor, op, value}: run only if currently true.
    guard: dict | None = None
    # notify only. {sensor.id} placeholders are substituted from the live
    # snapshot when the notification fires.
    title: str = Field(default="", max_length=200)
    message: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _entity_needs_an_entity(self) -> HaActionItem:
        """An entity action without an entity_id would be silently skipped by the
        engine — reject it here where the user can still see why."""
        if self.kind == "entity" and not self.entity_id:
            raise ValueError("entity_id is required for an entity action")
        if self.kind == "notify" and not self.message:
            raise ValueError("message is required for a notification")
        return self


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
    release_policy: str = Field(default="release")
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


# ── Export / Import (share schedules & templates as JSON) ─────

_EXPORT_TYPE = "franklinwh-automations"
_EXPORT_VERSION = 1

#: Portable fields carried in an export (identity/audit fields are stripped).
_PORTABLE_FIELDS = (
    "name", "action", "params", "target_type", "target_id", "enabled",
    "release", "conflict", "priority", "trigger_kind", "trigger_spec",
    "when_spec", "entry_conditions", "exit_conditions", "duration_s",
    "release_policy", "missed_policy", "entry_hold_s", "ha_actions",
)


def _portable(entry: dict) -> dict:
    """Strip an entry down to shareable fields (no id/created_at/live previews)."""
    return {k: entry[k] for k in _PORTABLE_FIELDS if k in entry}


def _collect_sensor_refs(tree: object) -> set[str]:
    """Every sensor id referenced by a condition tree (LHS + Lookup RHS)."""
    out: set[str] = set()

    def walk(node: dict) -> None:
        for c in node.get("conditions", []) or []:
            if isinstance(c, dict) and "conditions" in c:
                walk(c)
            elif isinstance(c, dict):
                if c.get("sensor"):
                    out.add(c["sensor"])
                if c.get("value_kind") == "sensor" and c.get("value_sensor"):
                    out.add(c["value_sensor"])

    if isinstance(tree, dict):
        walk(tree)
    return out


async def _validate_import_entry(request: Request, entry: dict) -> dict:
    """Validate one imported entry: hard errors (would fail to create) +
    soft warnings (missing local customisations — HA instances, gateway,
    unknown sensors) the user must fix before it does anything useful."""
    db = request.app.state.db
    name = entry.get("name") or "(unnamed)"
    warnings: list[str] = []
    errors: list[str] = []

    action = entry.get("action")
    try:
        _validate_action(action or "", entry.get("params") or {})
    except HTTPException as exc:
        errors.append(str(exc.detail))
    try:
        _validate_trigger(entry.get("trigger_kind"), entry.get("trigger_spec"))
    except HTTPException as exc:
        errors.append(str(exc.detail))

    # gateway target must exist locally (else it falls back to Default on import)
    if entry.get("target_type", "gateway") == "gateway" and entry.get("target_id"):
        gw_ids = {g["id"] for g in await get_gateways(db)}
        if entry["target_id"] not in gw_ids:
            warnings.append(
                f"gateway '{entry['target_id']}' not found — will target the primary gateway"
            )

    ha_ids = {h["id"] for h in await get_ha_instances(db)}
    for a in entry.get("ha_actions") or []:
        inst = a.get("instance_id")
        if inst and inst not in ha_ids:
            warnings.append(
                f"HA instance '{inst}' missing — action on '{a.get('entity_id')}' won't run"
            )

    # condition sensors: ha:* need the instance present; others must be in the catalog
    catalog = {s["id"] for s in sensor_catalog()}
    refs = _collect_sensor_refs(entry.get("entry_conditions")) | _collect_sensor_refs(
        entry.get("exit_conditions")
    )
    for sid in sorted(refs):
        if sid.startswith("ha:"):
            parts = sid.split(":")
            inst = parts[1] if len(parts) > 2 else ""
            if inst not in ha_ids:
                warnings.append(f"HA sensor '{sid}' — instance '{inst}' missing")
        elif sid not in catalog:
            warnings.append(f"unknown sensor '{sid}' — condition will fail closed")

    return {"name": name, "action": action, "ok": not errors,
            "warnings": warnings, "errors": errors}


@router.get("/schedules/export")
async def export_schedules(request: Request, ids: str | None = None):
    """Export schedules as a portable JSON bundle. ``ids`` = comma-separated
    entry ids (default: all)."""
    db: aiosqlite.Connection = request.app.state.db
    rows = await get_schedules(db)
    if ids:
        wanted = {i.strip() for i in ids.split(",") if i.strip()}
        rows = [r for r in rows if r["id"] in wanted]
    return {
        "type": _EXPORT_TYPE,
        "version": _EXPORT_VERSION,
        "entries": [_portable(r) for r in rows],
    }


class ImportBundle(BaseModel):
    type: str | None = None
    version: int | None = None
    entries: list[dict] = Field(default_factory=list)


@router.post("/schedules/import")
async def import_schedules(bundle: ImportBundle, request: Request, dry_run: bool = True):
    """Validate (and optionally create) an imported bundle. ``dry_run=true``
    (default) returns a per-entry validation report without creating anything;
    ``dry_run=false`` creates the valid entries DISABLED (for review), with any
    unknown gateway target falling back to Default."""
    if bundle.type and bundle.type != _EXPORT_TYPE:
        raise HTTPException(400, f"unexpected bundle type '{bundle.type}'")
    reports = [await _validate_import_entry(request, e) for e in bundle.entries]
    if dry_run:
        return {"entries": reports, "count": len(reports),
                "importable": sum(1 for r in reports if r["ok"])}

    db: aiosqlite.Connection = request.app.state.db
    gw_ids = {g["id"] for g in await get_gateways(db)}
    created: list[str] = []
    skipped: list[dict] = []
    for e, rep in zip(bundle.entries, reports, strict=False):
        if not rep["ok"]:
            skipped.append({"name": rep["name"], "errors": rep["errors"]})
            continue
        tt = e.get("target_type", "gateway")
        tid = e.get("target_id")
        if tt == "gateway" and tid and tid not in gw_ids:
            tid = "default"
        entry = await create_schedule(
            db, name=e.get("name", "imported"), when_spec=e.get("when_spec") or {},
            action=e["action"], params=e.get("params") or {},
            target_type=tt, target_id=tid or None,
            enabled=False,  # imported entries start disabled for review
            release=e.get("release", "release"), conflict=e.get("conflict", "defer"),
            priority=int(e.get("priority") or 0),
            trigger_kind=e.get("trigger_kind"), trigger_spec=e.get("trigger_spec") or {},
            entry_conditions=e.get("entry_conditions"), exit_conditions=e.get("exit_conditions"),
            duration_s=e.get("duration_s"),
            release_policy=e.get("release_policy", "release"),
            entry_hold_s=int(e.get("entry_hold_s") or 0),
            ha_actions=e.get("ha_actions") or [],
            missed_policy=e.get("missed_policy", "late_fire_remaining"),
        )
        await _audit_crud(
            request, db, schedule_id=entry["id"], name=entry["name"],
            target_type=entry.get("target_type"), target_id=entry.get("target_id"),
            result="created", detail=f"imported — {entry['action']}",
        )
        created.append(entry["id"])
    await _reload_engine(request)
    return {"created": created, "skipped": skipped}


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


@router.post("/schedules/{schedule_id}/stop")
async def stop_schedule(schedule_id: str, request: Request):
    """Gracefully stop a schedule's CURRENT run: release the live dispatch it
    owns and skip re-firing until the next scheduled window. The entry stays
    enabled (disable it to stop permanently)."""
    engine = getattr(request.app.state, "schedule_engine", None)
    if engine is None:
        raise HTTPException(503, "Schedule engine not available")
    result = await engine.stop_entry(schedule_id)
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
