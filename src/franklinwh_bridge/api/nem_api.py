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


# ── Dynamic (live) tariff (#89 part B) ────────────────────────

DYNAMIC_NOTE = (
    "Energy is priced at the live rate. Demand, export-bonus, export-charge "
    "and standing charges still apply on top. With no live price for a slice, "
    "that energy is recorded as unpriced."
)


class DynamicLive(BaseModel):
    buy_c_kwh: float | None = None
    sell_c_kwh: float | None = None
    buy: float | None = Field(default=None, description="$/kWh")
    sell: float | None = Field(default=None, description="$/kWh")
    source: str | None = None
    sell_source: str | None = None
    label: str = ""
    settlement_ts: int | None = None
    stale: bool = False


class DynamicTariffResponse(BaseModel):
    kind: str = Field(description="the ACTIVE plan's pricing kind: static|dynamic")
    dynamic: dict[str, Any] | None = Field(
        default=None, description="the provider shown (the preview's, else the active plan's)"
    )
    preview: bool = False
    live: DynamicLive | None = None
    regions: list[str]
    ha_options: list[dict[str, Any]]
    note: str = DYNAMIC_NOTE


@router.get("/dynamic", response_model=DynamicTariffResponse)
async def tariff_dynamic(
    request: Request,
    source: str | None = None,
    region: str | None = None,
    buy_entity: str | None = None,
    feedin_entity: str | None = None,
):
    """The live price a dynamic plan is (or would be) priced at.

    With no ``source``: the ACTIVE plan's provider and the live price billing
    is using now (``live`` is null for a static plan). With ``source`` (``nem``
    + ``region``, or ``ha`` + ``buy_entity`` [+ ``feedin_entity``]): a preview
    for the tariff editor, before anything is saved. Invalid input -> 422.
    """
    from franklinwh_bridge.gateway.dynamic_pricing import normalise_dynamic

    svc = _service(request)
    billing = getattr(request.app.state, "billing", None)
    kind = billing.pricing_kind() if billing is not None else "static"

    if source is not None:
        try:
            dyn = normalise_dynamic({
                "source": source, "region": region,
                "buy_entity": buy_entity, "feedin_entity": feedin_entity,
            })
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        preview = True
    else:
        dyn = billing.dynamic_config() if billing is not None else None
        preview = False

    live = None
    if dyn is not None:
        got = await svc.preview_prices(dyn) if preview else svc.dynamic_prices(dyn)
        live = DynamicLive(**{k: got.get(k) for k in DynamicLive.model_fields if k in got})
    return DynamicTariffResponse(
        kind=kind, dynamic=dyn, preview=preview, live=live,
        regions=list(REGIONS), ha_options=_ha_options(request),
    )
