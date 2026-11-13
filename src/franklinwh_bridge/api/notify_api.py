"""Phone notifications (#296 item 48): devices, the Push notifications switch, the
Notifications check, Auto-notify triggers and Recent notifications. The work is in
``actionable.py`` and ``notify_triggers.py``.

Reads need a signed-in user; changes and sends need admin.
"""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.actionable import Actionable
from franklinwh_bridge.api.auth import require_auth, require_role
from franklinwh_bridge.notify_triggers import Triggers
from franklinwh_bridge.store.db import (
    create_notify_device,
    delete_notify_device,
    get_ha_instances,
    get_notify_device,
    get_notify_devices,
    update_notify_device,
)

router = APIRouter(prefix="/api/notify", tags=["notifications"])
_ADMIN = require_role("admin")


_KIND = r"^(phone|tablet|tv|speaker|other)$"


class DeviceCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=80)
    instance_id: str = Field(min_length=1)
    service: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    kind: str = Field(default="other", pattern=_KIND)


class DeviceUpdate(BaseModel):
    alias: str | None = Field(default=None, min_length=1, max_length=80)
    instance_id: str | None = Field(default=None, min_length=1)
    service: str | None = Field(default=None, min_length=1, max_length=200)
    enabled: bool | None = None
    kind: str | None = Field(default=None, pattern=_KIND)


class TargetTest(BaseModel):
    instance_id: str = Field(min_length=1)
    service: str = Field(min_length=1, max_length=200)


class SettingsUpdate(BaseModel):
    push: bool


class TriggersUpdate(BaseModel):
    master: bool | None = None
    triggers: dict[str, dict] | None = None


class TriggerTest(BaseModel):
    device_ids: list[str] | None = None


class BroadcastBody(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    title: str | None = Field(default=None, max_length=120)


def _db(request: Request) -> aiosqlite.Connection:
    return request.app.state.db


def _act(request: Request) -> Actionable:
    act = getattr(request.app.state, "actionable", None)
    if act is None:
        raise HTTPException(503, "Notifications are not running")
    return act


def _triggers(request: Request) -> Triggers:
    trg = getattr(request.app.state, "notify_triggers", None)
    if trg is None:
        raise HTTPException(503, "Notifications are not running")
    return trg


def _service(name: str) -> str:
    return name.strip().removeprefix("notify.")


async def _device_rows(request: Request) -> list[dict]:
    instances = {r["id"]: r for r in await get_ha_instances(_db(request))}
    out = []
    for d in await get_notify_devices(_db(request)):
        inst = instances.get(d["instance_id"])
        out.append({**d, "instance_name": inst["name"] if inst else None,
                    "instance_enabled": bool(inst and inst["enabled"]),
                    "buttons": d["service"].startswith("mobile_app_")})
    return out


async def _device(request: Request, device_id: str) -> dict:
    row = await get_notify_device(_db(request), device_id)
    if row is None:
        raise HTTPException(404, "Device not found")
    return row


def _sent(res) -> dict:
    return {"delivered": res.delivered, "total": res.total, "summary": res.summary,
            "errors": [d.error for d in res.deliveries if not d.ok]}


# ── devices ──────────────────────────────────────────────────


@router.get("/devices", dependencies=[Depends(require_auth)])
async def list_devices(request: Request):
    """The devices, and the HA instances a device can be on (for the Add form, which
    must work with the HA Entities module off)."""
    instances = [{"id": r["id"], "name": r["name"], "enabled": r["enabled"]}
                 for r in await get_ha_instances(_db(request))]
    return {"devices": await _device_rows(request), "instances": instances}


@router.post("/devices", status_code=201, dependencies=[Depends(_ADMIN)])
async def add_device(body: DeviceCreate, request: Request):
    if not any(r["id"] == body.instance_id for r in await get_ha_instances(_db(request))):
        raise HTTPException(400, "HA instance not found")
    try:
        row = await create_notify_device(_db(request), body.alias.strip(), body.instance_id,
                                         _service(body.service), body.enabled, body.kind)
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(409, "That service on that HA instance is already a device") from exc
    return row


@router.patch("/devices/{device_id}", dependencies=[Depends(_ADMIN)])
async def edit_device(device_id: str, body: DeviceUpdate, request: Request):
    await _device(request, device_id)
    updates = body.model_dump(exclude_unset=True)
    if "instance_id" in updates and not any(
            r["id"] == updates["instance_id"] for r in await get_ha_instances(_db(request))):
        raise HTTPException(400, "HA instance not found")
    if "service" in updates:
        updates["service"] = _service(updates["service"])
    if "alias" in updates:
        updates["alias"] = updates["alias"].strip()
    try:
        return await update_notify_device(_db(request), device_id, **updates)
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(409, "That service on that HA instance is already a device") from exc


@router.delete("/devices/{device_id}", dependencies=[Depends(_ADMIN)])
async def remove_device(device_id: str, request: Request):
    await _device(request, device_id)
    await delete_notify_device(_db(request), device_id)
    return {"deleted": device_id}


@router.get("/discover", dependencies=[Depends(_ADMIN)])
async def discover_services(instance_id: str, request: Request):
    """Every ``notify.*`` service on one HA instance, phones first."""
    try:
        return {"services": await _act(request).discover(instance_id)}
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Home Assistant did not answer: {exc}") from exc


@router.post("/devices/{device_id}/test", dependencies=[Depends(_ADMIN)])
async def test_device(device_id: str, request: Request):
    """One plain message to this device, even with Push notifications off."""
    return _sent(await _act(request).test(await _device(request, device_id)))


@router.post("/test", dependencies=[Depends(_ADMIN)])
async def test_target(body: TargetTest, request: Request):
    """Send test from the Add dialog: one plain message to a service not saved yet."""
    return _sent(await _act(request).test({"instance_id": body.instance_id,
                                           "service": _service(body.service)}))


@router.post("/devices/{device_id}/test-buttons", dependencies=[Depends(_ADMIN)])
async def test_device_buttons(device_id: str, request: Request):
    """Yes and No to this phone; poll the request id for the tap (2 minutes)."""
    request_id, why = await _act(request).test_buttons(await _device(request, device_id))
    if request_id is None:
        raise HTTPException(409, why)
    return {"request_id": request_id}


@router.get("/test-buttons/{request_id}", dependencies=[Depends(require_auth)])
async def test_buttons_result(request_id: str, request: Request):
    row = _act(request).tests.get(request_id)
    if row is None:
        raise HTTPException(404, "No such test since the bridge started")
    return row


@router.post("/broadcast", dependencies=[Depends(_ADMIN)])
async def broadcast(body: BroadcastBody, request: Request):
    act = _act(request)
    if not act.enabled:
        raise HTTPException(409, "Push notifications are off")
    if not await act.targets():
        raise HTTPException(409, "No devices are on")
    return _sent(await act.broadcast(body.message, body.title))


# ── Auto-notify triggers ─────────────────────────────────────


@router.get("/triggers", dependencies=[Depends(require_auth)])
async def get_triggers(request: Request):
    """The triggers (labels, on/off, thresholds, Send to) and per-trigger counts."""
    trg = _triggers(request)
    return {"config": trg.cfg, "stats": await trg.stats()}


@router.put("/triggers", dependencies=[Depends(_ADMIN)])
async def put_triggers(body: TriggersUpdate, request: Request):
    """Change some triggers: ``{"triggers": {key: {enabled?, threshold?, target?}}}``.
    A bad value refuses the whole save."""
    try:
        return {"config": await _triggers(request).save(body.model_dump(exclude_unset=True))}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/triggers/test", dependencies=[Depends(_ADMIN)])
async def test_triggers(body: TriggerTest, request: Request):
    """Send test: one trigger notification to every enabled device, or to those chosen."""
    act = _act(request)
    if act.notifier is None:
        raise HTTPException(503, "Notifications are not running")
    if not act.enabled:
        raise HTTPException(409, "Push notifications are off")
    targets = await act.targets(body.device_ids) if body.device_ids else await act.targets()
    if not targets:
        raise HTTPException(409, "No devices are on")
    return _sent(await act.notifier.notify("trigger_test", targets))


# ── switch, check, log ───────────────────────────────────────


@router.get("/settings", dependencies=[Depends(require_auth)])
async def get_settings(request: Request):
    return {"push": _act(request).enabled}


@router.put("/settings", dependencies=[Depends(_ADMIN)])
async def put_settings(body: SettingsUpdate, request: Request):
    await _act(request).set_enabled(body.push)
    return {"push": body.push}


@router.get("/checks", dependencies=[Depends(require_auth)])
async def checks(request: Request):
    """"Would a notification reach me?" One line per check: pass, fail or not set up,
    with the fix."""
    act = _act(request)
    lines: list[dict] = []

    def line(name: str, status: str, detail: str, fix: str | None = None) -> None:
        lines.append({"name": name, "status": status, "detail": detail, "fix": fix})

    line("Push notifications switch", "pass" if act.enabled else "fail",
         "On" if act.enabled else "Off", None if act.enabled else "Switch it on.")

    reg = getattr(request.app.state, "ha_registry", None)
    primary = reg.primary() if reg is not None else None
    if primary is None:
        line("Default HA instance", "not_set_up", "No Home Assistant instance is set up",
             "Add one under Settings → Home Assistant.")
    else:
        await primary.refresh()
        if primary.connected:
            line("Default HA instance", "pass", f'"{primary.name}": reachable, token accepted')
        else:
            line("Default HA instance", "fail", f'"{primary.name}": {primary.last_error}',
                 "Check its URL and token (Edit → Test).")

    listeners = act.listener_status()
    if not listeners:
        line("Button taps listener", "not_set_up", "No enabled HA instance",
             "Add or enable an HA instance.")
    for st in listeners.values():
        ok = st["connected"]
        line(f"Button taps listener: {st['name']}", "pass" if ok else "fail",
             "Connected" if ok else (st["error"] or "Not connected yet"),
             None if ok else "Check that instance's URL and token.")

    phones = [t for t in await act.targets() if t.buttons]
    if phones:
        line("Phones (mobile_app_*)", "pass", f"{len(phones)} enabled phone(s)")
    else:
        line("Phones (mobile_app_*)", "not_set_up", "No enabled phone",
             "Install the Companion app, then Add device and pick its notify.mobile_app_* "
             "service.")

    last = await act.last_test()
    if last is None:
        line("Last Test with buttons", "not_set_up", "Never run",
             "Press Test with buttons on a phone's row, then tap Yes or No on the phone.")
    elif last["result"] in ("yes", "no"):
        line("Last Test with buttons", "pass",
             f"{last['alias']}: round trip works, tapped {last['result'].title()}")
    else:
        line("Last Test with buttons", "fail", f"{last['alias']}: no reply within 120 s",
             "Check the phone allows notifications from the Companion app, then try again.")
    return {"lines": lines}


@router.get("/log", dependencies=[Depends(require_auth)])
async def recent(request: Request, limit: int = 100):
    """Recent sends and taps, newest first."""
    return {"entries": await _act(request).recent(max(1, min(limit, 500)))}
