"""Gateway management REST endpoints.

CRUD for gateways, per-gateway TCP connectivity test, start/stop control,
and site configuration.  Also provides gateway-scoped versions of existing
endpoints (points, command, models, battery limits).
"""

from __future__ import annotations

import logging
import time
from zoneinfo import ZoneInfo

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from franklinwh_bridge.config import clock
from franklinwh_bridge.gateway import diagnostics as diagnostics_mod
from franklinwh_bridge.gateway.net_probe import tcp_probe
from franklinwh_bridge.gateway.phase_detect import detect_phases, phase_matches
from franklinwh_bridge.publish.command_handler import DEFAULT_MAX_POWER_W
from franklinwh_bridge.store.db import (
    create_gateway,
    create_service,
    delete_gateway,
    delete_service,
    get_gateway,
    get_gateways,
    get_service,
    get_services,
    get_site_config,
    service_has_history,
    start_new_plan,
    update_gateway,
    update_service,
    update_site_config,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["gateways"])


def _registry(request: Request):
    reg = getattr(request.app.state, "registry", None)
    if reg is None:
        raise HTTPException(503, "Gateway registry not available")
    return reg


# ── Site config ───────────────────────────────────────────────


class SiteConfigUpdate(BaseModel):
    # Reject unknown fields instead of dropping them. Pydantic's default is to
    # IGNORE an undeclared key, so a request naming a field the model doesn't
    # have returned 200 having changed nothing — which happened twice while
    # building this router (ac_type, then plan_version), each time reading as a
    # successful save. A 422 is far better than a silent no-op.
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    description: str | None = None
    meter_number: str | None = None
    account_number: str | None = None
    ac_service_type: int | None = Field(default=None, ge=1, le=3)
    aggregate_entities: bool | None = None
    full_backup: bool | None = None
    grid_forming: bool | None = None
    generator_input: bool | None = None
    solar_type: str | None = None
    solar_kwp: float | None = Field(default=None, ge=0)
    load_shedding: bool | None = None
    nonbackup_loads: bool | None = None
    battery_label: str | None = None


@router.get("/site")
async def get_site(request: Request):
    """Return site configuration."""
    db: aiosqlite.Connection = request.app.state.db
    return await get_site_config(db)


@router.get("/site/status")
async def get_site_status(request: Request):
    """Return aggregated site-level status from all gateways."""
    aggregator = getattr(request.app.state, "site_aggregator", None)
    if aggregator is None:
        return {"gateway_count": 0, "points": {}}
    return {
        "gateway_count": aggregator.gateway_count,
        "points": aggregator.site_points,
        "ts": aggregator.last_update or None,
    }


@router.patch("/site")
async def patch_site(body: SiteConfigUpdate, request: Request):
    """Update site configuration."""
    db: aiosqlite.Connection = request.app.state.db
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "aggregate_entities" in updates:
        updates["aggregate_entities"] = int(updates["aggregate_entities"])
    return await update_site_config(db, **updates)


# ── System timezone ───────────────────────────────────────────
# Schedules, TOU windows and every time.* sensor run on the bridge's local
# clock, and nothing in the app chooses it — it comes from the container. The
# startup guard catches that clock DRIFTING, but it seeds itself from the
# environment on first run, so an install that was wrong from day one (a
# container with no TZ resolves to UTC) would be verified as correct forever.
# Confirmation is the only thing that closes that, and only a person can give
# it.


class TimezoneConfirm(BaseModel):
    # Reject unknown fields instead of dropping them. Pydantic's default is to
    # IGNORE an undeclared key, so a request naming a field the model doesn't
    # have returned 200 having changed nothing — which happened twice while
    # building this router (ac_type, then plan_version), each time reading as a
    # successful save. A 422 is far better than a silent no-op.
    model_config = ConfigDict(extra="forbid")

    # None = "the detected one is right". A name = correct it to this.
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("timezone")
    @classmethod
    def _known(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(
                f"unknown timezone '{v}' — use an IANA name like Australia/Sydney"
            ) from exc
        return v


@router.get("/system/timezone")
async def get_timezone(request: Request):
    """Detected timezone, whether a user ever confirmed it, and the local time
    right now — so the prompt can show what the bridge believes rather than
    asking the user to take it on trust."""
    return await clock.status(request.app.state.db)


@router.post("/system/timezone/confirm")
async def confirm_timezone_endpoint(body: TimezoneConfirm, request: Request):
    """Record the user's decision, optionally correcting the timezone.

    Correcting it here only changes what the bridge EXPECTS. The clock itself
    comes from the container, so a correction must also be applied there (TZ in
    docker-compose, or the Supervisor's setting) — otherwise the next start
    reports a mismatch, which is the honest outcome rather than a silent
    disagreement.
    """
    return await clock.confirm_timezone(request.app.state.db, body.timezone)


# ── Electricity Utility Services ──────────────────────────────


class ServiceCreate(BaseModel):
    # Reject unknown fields instead of dropping them. Pydantic's default is to
    # IGNORE an undeclared key, so a request naming a field the model doesn't
    # have returned 200 having changed nothing — which happened twice while
    # building this router (ac_type, then plan_version), each time reading as a
    # successful save. A 422 is far better than a silent no-op.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    meter_number: str = Field(default="", max_length=120)
    account: str = Field(default="", max_length=120)
    ac_service: int = Field(default=1, ge=1, le=3)
    rated_amps: int = Field(default=0, ge=0, le=10000)


class TariffWindow(BaseModel):
    """A season/day/time window a tariff period applies in. Empty months/days
    mean "all"; start/end are local HH:MM."""

    months: list[int] = Field(default_factory=list)   # 1..12
    days: list[int] = Field(default_factory=list)      # 0..6, Mon=0
    start: str = Field(default="00:00", pattern=r"^\d{1,2}:\d{2}$")
    end: str = Field(default="23:59", pattern=r"^\d{1,2}:\d{2}$")


class ServiceUpdate(BaseModel):
    # Reject unknown fields instead of dropping them. Pydantic's default is to
    # IGNORE an undeclared key, so a request naming a field the model doesn't
    # have returned 200 having changed nothing — which happened twice while
    # building this router (ac_type, then plan_version), each time reading as a
    # successful save. A 422 is far better than a silent no-op.
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    meter_number: str | None = Field(default=None, max_length=120)
    account: str | None = Field(default=None, max_length=120)
    ac_service: int | None = Field(default=None, ge=1, le=3)
    rated_amps: int | None = Field(default=None, ge=0, le=10000)
    # ── Billing / tariff (migration 33) ──
    has_tou: bool | None = None
    has_peak_demand: bool | None = None
    has_export_bonus: bool | None = None
    min_monthly_bill: float | None = Field(default=None, ge=0)
    pricing_api: str | None = Field(default=None, pattern=r"^(none|ha|direct)$")
    demand_window: TariffWindow | None = None
    bonus_window: TariffWindow | None = None
    pricing: dict | None = None
    # ── Plan description + what the plan permits (migration 36) ──
    # plan_type is informational: it names the tariff structure but drives no
    # rate calculation. The permissions are utility/plan requirements, surfaced
    # to automations as service.* sensors.
    plan_type: str | None = Field(
        # 'hybrid' added 2026-09-15: time-of-use bands whose prices are tiered.
        default=None,
        pattern=r"^(unknown|fixed|tiered|tou|hybrid|demand|dynamic|other)$",
    )
    export_allowed: bool | None = None
    export_limit_kw: float | None = Field(default=None, ge=0)  # None/0 → unlimited
    charging_allowed: bool | None = None
    discharging_allowed: bool | None = None
    # ── Where the service is billed (migration 38) ──
    # Advisory: the engine still evaluates windows on the container clock. This
    # records what the plan's TOU windows are WRITTEN in, so the two can be
    # compared — see service.tz_matches_clock.
    country: str | None = Field(default=None, max_length=2)
    timezone: str | None = Field(default=None, max_length=64)
    # Accepted and IGNORED, not forbidden. It is a UI-only toggle — the server
    # derives the real state from pricing.export_charge — but older cached
    # page loads still send it, and 422-ing them means a user with a stale tab
    # simply cannot save. Tightening a schema must not break clients already in
    # flight; declaring the field keeps them working and documents why it's
    # inert. (Hit for real on 2026-09-14.)
    has_export_charge: bool | None = Field(default=None, exclude=True)

    # ── Supplier + lifecycle (migration 40) ──
    # Retailer and network change independently: in AU you can switch retailer
    # (AGL) and stay on the same network/DNSP (Ausgrid), whose two-way export
    # tariff applies either way.
    retailer: str | None = Field(default=None, max_length=120)
    network: str | None = Field(default=None, max_length=120)
    # Retiring a service stops it pricing anything but keeps its history.
    enabled: bool | None = None
    # ── Connection-level, granted by the NETWORK (migration 42) ──
    # Not plan terms: these survive a change of retailer or plan.
    pto_status: str | None = Field(
        default=None, pattern=r"^(unknown|none|pending|approved)$"
    )
    pto_date: float | None = None
    pto_reference: str | None = Field(default=None, max_length=120)

    @field_validator("country")
    @classmethod
    def _upper_country(cls, v: str | None) -> str | None:
        """ISO 3166-1 alpha-2, upper-cased. "" clears it."""
        if v is None:
            return None
        v = v.strip().upper()
        if v and not (len(v) == 2 and v.isalpha()):
            raise ValueError("country must be a 2-letter ISO code (e.g. AU, NZ, US)")
        return v

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str | None) -> str | None:
        """Reject anything zoneinfo can't resolve.

        A timezone that only looks right is worse than a blank one: it reads as
        confirmation while being unusable, which is exactly how a clock problem
        stays invisible.
        """
        if v is None:
            return None
        v = v.strip()
        if not v:
            return ""
        try:
            ZoneInfo(v)
        except Exception as exc:
            raise ValueError(
                f"unknown timezone '{v}' — use an IANA name like Australia/Sydney"
            ) from exc
        return v


class PlanSwitch(BaseModel):
    """Start a new plan on a service. Both fields optional — switching plan with
    the same retailer (a re-contract) is as common as changing supplier."""
    # Reject unknown fields instead of dropping them. Pydantic's default is to
    # IGNORE an undeclared key, so a request naming a field the model doesn't
    # have returned 200 having changed nothing — which happened twice while
    # building this router (ac_type, then plan_version), each time reading as a
    # successful save. A 422 is far better than a silent no-op.
    model_config = ConfigDict(extra="forbid")


    retailer: str | None = Field(default=None, max_length=120)
    network: str | None = Field(default=None, max_length=120)


@router.post("/services/{service_id}/switch-plan")
async def switch_plan(service_id: str, body: PlanSwitch, request: Request):
    """Close the current billing period and begin a new plan.

    Totals reset at this boundary deliberately: a period must never span two
    price sets, or its cost is an average of tariffs that were never both in
    force. Closed periods keep their own snapshot of retailer/network/version,
    so history reads as what it was.
    """
    db: aiosqlite.Connection = request.app.state.db
    if await get_service(db, service_id) is None:
        raise HTTPException(404, f"Service '{service_id}' not found")

    # Close the period FIRST, so the numbers accrued so far are attributed to
    # the outgoing plan rather than the incoming one.
    tracker = getattr(request.app.state, "demand_tracker", None)
    closed = False
    if tracker is not None and hasattr(tracker, "close_period_now"):
        try:
            closed = bool(await tracker.close_period_now())
        except Exception as exc:
            logger.warning("Could not close the billing period on switch: %s", exc)

    row = await start_new_plan(
        db, service_id, retailer=body.retailer, network=body.network,
    )
    store = getattr(request.app.state, "billing", None)
    if store is not None:
        await store.load()
    return {"service": row, "period_closed": closed}


# ── Tariff profile export / import ────────────────────────────
# Mirrors the schedule bundle (schedules_api) so both share one idea of what a
# shareable artefact looks like.
_TARIFF_EXPORT_TYPE = "franklinwh-bridge/tariff-profile"
_TARIFF_EXPORT_VERSION = 1

#: What travels. Deliberately EXCLUDES anything identifying: meter number,
#: account, PTO reference and the network's approval are specific to one
#: connection and must not be shared with a plan. A profile describes a TARIFF,
#: not a customer.
_PORTABLE_SERVICE_FIELDS = (
    "name", "retailer", "network", "plan_type", "country", "timezone",
    "has_tou", "has_peak_demand", "has_export_bonus", "min_monthly_bill",
    "demand_window", "bonus_window", "pricing",
)


@router.get("/services/{service_id}/export")
async def export_service(service_id: str, request: Request):
    """Export a service's tariff as a portable profile.

    Identifying fields are stripped: sharing a plan should not share a meter
    number, an account, or a network approval that belongs to one connection.
    """
    row = await get_service(request.app.state.db, service_id)
    if row is None:
        raise HTTPException(404, f"Service '{service_id}' not found")
    profile = {k: row.get(k) for k in _PORTABLE_SERVICE_FIELDS}
    return {
        "type": _TARIFF_EXPORT_TYPE,
        "version": _TARIFF_EXPORT_VERSION,
        "exported_at": time.time(),
        "profile": profile,
    }


class TariffImportBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str | None = None
    version: int | None = None
    exported_at: float | None = None
    profile: dict = Field(default_factory=dict)


@router.post("/services/import")
async def import_service(
    bundle: TariffImportBundle, request: Request,
    dry_run: bool = True, service_id: str | None = None,
):
    """Validate a tariff profile, and optionally apply it.

    ``dry_run=true`` (the default) reports what would happen without touching
    anything — importing a tariff overwrites rates that price real money, so it
    should never be a single unconfirmed click.

    With ``service_id`` the profile is applied to that service, leaving its
    identity and network grant intact. Without one, a new service is created.
    """
    from franklinwh_bridge.gateway.rate_model import validate as validate_rates

    if bundle.type and bundle.type != _TARIFF_EXPORT_TYPE:
        raise HTTPException(400, f"unexpected bundle type '{bundle.type}'")

    profile = {k: v for k, v in bundle.profile.items() if k in _PORTABLE_SERVICE_FIELDS}
    unknown = sorted(set(bundle.profile) - set(_PORTABLE_SERVICE_FIELDS))
    pricing = profile.get("pricing") if isinstance(profile.get("pricing"), dict) else {}
    problems = validate_rates(pricing.get("seasons"), pricing.get("default_rate"))

    report = {
        "ok": not problems,
        "name": profile.get("name"),
        "retailer": profile.get("retailer"),
        "network": profile.get("network"),
        "seasons": len(pricing.get("seasons") or []),
        "rate_problems": problems,
        # Named rather than silently dropped: a profile from a newer bridge may
        # carry fields this one doesn't understand, and the user should know
        # what didn't come across.
        "ignored_fields": unknown,
    }
    if dry_run:
        return report
    if problems:
        raise HTTPException(400, "profile has rate problems; fix them or import as dry run")

    db: aiosqlite.Connection = request.app.state.db
    if service_id:
        if await get_service(db, service_id) is None:
            raise HTTPException(404, f"Service '{service_id}' not found")
        # Identity and the network's grant stay put — only the tariff lands.
        applied = {k: v for k, v in profile.items() if k != "name" and v is not None}
        row = await update_service(db, service_id, **applied)
    else:
        row = await create_service(db, name=profile.get("name") or "Imported tariff")
        applied = {k: v for k, v in profile.items() if k != "name" and v is not None}
        row = await update_service(db, row["id"], **applied)

    store = getattr(request.app.state, "billing", None)
    if store is not None:
        await store.load()
    return {**report, "applied_to": row["id"]}


class RateValidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seasons: list[dict] = Field(default_factory=list)
    default_rate: dict | None = None


@router.post("/tariff/validate-rates")
async def validate_rates(body: RateValidateBody):
    """Check a season/wave plan before it is saved.

    Server-side because the resolver is the authority on what it will actually
    price — a UI that validated separately would drift from it. Returns
    ``{ok, problems: [...]}`` where problems are plain sentences.
    """
    from franklinwh_bridge.gateway.rate_model import TIME_PERIOD_LABELS, validate

    problems = validate(body.seasons, body.default_rate)
    return {
        "ok": not problems, "problems": problems,
        "time_period_labels": TIME_PERIOD_LABELS,
    }


@router.get("/services/{service_id}/history-count")
async def service_history_count(service_id: str, request: Request):
    """How many closed billing periods this service produced — the UI uses it
    to offer 'retire' instead of 'delete'."""
    return {"periods": await service_has_history(request.app.state.db, service_id)}


@router.get("/services")
async def list_services(request: Request):
    """List all electricity utility services."""
    db: aiosqlite.Connection = request.app.state.db
    return {"services": await get_services(db)}


@router.post("/services", status_code=201)
async def add_service(body: ServiceCreate, request: Request):
    """Create a new utility service."""
    db: aiosqlite.Connection = request.app.state.db
    return await create_service(
        db,
        name=body.name,
        meter_number=body.meter_number,
        account=body.account,
        ac_service=body.ac_service,
        rated_amps=body.rated_amps,
    )


#: Editing any of these changes what the CURRENT period is being priced at.
#: The user is asked whether it's a new plan or a correction, rather than the
#: bridge guessing: auto-resetting on every edit would wipe a month's totals
#: over a typo'd rate, and never asking lets one period span two price sets.
_PLAN_MATERIAL_FIELDS = (
    "pricing", "demand_window", "bonus_window", "has_tou", "has_peak_demand",
    "has_export_bonus", "plan_type", "min_monthly_bill",
)


@router.patch("/services/{service_id}")
async def patch_service(service_id: str, body: ServiceUpdate, request: Request):
    """Update a utility service. exclude_unset so booleans/windows can be set to
    false/null explicitly (e.g. clearing a tariff window)."""
    db: aiosqlite.Connection = request.app.state.db
    updates = body.model_dump(exclude_unset=True)
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    result = await update_service(db, service_id, **updates)
    if result is None:
        raise HTTPException(404, f"Service '{service_id}' not found")

    # Flag a pricing edit so the UI can ask "new plan, or a correction?".
    # Advisory only — nothing resets here. Suppressed when the service has no
    # closed periods yet, because then there is no history for a boundary to
    # protect and the question is just noise during setup.
    touched = [f for f in _PLAN_MATERIAL_FIELDS if f in updates]
    result = dict(result)
    result["plan_change_suspected"] = bool(
        touched and await service_has_history(db, service_id)
    )
    result["plan_changed_fields"] = touched

    # Refresh the billing-window cache so demand.*/bonus.* sensors reflect the edit.
    billing = getattr(request.app.state, "billing", None)
    if billing is not None:
        await billing.load()
    fixed = getattr(request.app.state, "fixed_charges", None)
    if fixed is not None:
        await fixed.load()
    return result


@router.delete("/services/{service_id}")
async def remove_service(service_id: str, request: Request):
    """Delete a utility service — refused once it has produced billing history.

    Deleting would strand closed periods against a service that no longer
    exists, which is exactly the continuity the history view is for. Retiring
    (``enabled: false``) stops it pricing anything while keeping its rows.
    """
    db: aiosqlite.Connection = request.app.state.db
    periods = await service_has_history(db, service_id)
    if periods:
        raise HTTPException(
            409,
            f"'{service_id}' has {periods} closed billing period(s). Retire it "
            "(set enabled: false) instead — deleting would orphan that history.",
        )
    if not await delete_service(db, service_id):
        raise HTTPException(404, f"Service '{service_id}' not found")
    return {"deleted": True}


# ── Gateway CRUD ──────────────────────────────────────────────


class GatewayCreate(BaseModel):
    # Reject unknown fields instead of dropping them. Pydantic's default is to
    # IGNORE an undeclared key, so a request naming a field the model doesn't
    # have returned 200 having changed nothing — which happened twice while
    # building this router (ac_type, then plan_version), each time reading as a
    # successful save. A 422 is far better than a silent no-op.
    model_config = ConfigDict(extra="forbid")

    gateway_id: str = Field(..., min_length=1, max_length=63)
    name: str = Field(..., min_length=1, max_length=120)
    host: str = Field(default="", max_length=255)
    port: int = Field(default=502, ge=1, le=65535)
    unit_id: int = Field(default=1, ge=1, le=247)
    description: str = ""
    poll_interval: int = Field(default=10, ge=1, le=300)
    timeout: float = Field(default=10.0, ge=1, le=60)
    mock: bool = False
    # 'agate' = full battery system; 'mac1' = Meter Adaptor Collar (metering
    # only, far fewer SunSpec models, no battery to command).
    device_type: str = Field(default="agate", pattern=r"^(agate|mac1)$")


class GatewayUpdate(BaseModel):
    # Reject unknown fields instead of dropping them. Pydantic's default is to
    # IGNORE an undeclared key, so a request naming a field the model doesn't
    # have returned 200 having changed nothing — which happened twice while
    # building this router (ac_type, then plan_version), each time reading as a
    # successful save. A 422 is far better than a silent no-op.
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    unit_id: int | None = Field(default=None, ge=1, le=247)
    description: str | None = None
    poll_interval: int | None = Field(default=None, ge=1, le=300)
    timeout: float | None = Field(default=None, ge=1, le=60)
    enabled: bool | None = None
    # Layer-2 linkage. service_id='' clears the link; phase in all|L1|L2|L3|combo.
    service_id: str | None = Field(default=None, max_length=63)
    phase: str | None = Field(default=None, pattern=r"^(all|L[123](\+L[123])*)$")
    phase_view: str | None = Field(default=None, pattern=r"^(both|aggregate|per_phase)$")
    device_type: str | None = Field(default=None, pattern=r"^(agate|mac1)$")
    # 0 single phase, 1 split phase (L1+L2), 2 three phase. A real gateway
    # detects this from Modbus and overwrites it; settable mainly so a MOCK can
    # stand in for a US split-phase aGate without US hardware.
    ac_type: int | None = Field(default=None, ge=0, le=2)
    # Publish this gateway's own MQTT/HA Discovery entities (migration 39).
    # Turning it off tombstones its discovery so HA removes the entities rather
    # than leaving them permanently unavailable.
    publish_to_ha: bool | None = None


@router.get("/gateways")
async def list_gateways(request: Request):
    """List all gateways with live status from the registry."""
    db: aiosqlite.Connection = request.app.state.db
    db_rows = await get_gateways(db)

    registry = getattr(request.app.state, "registry", None)
    gateways = []
    for row in db_rows:
        gw_id = row["id"]
        entry = dict(row)
        # Merge live status from registry if instance exists
        if registry:
            inst = registry.get(gw_id)
            if inst:
                entry.update({
                    "connected": inst.status.connected,
                    "polling": inst.status.polling,
                    "health": inst.status.health,
                    "serial": inst.status.serial or entry.get("serial"),
                    "model": inst.status.model or entry.get("model"),
                    "firmware": inst.status.firmware or entry.get("firmware"),
                    "ac_type": inst.status.ac_type,
                    "last_poll_ts": (
                        inst.poller.state.last_poll_ts
                        if inst.poller else None
                    ),
                    "last_error": inst.status.last_error,
                })
            else:
                entry.update({
                    "connected": False,
                    "polling": False,
                    "health": "stopped" if entry.get("enabled") else "disabled",
                })
        gateways.append(entry)

    return {"gateways": gateways}


@router.post("/gateways", status_code=201)
async def add_gateway(body: GatewayCreate, request: Request):
    """Add a new gateway."""
    db: aiosqlite.Connection = request.app.state.db
    existing = await get_gateway(db, body.gateway_id)
    if existing:
        raise HTTPException(409, f"Gateway '{body.gateway_id}' already exists")

    # Conflict guards apply to REAL gateways only (mocks never open a Modbus
    # connection, so they can't contend or clash).
    if not body.mock:
        if not body.host:
            raise HTTPException(400, "Host is required for a real gateway")
        for row in await get_gateways(db):
            if row.get("mock"):
                continue
            if (
                row["host"] == body.host
                and row["port"] == body.port
                and row.get("unit_id", 1) == body.unit_id
            ):
                raise HTTPException(
                    409,
                    f"Gateway '{row['id']}' already polls {body.host}:{body.port} "
                    f"unit {body.unit_id}. Two real gateways can't share one "
                    f"aGate's Modbus session — point this one at a different "
                    f"device, or mark it as a mock.",
                )

    gw = await create_gateway(
        db,
        gateway_id=body.gateway_id,
        name=body.name,
        host=body.host or ("mock" if body.mock else ""),
        port=body.port,
        unit_id=body.unit_id,
        description=body.description,
        poll_interval=body.poll_interval,
        timeout=body.timeout,
        mock=body.mock,
        device_type=body.device_type,
    )

    # Onboard immediately so the gateway starts polling without an app restart.
    # start_gateway is non-blocking (connect/discover runs in a background
    # task); a failed connection just surfaces the gateway as offline rather
    # than failing the create.
    registry = getattr(request.app.state, "registry", None)
    if registry is not None:
        try:
            await registry.start_gateway(body.gateway_id)
        except Exception as exc:
            logger.warning(
                "Gateway %s created but failed to start: %s", body.gateway_id, exc
            )
    return gw


@router.get("/gateways/{gw_id}")
async def get_single_gateway(gw_id: str, request: Request):
    """Get a single gateway with live status."""
    db: aiosqlite.Connection = request.app.state.db
    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    entry = dict(row)
    registry = getattr(request.app.state, "registry", None)
    if registry:
        inst = registry.get(gw_id)
        if inst:
            entry.update(inst.to_dict())
    return entry


@router.patch("/gateways/{gw_id}")
async def patch_gateway(gw_id: str, body: GatewayUpdate, request: Request):
    """Update gateway configuration."""
    db: aiosqlite.Connection = request.app.state.db
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    if "publish_to_ha" in updates:
        updates["publish_to_ha"] = int(updates["publish_to_ha"])
    if not updates:
        row = await get_gateway(db, gw_id)
        if row is None:
            raise HTTPException(404, f"Gateway '{gw_id}' not found")
        return row

    result = await update_gateway(db, gw_id, **updates)
    if result is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    # Apply a phase-view change to the live publisher (default gateway only —
    # that's the one wired to the MQTT publisher today). Re-publishes discovery.
    if "phase_view" in updates and gw_id == "default":
        publisher = getattr(request.app.state, "mqtt_publisher", None)
        if publisher is not None:
            publisher.set_phase_view(updates["phase_view"])

    # Keep the site aggregator's per-phase bucketing in sync with the tag.
    if "phase" in updates:
        aggregator = getattr(request.app.state, "site_aggregator", None)
        if aggregator is not None:
            aggregator.set_gateway_phase(gw_id, updates["phase"])

    # Register/unregister this gateway's own HA device when the publish toggle
    # (or enabled) changes, so it takes effect without a restart. Turning it off
    # tombstones discovery, which removes the entities from HA instead of
    # leaving them behind as permanently unavailable.
    if "publish_to_ha" in updates or "enabled" in updates:
        sync = getattr(request.app.state, "sync_mqtt_devices", None)
        if sync is not None:
            try:
                await sync()
            except Exception as exc:  # never fail the edit over the side effect
                logger.warning("MQTT device sync after gateway edit failed: %s", exc)

    # Keep the running instance's service link in sync so the schedule engine's
    # 'service' fan-out (SCH3) sees the change without a restart.
    if "service_id" in updates:
        registry = getattr(request.app.state, "registry", None)
        inst = registry.get(gw_id) if registry else None
        if inst is not None:
            inst.config.service_id = updates["service_id"] or None

    return result


@router.delete("/gateways/{gw_id}")
async def remove_gateway(gw_id: str, request: Request):
    """Remove a gateway. Default gateway cannot be removed."""
    db: aiosqlite.Connection = request.app.state.db
    registry = _registry(request)

    # Stop instance if running
    if registry.get(gw_id):
        await registry.stop_gateway(gw_id)

    # Offboard: drop the gateway's cached samples so it no longer skews the
    # site aggregate after removal.
    aggregator = getattr(request.app.state, "site_aggregator", None)
    if aggregator is not None:
        aggregator.clear_gateway(gw_id)

    try:
        ok = await delete_gateway(db, gw_id)
    except ValueError as e:
        raise HTTPException(403, str(e)) from e
    if not ok:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")
    return {"deleted": True}


# ── Gateway lifecycle ─────────────────────────────────────────


@router.post("/gateways/{gw_id}/start")
async def start_gateway_endpoint(gw_id: str, request: Request):
    """Start polling a gateway."""
    db: aiosqlite.Connection = request.app.state.db
    registry = _registry(request)

    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    # Enable + clear any prior "user stopped" pause so it auto-starts on boot.
    await update_gateway(db, gw_id, enabled=1, autostart=1)

    inst = await registry.start_gateway(gw_id)
    if inst is None:
        raise HTTPException(500, f"Failed to start gateway '{gw_id}'")
    return {"started": True, "gateway_id": gw_id}


@router.post("/gateways/{gw_id}/stop")
async def stop_gateway_endpoint(gw_id: str, request: Request):
    """Stop polling a gateway and remember the stop across restarts.

    Sets ``autostart=0`` so the gateway is not re-started on the next app
    boot.  ``enabled`` stays 1 — this is a user pause, not an admin disable,
    so the gateway remains configured and can be started again.  This is what
    keeps a stopped (esp. mock) gateway from self-restarting on reboot.
    """
    db: aiosqlite.Connection = request.app.state.db
    registry = _registry(request)
    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    await update_gateway(db, gw_id, autostart=0)
    if registry.get(gw_id):
        await registry.stop_gateway(gw_id)
    return {"stopped": True, "gateway_id": gw_id}


@router.post("/gateways/{gw_id}/restart")
async def restart_gateway_endpoint(gw_id: str, request: Request):
    """Tear down and recreate a gateway's Modbus connection.

    Unlike /diagnose (read-only), this is the actual fix for a wedged
    session: it discards the current GatewayInstance (closing whatever
    socket state it had, good or bad) and reconnects from scratch, without
    needing a full bridge process restart. ``registry.start_gateway``
    already stops any existing instance for this ID before starting a new
    one, so this is mechanically identical to /start — this endpoint exists
    for a clear, explicit "restart" action in the UI/CLI regardless of
    whether the gateway looked like it was already running.
    """
    db: aiosqlite.Connection = request.app.state.db
    registry = _registry(request)

    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    await update_gateway(db, gw_id, enabled=1, autostart=1)
    inst = await registry.start_gateway(gw_id)
    if inst is None:
        raise HTTPException(500, f"Failed to restart gateway '{gw_id}'")
    return {"restarted": True, "gateway_id": gw_id}


@router.post("/gateways/{gw_id}/healthcheck")
async def force_healthcheck_endpoint(gw_id: str, request: Request):
    """Force an immediate TCP health probe, instead of waiting for the
    periodic HealthChecker (default: every 60s) to get to this gateway.

    Updates the same ``status.health`` field the topbar dot reads, so the
    UI reflects the result on its next refresh.
    """
    registry = _registry(request)
    if registry.get(gw_id) is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found or not running")

    health_checker = getattr(request.app.state, "health_checker", None)
    if health_checker is None:
        raise HTTPException(503, "Health checker not available")

    health = await health_checker.check_one(gw_id)
    return {"gateway_id": gw_id, "health": health}


@router.get("/gateways/{gw_id}/detect-phases")
async def detect_gateway_phases(gw_id: str, request: Request):
    """Auto-detect the gateway's wired phase(s) from its 701 registers.

    Reads the gateway's latest cached points (no extra Modbus traffic) and
    infers connected/utilised phases. Also reports whether the result matches
    the gateway's currently declared ``phase``.
    """
    db: aiosqlite.Connection = request.app.state.db
    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    # Prefer the gateway's own sample bus (carries the full 701.* per-phase set,
    # same source as /api/points); fall back to the site aggregator's cache.
    points: dict = {}
    registry = getattr(request.app.state, "registry", None)
    inst = registry.get(gw_id) if registry else None
    if inst is not None and inst.sample_bus.last_sample is not None:
        points = dict(inst.sample_bus.last_sample.points)
    else:
        aggregator = getattr(request.app.state, "site_aggregator", None)
        if aggregator is not None:
            points = aggregator.get_gateway_points(gw_id)
    result = detect_phases(points)
    declared = row.get("phase", "all")
    result["declared"] = declared
    result["matches_declared"] = phase_matches(declared, result["detected"])
    return result


# ── TCP connectivity test ─────────────────────────────────────


@router.post("/gateways/{gw_id}/test")
async def test_gateway_tcp(gw_id: str, request: Request):
    """Test TCP connectivity to a gateway's host:port."""
    db: aiosqlite.Connection = request.app.state.db
    row = await get_gateway(db, gw_id)
    if row is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found")

    # A mock gateway has no socket — a real TCP probe would time out against its
    # synthetic host. Report a synthetic OK instead.
    if row.get("mock"):
        return {
            "gateway_id": gw_id,
            "ok": True,
            "detail": "mock gateway — no TCP connection (synthetic)",
            "latency_ms": 0,
        }

    result = await tcp_probe(row["host"], row["port"])
    return {"gateway_id": gw_id, **result.to_dict()}


@router.post("/gateways/{gw_id}/diagnose")
async def diagnose_gateway(gw_id: str, request: Request):
    """Root-cause a Modbus connectivity problem for a gateway.

    Runs TCP reachability probes (Modbus port + Local API port), a live
    Modbus protocol-level read reusing the poller's existing session (never
    opens a second concurrent Modbus session — see docs/vendor-issues.md on
    concurrent-access corruption), and cross-checks the result against the
    poller's own reported state to catch cases where the poller thinks it's
    fine but isn't.
    """
    registry = getattr(request.app.state, "registry", None)
    inst = registry.get(gw_id) if registry else None
    if inst is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not found or not running")

    # A mock has no socket/Modbus session to diagnose — report it synthetically.
    if inst.config.mock:
        polling = inst.status.polling
        return {
            "gateway_id": gw_id,
            "mock": True,
            "overall": "ok" if polling else "degraded",
            "summary": (
                "Mock gateway — synthetic samples, no Modbus/TCP. "
                + ("Poller running." if polling else "Poller not running.")
            ),
            "checks": [],
        }

    log_buffer = getattr(request.app.state, "log_buffer", None)
    return await diagnostics_mod.run_diagnostics(inst, log_buffer=log_buffer)


# ── Gateway-scoped endpoints ─────────────────────────────────


@router.get("/gateways/{gw_id}/points")
async def get_gateway_points(gw_id: str, request: Request):
    """Get current points for a specific gateway."""
    registry = _registry(request)
    inst = registry.get(gw_id)
    if inst is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not running")

    last = inst.sample_bus.last_sample
    if last is None:
        return {"gateway_id": gw_id, "points": {}, "ts": None}

    points = dict(last.points)
    if inst.command_handler:
        points.update(inst.command_handler.virtual_points)
    return {
        "gateway_id": gw_id,
        "points": points,
        "ts": last.ts,
        "quality": last.quality,
    }


class GatewayCommandRequest(BaseModel):
    slug: str
    value: str


@router.post("/gateways/{gw_id}/command")
async def send_gateway_command(
    gw_id: str, body: GatewayCommandRequest, request: Request,
):
    """Dispatch a control command to a specific gateway."""
    registry = _registry(request)
    inst = registry.get(gw_id)
    if inst is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not running")
    if inst.command_handler is None:
        return {
            "ok": False, "slug": body.slug,
            "result": "Command handler not available",
        }

    await inst.command_handler.handle_command(body.slug, body.value)
    return {
        "ok": inst.command_handler.state.last_success,
        "slug": body.slug,
        "gateway_id": gw_id,
        "result": inst.command_handler.state.last_result or "Sent",
    }


@router.get("/gateways/{gw_id}/battery/limits")
async def get_gateway_battery_limits(gw_id: str, request: Request):
    """Return battery power limits for a specific gateway."""
    registry = _registry(request)
    inst = registry.get(gw_id)
    if inst is None:
        raise HTTPException(404, f"Gateway '{gw_id}' not running")
    if inst.command_handler is None:
        return {
            "max_charge_w": DEFAULT_MAX_POWER_W,
            "max_discharge_w": DEFAULT_MAX_POWER_W,
            "source": "default",
        }
    return inst.command_handler.power_limits
