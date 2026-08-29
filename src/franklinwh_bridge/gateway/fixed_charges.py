"""Fixed / periodic standing charges → informational accrual sensors.

Standing charges (daily supply, metering, membership/subscription, connection
fees, …) are levied on a fixed cadence regardless of usage. They're predictable
but can be significant, and a target for export revenue to offset — "I've accrued
$X in standing charges with Y days left in the cycle, so export more to cover
it." This store reads them from each service's ``pricing`` JSON and exposes the
amount accrued so far this billing period as ``fixed.*`` sensors an automation
can condition on.

Charges live in ``pricing.fixed_charges`` — a list of
``{type, description, levied_by, frequency, rate, tax_rate}``. Accrual is
deterministic (no meter data): each charge is normalised to a per-day equivalent
aligned to the billing period, then multiplied by days elapsed. Reported values
are tax-inclusive (``rate × (1 + tax_rate)``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from franklinwh_bridge.gateway.demand import _period_end, _period_start
from franklinwh_bridge.store.db import get_services

logger = logging.getLogger(__name__)

#: Accepted cadences. monthly/quarterly/annual are billing-period-aligned so a
#: monthly charge accrues to exactly ``rate`` over one billing cycle.
FREQUENCIES = ("daily", "weekly", "monthly", "quarterly", "annual")


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _daily_equiv(freq: str, rate: float, days_in_period: float) -> float:
    """Per-day share of one charge, aligned to the billing period for the
    period-based cadences so each period gets its correct fraction."""
    dip = days_in_period or 30.0
    if freq == "daily":
        return rate
    if freq == "weekly":
        return rate / 7.0
    if freq == "monthly":
        return rate / dip
    if freq == "quarterly":
        return rate / (dip * 3.0)
    if freq == "annual":
        return rate / (dip * 12.0)
    return 0.0


class FixedChargesStore:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._charges: list[dict] = []
        self._cycle_day = 1

    async def load(self) -> None:
        try:
            services = await get_services(self._db)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("FixedChargesStore load failed: %s", exc)
            return
        charges: list[dict] = []
        cycle_day = 1
        for s in services:
            pricing = s.get("pricing") if isinstance(s.get("pricing"), dict) else {}
            cd = int(_num(pricing.get("billing_cycle_day"), 1)) or 1
            cycle_day = cd  # last service with a cycle day wins (single-service v1)
            for c in pricing.get("fixed_charges") or []:
                if not isinstance(c, dict):
                    continue
                freq = c.get("frequency") if c.get("frequency") in FREQUENCIES else "daily"
                charges.append(
                    {
                        "type": c.get("type") or "charge",
                        "description": c.get("description") or "",
                        "levied_by": c.get("levied_by") or "utility",
                        "frequency": freq,
                        "rate": _num(c.get("rate")),
                        "tax_rate": _num(c.get("tax_rate")),
                    }
                )
        self._charges = charges
        self._cycle_day = cycle_day

    def charges(self) -> list[dict]:
        return list(self._charges)

    def as_points(self, now: datetime | None = None) -> dict[str, float]:
        """Accrual outputs merged into the sensor snapshot → ``fixed.*`` sensors."""
        now = now or datetime.now()
        ps = _period_start(now, self._cycle_day)
        pe = _period_end(ps, self._cycle_day)
        days_in_period = round((pe - ps).total_seconds() / 86400.0, 3) or 30.0
        days_elapsed = round(max(0.0, (now - ps).total_seconds() / 86400.0), 3)
        days_elapsed = min(days_elapsed, days_in_period)

        daily = 0.0
        for c in self._charges:
            eq = _daily_equiv(c["frequency"], c["rate"], days_in_period)
            daily += eq * (1.0 + c["tax_rate"])  # tax-inclusive

        daily = round(daily, 4)
        period_total = round(daily * days_in_period, 2)
        accrued = round(daily * days_elapsed, 2)
        return {
            "fixed_daily_charge": daily,
            "fixed_accrued_period": accrued,
            "fixed_period_total": period_total,
            "fixed_remaining": round(max(0.0, period_total - accrued), 2),
            "fixed_days_remaining": round(max(0.0, days_in_period - days_elapsed), 3),
        }
