"""Energy-Costs / Tariff tab — one aggregating read endpoint.

Phase B of the tariff module. Surfaces, for the dedicated tab, the three things
the Settings card can't show well:

- **Setup** — the resolved demand / export-reward / export-charge configuration
  (windows, rates, free allowance, billing cycle) per the Phase A engine, plus a
  per-service flag summary so the user knows what's on and where to edit it.
- **This period (live)** — the current billing-period ``tariff.*`` / ``demand.*``
  / ``bonus.*`` sensor values (peak kW + charge, reward kWh + credit, export
  charge kWh + net-above-free + cost, free-allowance remaining, days elapsed).
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
from fastapi import APIRouter, Request
from fastapi.responses import Response

from franklinwh_bridge.api.scheduler_api import _gateway_points
from franklinwh_bridge.api.schedules_api import _collect_sensor_refs, _decorate
from franklinwh_bridge.gateway.scheduler_sensors import sensor_catalog
from franklinwh_bridge.store.db import get_billing_periods, get_schedules, get_services

router = APIRouter(prefix="/api", tags=["tariff"])

#: Sensor-id prefixes this tab owns.
_TARIFF_PREFIXES = ("tariff.", "demand.", "bonus.")


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
    "charge_cost", "net_total",
)


@router.get("/tariff/history")
async def tariff_history(request: Request, gateway: str = "default", limit: int = 36):
    """Closed billing periods (reporting/history), most-recent first."""
    db: aiosqlite.Connection = request.app.state.db
    periods = await get_billing_periods(db, gateway, max(1, min(limit, 240)))
    return {"gateway": gateway, "periods": periods}


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
