"""Energy-Costs / Tariff tab — one aggregating read endpoint.

Phase B of the tariff module. Surfaces, for the dedicated tab, the three things
the Settings card can't show well:

- **Setup** — the resolved demand / export-reward / export-charge configuration
  (windows, rates, free allowance, billing cycle) per the Phase A engine, plus a
  per-service flag summary so the user knows what's on and where to edit it.
- **This period (live)** — the current billing-period ``tariff.*`` / ``demand.*``
  / ``bonus.*`` / ``fixed.*`` sensor values (peak kW + charge, reward kWh +
  credit, export charge kWh + net-above-free + cost, free-allowance remaining,
  standing charges accrued, days elapsed).
- **Linked automations** — which schedule entries reference any tariff/demand
  sensor, reusing the ``_collect_sensor_refs`` scanner from the import validator.

Purely a read view over already-computed state — no new persistence. Editing
still happens on the Settings service form (Phase A); reporting/history is
Phase C.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from franklinwh_bridge.api.auth import require_capability
from franklinwh_bridge.api.scheduler_api import _gateway_points
from franklinwh_bridge.api.schedules_api import _collect_sensor_refs, _decorate
from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog
from franklinwh_bridge.store.db import (
    correct_plan_start,
    get_billing_periods,
    get_schedules,
    get_service,
    get_service_plans,
    get_services,
    plans_overlapping,
    record_plan_change,
)

router = APIRouter(prefix="/api", tags=["tariff"])

# Hoisted: Depends() in an argument default is evaluated at import, and building
# it inline trips ruff B008.
_REQUIRE_CONTROL = Depends(require_capability("control"))

#: Sensor-id prefixes this tab owns.
_TARIFF_PREFIXES = ("tariff.", "demand.", "bonus.", "fixed.")


def _is_tariff(sensor_id: str) -> bool:
    return sensor_id.startswith(_TARIFF_PREFIXES)


@router.get("/tariff/overview")
async def tariff_overview(request: Request, gateway: str = "default"):
    """Setup config + live period values + linked automations for the tab."""
    db: aiosqlite.Connection = request.app.state.db
    billing = getattr(request.app.state, "billing", None)

    # ── Setup: resolved engine config + per-service flag summary ──
    demand_cfg = billing.demand_config() if billing else None
    bonus_cfg = billing.bonus_config() if billing else None
    charge_cfg = billing.charge_config() if billing else None

    services = []
    for s in await get_services(db):
        pricing = s.get("pricing") if isinstance(s.get("pricing"), dict) else {}
        services.append(
            {
                "id": s.get("id"),
                "name": s.get("name") or s.get("id"),
                "has_tou": bool(s.get("has_tou")),
                "has_peak_demand": bool(s.get("has_peak_demand")),
                "has_export_bonus": bool(s.get("has_export_bonus")),
                "has_export_charge": bool(pricing.get("export_charge")),
                "billing_cycle_day": pricing.get("billing_cycle_day"),
            }
        )

    # ── This period (live): tariff/demand/bonus sensors with values ──
    points = _gateway_points(request, gateway)
    live = [c for c in sensor_catalog(points) if _is_tariff(c["id"])]

    # Period progress (days elapsed of the full billing-period length) so the tab
    # can linearly project end-of-period totals. Raw points from the tracker.
    period = {
        "days_elapsed": points.get("demand_days_in_period"),
        "days_total": points.get("demand_period_days"),
    }

    # ── Linked automations: schedules touching any tariff sensor ──
    linked = []
    for entry in await get_schedules(db):
        refs = _collect_sensor_refs(entry.get("entry_conditions")) | _collect_sensor_refs(
            entry.get("exit_conditions")
        )
        tariff_refs = sorted(r for r in refs if _is_tariff(r))
        if not tariff_refs:
            continue
        dec = _decorate(entry, datetime.now())
        linked.append(
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "enabled": bool(entry.get("enabled")),
                "active_now": bool(dec.get("active_now")),
                "action": entry.get("action"),
                "sensors": tariff_refs,
            }
        )

    return {
        "gateway": gateway,
        "config": {
            "demand": demand_cfg,
            "bonus": bonus_cfg,
            "charge": charge_cfg,
        },
        "services": services,
        "period": period,
        "live": live,
        "linked": linked,
    }


#: History columns (order is the CSV column order too).
_HISTORY_COLS = (
    "period_start", "period_end", "demand_peak_kw", "demand_charge",
    "reward_kwh", "reward_credit", "charge_kwh", "charge_net_kwh",
    "charge_cost", "fixed_charges", "net_total",
)


async def _default_service_id(db: aiosqlite.Connection) -> str | None:
    """The service plan history is read against when none is named."""
    services = await get_services(db)
    return services[0]["id"] if services else None


async def _annotate_plans(
    db: aiosqlite.Connection, service_id: str | None, periods: list[dict]
) -> list[dict]:
    """Tag each period with the tariff plan(s) in force during it.

    A period that spans a retailer switch was snapshotted ONCE, by whichever
    plan was current when it closed — so its figures attribute the whole period
    to one retailer. That is the defect migration 45 exists to expose, and the
    honest thing for this endpoint to do is say so per row rather than present a
    single-retailer total as though it were unambiguous. ``spans_switch`` is what
    the UI puts a warning marker on.
    """
    if not service_id:
        return periods

    out = []
    for p in periods:
        plans = await plans_overlapping(db, service_id, p["period_start"], p["period_end"])
        out.append(
            p
            | {
                "plans": [
                    {
                        "id": pl["id"],
                        "retailer": pl["retailer"],
                        "network": pl["network"],
                        "valid_from": pl["valid_from"],
                        "valid_to": pl["valid_to"],
                    }
                    for pl in plans
                ],
                "spans_switch": len(plans) > 1,
            }
        )
    return out


@router.get("/tariff/history")
async def tariff_history(
    request: Request,
    gateway: str = "default",
    limit: int = 36,
    service: str | None = None,
):
    """Closed billing periods (reporting/history), most-recent first.

    Each period carries the plan(s) in force during it, so a period that
    straddles a retailer switch can be flagged rather than silently reported
    under one retailer's name.
    """
    db: aiosqlite.Connection = request.app.state.db
    periods = await get_billing_periods(db, gateway, max(1, min(limit, 240)))
    service_id = service or await _default_service_id(db)
    return {
        "gateway": gateway,
        "service": service_id,
        "periods": await _annotate_plans(db, service_id, periods),
    }


# ── Plan history (who billed you, and when) ───────────────────


class PlanChange(BaseModel):
    """A retailer/plan switch, recorded at the date it actually happened.

    ``valid_from`` is almost always in the past: a switch is discovered after
    the fact, and stamping it "now" is what made the owner's own Amber → AGL
    change on 09 Sep unrepresentable.
    """

    valid_from: float = Field(gt=0, description="epoch seconds; may be in the past")
    retailer: str | None = None
    network: str | None = None
    plan_type: str | None = None
    pricing: str | dict | None = None
    note: str = ""


class PlanStartCorrection(BaseModel):
    """Move a plan's start date without creating a new plan."""

    valid_from: float = Field(gt=0)


@router.get("/tariff/plans")
async def tariff_plans(request: Request, service: str | None = None):
    """Plan history for a service, oldest first."""
    db: aiosqlite.Connection = request.app.state.db
    service_id = service or await _default_service_id(db)
    if not service_id:
        return {"service": None, "plans": []}
    if await get_service(db, service_id) is None:
        raise HTTPException(404, f"No such service '{service_id}'")
    return {"service": service_id, "plans": await get_service_plans(db, service_id)}


@router.post("/tariff/plans")
async def create_tariff_plan(
    request: Request,
    body: PlanChange,
    service: str | None = None,
    _user: dict = _REQUIRE_CONTROL,
):
    """Record a plan change, closing the plan that was in force at that date.

    Inserting *between* two existing plans is supported — that is what recording
    history you did not capture at the time means.
    """
    db: aiosqlite.Connection = request.app.state.db
    service_id = service or await _default_service_id(db)
    if not service_id:
        raise HTTPException(400, "No service to record a plan against")

    try:
        plan = await record_plan_change(
            db, service_id, **body.model_dump(exclude_none=True)
        )
    except ValueError as exc:
        # Overlapping/zero-length ranges are refused by the store rather than
        # written and left for the resolver to pick arbitrarily between.
        raise HTTPException(400, str(exc)) from None

    if plan is None:
        raise HTTPException(404, f"No such service '{service_id}'")
    return {"service": service_id, "plan": plan}


@router.patch("/tariff/plans/{plan_id}")
async def patch_tariff_plan(
    request: Request,
    plan_id: str,
    body: PlanStartCorrection,
    _user: dict = _REQUIRE_CONTROL,
):
    """Correct a plan's start date.

    Needed because a backfilled plan starts at the placeholder migration 45 had
    nothing better to use, leaving no earlier instant at which to insert a
    predecessor. The real start has to be settable *first*, then the earlier
    retailer can be recorded before it.
    """
    db: aiosqlite.Connection = request.app.state.db
    try:
        plan = await correct_plan_start(db, plan_id, body.valid_from)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if plan is None:
        raise HTTPException(404, f"No such plan '{plan_id}'")
    return {"plan": plan}


@router.get("/tariff/history.csv")
async def tariff_history_csv(request: Request, gateway: str = "default"):
    """Closed billing periods as a CSV download (chronological)."""
    db: aiosqlite.Connection = request.app.state.db
    periods = await get_billing_periods(db, gateway, 240)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(("period_start_iso", "period_end_iso", *_HISTORY_COLS[2:]))
    for p in reversed(periods):  # oldest → newest for a report
        writer.writerow(
            (
                datetime.fromtimestamp(p["period_start"]).date().isoformat(),
                datetime.fromtimestamp(p["period_end"]).date().isoformat(),
                *(p.get(c) for c in _HISTORY_COLS[2:]),
            )
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="tariff-history-{gateway}.csv"'},
    )
