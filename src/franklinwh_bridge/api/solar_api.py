"""Weather + solar forecast REST (#88) — Open-Meteo, scaled to the PV array.

``GET /api/solar/forecast`` is the Local Bridge's route, same shape, so a client
can talk to either bridge. The settings routes are this bridge's (the Local
Bridge folds them into its generic settings PUT). There is no "sync from
gateway": Modbus has no location register. A configured Home Assistant can
supply its own home location instead (``get_config``).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.api.auth import require_role
from franklinwh_bridge.gateway.solar_forecast import (
    NOT_CONFIGURED_REASON,
    SolarForecastService,
    actual_vs_forecast,
    sensors,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/solar", tags=["solar"])

_REQUIRE_ADMIN = Depends(require_role("admin"))

AZIMUTH_HELP = "Open-Meteo convention: 0 = south, -90 = east, 90 = west, ±180 = north."


class SolarSettingsBody(BaseModel):
    """Location + PV array. Send ``null`` for latitude/longitude to switch the
    forecast off; omitted fields are left as they are."""

    pv_latitude: float | None = Field(default=None, ge=-90, le=90)
    pv_longitude: float | None = Field(default=None, ge=-180, le=180)
    pv_kwp: float = Field(default=5.0, gt=0, le=1000, description="Array size (kWp)")
    pv_tilt: float = Field(default=20.0, ge=0, le=90, description="Panel tilt (deg)")
    pv_azimuth: float = Field(default=0.0, ge=-180, le=180, description=AZIMUTH_HELP)


class SolarSettingsResponse(BaseModel):
    values: dict[str, Any]
    configured: bool
    azimuth_help: str = AZIMUTH_HELP
    #: HA instances that can supply a location (empty = no "use HA" option).
    ha_instances: list[dict[str, str]] = Field(default_factory=list)


def _service(request: Request) -> SolarForecastService:
    svc = getattr(request.app.state, "solar_forecast", None)
    if svc is None:
        raise HTTPException(503, "Solar forecast not available")
    return svc


def _ha_instances(request: Request) -> list[dict[str, str]]:
    reg = getattr(request.app.state, "ha_registry", None)
    if reg is None:
        return []
    return [{"id": s["id"], "name": s["name"]} for s in reg.status()]


def _settings_response(request: Request, svc: SolarForecastService) -> SolarSettingsResponse:
    return SolarSettingsResponse(
        values=svc.values(),
        configured=svc.settings.configured,
        ha_instances=_ha_instances(request),
    )


@router.get("/forecast")
async def solar_forecast(request: Request, force: bool = False, gateway: str = "default"):
    """Weather + PV production forecast. Needs latitude/longitude set; otherwise
    ``{configured: false, reason}``. Cached 15 min; ``force`` refetches."""
    svc = _service(request)
    s = svc.settings
    if not s.configured:
        return {"configured": False, "reason": NOT_CONFIGURED_REASON}
    fc = await svc.forecast(force=force)
    # Actual PV today (counter delta, same figure as energy.solar.today_kwh).
    actual = None
    totals = getattr(request.app.state, "energy_totals", None)
    if totals is not None:
        try:
            actual = totals.current_totals(gateway).get("energy_solar_today_kwh")
        except Exception as exc:
            logger.debug("energy totals unavailable for %s: %s", gateway, exc)
    sens = sensors(fc)
    sens.update(
        actual_vs_forecast(
            actual, sens["solar_forecast.today_kwh"], sens["solar_forecast.remaining_kwh"]
        )
    )
    return {
        "configured": True,
        **fc,
        "pv": {
            "kwp": s.pv_kwp,
            "tilt": s.pv_tilt,
            "azimuth": s.pv_azimuth,
            "lat": s.pv_latitude,
            "lon": s.pv_longitude,
        },
        "actual_today_kwh": actual,
        "sensors": sens,
    }


@router.get("/settings", response_model=SolarSettingsResponse)
async def get_solar_settings(request: Request):
    """Location + PV array settings for the Settings form."""
    return _settings_response(request, _service(request))


@router.put("/settings", response_model=SolarSettingsResponse)
async def put_solar_settings(
    body: SolarSettingsBody, request: Request, _user: dict = _REQUIRE_ADMIN
):
    """Update the location / PV array (only the fields sent)."""
    svc = _service(request)
    try:
        await svc.update(body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return _settings_response(request, svc)


@router.post("/location/from-ha", response_model=SolarSettingsResponse)
async def location_from_ha(
    request: Request, instance: str | None = None, _user: dict = _REQUIRE_ADMIN
):
    """Use a configured Home Assistant's home location (its ``get_config``
    latitude/longitude) as the forecast location, and save it."""
    svc = _service(request)
    reg = getattr(request.app.state, "ha_registry", None)
    insts = _ha_instances(request)
    if reg is None or not insts:
        raise HTTPException(404, "No Home Assistant instance is configured")
    inst_id = instance or insts[0]["id"]
    inst = reg.get(inst_id)
    if inst is None:
        raise HTTPException(404, f"Home Assistant instance {inst_id!r} not found")
    try:
        cfg = await inst.ws_call({"type": "get_config"})
    except Exception as exc:
        raise HTTPException(502, f"Home Assistant did not answer: {exc}") from None
    lat = (cfg or {}).get("latitude")
    lon = (cfg or {}).get("longitude")
    if lat is None or lon is None:
        raise HTTPException(422, "Home Assistant did not report a location")
    try:
        await svc.update({"pv_latitude": lat, "pv_longitude": lon})
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return _settings_response(request, svc)
