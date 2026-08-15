"""Utility-service billing windows → automation sensors.

Caches the demand + battery-bonus tariff windows across all services (where the
matching flag is set) so the sync points-merge can expose them as
``tariff_demand_windows`` / ``tariff_bonus_windows`` — the ``demand.window_active``
and ``bonus.window_active`` sensors evaluate ``now`` against these. Reloaded on
service CRUD (see gateways_api) and at startup.
"""

from __future__ import annotations

import logging
from typing import Any

from franklinwh_bridge.store.db import get_services

logger = logging.getLogger(__name__)


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class BillingStore:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._demand: list[dict] = []
        self._bonus: list[dict] = []
        # First service with each flag drives the demand/bonus calculator (v1).
        self._demand_cfg: dict | None = None
        self._bonus_cfg: dict | None = None

    async def load(self) -> None:
        try:
            services = await get_services(self._db)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("BillingStore load failed: %s", exc)
            return
        demand, bonus = [], []
        demand_cfg = bonus_cfg = None
        for s in services:
            pricing = s.get("pricing") if isinstance(s.get("pricing"), dict) else {}
            cycle_day = int(_num(pricing.get("billing_cycle_day"), 1)) or 1
            if s.get("has_peak_demand") and isinstance(s.get("demand_window"), dict):
                demand.append(s["demand_window"])
                if demand_cfg is None:
                    interval = int(_num(pricing.get("demand_interval_min"), 30)) or 30
                    basis = pricing.get("demand_charge_basis") or "per_kw_day"
                    if basis not in ("per_kw_day", "flat_per_kw"):
                        basis = "per_kw_day"
                    demand_cfg = {
                        "window": s["demand_window"],
                        "rate": _num(pricing.get("demand_rate")),  # $/kW/day (or flat $/kW)
                        "cycle_day": cycle_day,
                        "interval_min": interval if interval in (15, 30, 60) else 30,
                        "charge_basis": basis,
                    }
            if s.get("has_export_bonus") and isinstance(s.get("bonus_window"), dict):
                bonus.append(s["bonus_window"])
                if bonus_cfg is None:
                    bonus_cfg = {
                        "window": s["bonus_window"],
                        "rate": _num(pricing.get("export_bonus_rate")),  # $/kWh
                        "cycle_day": cycle_day,
                    }
        self._demand, self._bonus = demand, bonus
        self._demand_cfg, self._bonus_cfg = demand_cfg, bonus_cfg

    def demand_config(self) -> dict | None:
        return self._demand_cfg

    def bonus_config(self) -> dict | None:
        return self._bonus_cfg

    def as_points(self) -> dict[str, Any]:
        d = self._demand_cfg or {}
        return {
            "tariff_demand_windows": self._demand,
            "tariff_bonus_windows": self._bonus,
            "tariff_demand_rate": d.get("rate", 0.0),
            "tariff_export_bonus_rate": (self._bonus_cfg or {}).get("rate", 0.0),
            "tariff_demand_charge_basis": d.get("charge_basis", "per_kw_day"),
        }
