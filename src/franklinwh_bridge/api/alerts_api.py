"""Critical alerts over REST (#324): list them, and acknowledge one (admin).

The same endpoints as the DC bridge (its #271), so one HA automation or script
works with either bridge. The summary for the UI's poll is in /api/status.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from franklinwh_bridge import alert_delivery
from franklinwh_bridge.api.auth import require_auth, require_role
from franklinwh_bridge.store import alerts as alert_store

router = APIRouter(prefix="/api", tags=["alerts"])
_ADMIN = require_role("admin")


class Delivery(BaseModel):
    channel: str
    status: str
    attempts: int
    delivered_ts: float | None = None
    last_error: str | None = None


class Alert(BaseModel):
    id: int
    gateway_id: str
    kind: Literal["event", "condition"]
    severity: Literal["warning", "critical"]
    code: str
    title: str
    message: str
    opened_ts: float
    last_ts: float
    count: int
    resolved_ts: float | None = None
    acked_by: str | None = None
    acked_ts: float | None = None
    deliveries: list[Delivery] = []


class AckResult(BaseModel):
    ok: bool
    id: int
    acked_by: str


def _db(request: Request):
    db = getattr(request.app.state, "db", None)
    if db is None:
        raise HTTPException(503, "the database is not ready")
    return db


@router.get("/alerts", response_model=list[Alert], dependencies=[Depends(require_auth)])
async def get_alerts(
    request: Request,
    open: bool = Query(False, description="only alerts that still need someone"),  # noqa: A002
    kind: Literal["event", "condition"] | None = None,
    since: float | None = Query(None, description="epoch seconds; last activity at or after"),
    limit: int = Query(200, ge=1, le=1000),
) -> list[dict]:
    """Alerts newest first, with where each was delivered. Any signed-in role."""
    return await alert_store.list_alerts(
        _db(request), open_only=open, kind=kind, since=since, limit=limit)


@router.post("/alerts/{alert_id}/ack", response_model=AckResult)
async def ack_alert(
    alert_id: int, request: Request, user: dict = Depends(_ADMIN),
) -> AckResult:
    """Acknowledge for everyone (admin). Recorded as ``user:<name>``."""
    acked_by = f"user:{user.get('username') or '?'}"
    if not await alert_store.ack(_db(request), alert_id, acked_by):
        raise HTTPException(404, "no such alert, or it is already acknowledged")
    return AckResult(ok=True, id=alert_id, acked_by=acked_by)


class AlertSettings(BaseModel):
    ha_channel: bool
    #: True when a Home Assistant is set up to receive the notifications.
    ha_configured: bool = False
    #: The standing warning the settings page shows while the HA channel is off.
    warning: str | None = None


class AlertSettingsUpdate(BaseModel):
    ha_channel: bool


async def _settings(request: Request) -> AlertSettings:
    on = await alert_delivery.ha_channel_on(_db(request))
    ha = getattr(request.app.state, "ha_registry", None)
    configured = bool(ha and ha.primary())
    warning = None
    if not on:
        warning = "Critical alerts reach only the bridge UI."
    elif not configured:
        warning = "No Home Assistant is set up: critical alerts reach only the bridge UI."
    return AlertSettings(ha_channel=on, ha_configured=configured, warning=warning)


@router.get("/alerts/settings", response_model=AlertSettings,
            dependencies=[Depends(require_auth)])
async def get_alert_settings(request: Request) -> AlertSettings:
    """Whether critical alerts also go to Home Assistant (on by default)."""
    return await _settings(request)


@router.put("/alerts/settings", response_model=AlertSettings, dependencies=[Depends(_ADMIN)])
async def put_alert_settings(body: AlertSettingsUpdate, request: Request) -> AlertSettings:
    """Switch HA notifications on or off (admin). Off never hides the bridge's modal."""
    await alert_delivery.set_ha_channel(_db(request), body.ha_channel)
    return await _settings(request)
