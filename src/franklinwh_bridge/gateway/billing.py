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


class BillingStore:
    def __init__(self, db: Any) -> None:
        self._db = db
        self._demand: list[dict] = []
        self._bonus: list[dict] = []

    async def load(self) -> None:
        try:
            services = await get_services(self._db)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("BillingStore load failed: %s", exc)
            return
        demand, bonus = [], []
        for s in services:
            if s.get("has_peak_demand") and isinstance(s.get("demand_window"), dict):
                demand.append(s["demand_window"])
            if s.get("has_export_bonus") and isinstance(s.get("bonus_window"), dict):
                bonus.append(s["bonus_window"])
        self._demand, self._bonus = demand, bonus

    def as_points(self) -> dict[str, list[dict]]:
        return {
            "tariff_demand_windows": self._demand,
            "tariff_bonus_windows": self._bonus,
        }
