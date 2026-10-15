"""Wholesale spot price REST (#89, part A) — AEMO NEM + optional HA price entities.

``GET /api/tariff/spot`` is the Local Bridge's route, same core shape (region,
regions, configured_region, spot, ha, ha_options, active), so a client can talk
to either bridge. The settings routes are this bridge's (the Local Bridge folds
them into its generic settings PUT), mirroring ``/api/solar/settings``.

Mounted WITHOUT the ``energy_costs`` module gate: the region is a site setting
that feeds condition sensors, and must work with the Energy Costs tab off.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from franklinwh_bridge.api.auth import require_role
from franklinwh_bridge.gateway.aemo_nem import REGIONS, NemPricingService

router = APIRouter(prefix="/api/tariff", tags=["tariff"])

_REQUIRE_ADMIN = Depends(require_role("admin"))

NEM_HELP = (
    "Australia only: the AEMO National Electricity Market 5-minute wholesale "
    "spot price (RRP) for your region, not your retail tariff."
)


class SpotSettingsBody(BaseModel):
    """Omitted fields are left as they are; ``""`` switches a field off."""

    nem_region: str | None = Field(default=None, description="NSW1/QLD1/SA1/TAS1/VIC1, '' = off")
    tariff_price_entity: str | None = Field(
        default=None, description="HA buy price, 'ha:<instance>:<entity_id>', '' = none"
    )
    tariff_feedin_entity: str | None = Field(
        default=None, description="HA feed-in price, 'ha:<instance>:<entity_id>', '' = none"
    )


class SpotSettingsResponse(BaseModel):
    values: dict[str, Any]
    regions: list[str]
    configured: bool
    help: str = NEM_HELP


def _service(request: Request) -> NemPricingService:
    svc = getattr(request.app.state, "nem_pricing", None)
    if svc is None:
        raise HTTPException(503, "Spot pricing not available")
    return svc


def _ha_options(request: Request) -> list[dict[str, Any]]:
    """Exposed numeric HA entities a price can come from."""
    reg = getattr(request.app.state, "ha_registry", None)
    if reg is None:
        return []
    return [
        {"id": e["id"], "label": e["label"], "unit": e.get("unit")}
        for e in reg.catalog()
        if e.get("kind") == "number"
    ]


def _settings_response(svc: NemPricingService) -> SpotSettingsResponse:
    return SpotSettingsResponse(
        values=svc.values(), regions=list(REGIONS), configured=bool(svc.settings.nem_region)
    )


@router.get("/spot")
async def tariff_spot(request: Request, region: str | None = None, force: bool = False):
    """Live wholesale price: the AEMO NEM spot for ``region`` (default: the
    configured one) and the configured HA price entities, plus the ACTIVE
    buy/sell (HA wins per side over NEM). No region and nothing configured ->
    ``spot: null``, and nothing is fetched."""
    svc = _service(request)
    configured = svc.settings.nem_region
    reg = (region or configured or "").upper()
    if reg and reg not in REGIONS:
        raise HTTPException(400, f"region must be one of {', '.join(REGIONS)}")
    spot = await svc.client.spot_price(reg, force=force) if reg else None
    ha = svc.ha_prices()
    s = svc.settings
    return {
        "region": reg or None,
        "regions": list(REGIONS),
        "configured_region": configured or None,
        "spot": spot,
        "ha": {
            "price_entity": s.tariff_price_entity or None,
            "feedin_entity": s.tariff_feedin_entity or None,
            **ha,
        },
        "ha_options": _ha_options(request),
        # The active source is for the CONFIGURED region (what the sensors
        # see), not an ad-hoc ?region= lookup.
        "active": svc.active(
            spot if reg == configured else svc.client.cached(configured) if configured else None
        ),
        "help": NEM_HELP,
    }


@router.get("/spot/settings", response_model=SpotSettingsResponse)
async def get_spot_settings(request: Request):
    """NEM region + HA price entities, for the Settings form."""
    return _settings_response(_service(request))


@router.put("/spot/settings", response_model=SpotSettingsResponse)
async def put_spot_settings(body: SpotSettingsBody, request: Request, _user: dict = _REQUIRE_ADMIN):
    """Update the NEM region / HA price entities (only the fields sent)."""
    svc = _service(request)
    try:
        await svc.update(body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return _settings_response(svc)
