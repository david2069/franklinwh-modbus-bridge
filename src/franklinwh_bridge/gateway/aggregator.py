"""Site aggregator — computes virtual site-level points from all gateways.

Subscribes to the global ``SampleBus`` and accumulates the latest points
per gateway.  The aggregated points (total solar, total battery, average
SoC, etc.) are available via ``site_points`` for the dashboard and can
be published as site-level MQTT entities in a future phase.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from franklinwh_bridge.modbus.sample import Sample

logger = logging.getLogger(__name__)


class SiteAggregator:
    """Computes aggregated site-level metrics from per-gateway samples."""

    def __init__(self) -> None:
        # Latest points per gateway: {gateway_id: {key: value, ...}}
        self._latest: dict[str, dict] = {}
        self._last_update: float = 0

    async def on_sample(self, sample: Sample) -> None:
        """SampleBus subscriber callback — stores latest per-gateway points."""
        self._latest[sample.gateway_id] = dict(sample.points)
        self._last_update = time.time()

    @property
    def gateway_count(self) -> int:
        return len(self._latest)

    @property
    def site_points(self) -> dict:
        """Compute aggregated site-level virtual points.

        Sums power values across gateways, averages SoC, and provides
        min/max SoC.  Returns an empty dict if no gateway data is
        available.
        """
        if not self._latest:
            return {}

        # Power keys to sum across gateways
        sum_keys = {
            "total_solar": "site_total_solar_w",
            "battery_power_w": "site_battery_power_w",
            "grid_power_w": "site_grid_power_w",
            "home_load_ext": "site_home_load_w",
        }

        result: dict = {}
        for src_key, dest_key in sum_keys.items():
            total = 0.0
            has_value = False
            for pts in self._latest.values():
                val = pts.get(src_key)
                if val is not None:
                    total += val
                    has_value = True
            if has_value:
                result[dest_key] = round(total, 1)

        # SoC: average, min, max across gateways
        socs = [
            pts["soc"]
            for pts in self._latest.values()
            if pts.get("soc") is not None
        ]
        if socs:
            result["site_soc_avg"] = round(sum(socs) / len(socs), 1)
            result["site_soc_min"] = min(socs)
            result["site_soc_max"] = max(socs)

        # Off-grid detection: True if any gateway is islanded
        for pts in self._latest.values():
            conn_st = pts.get("connection_state")
            if conn_st and "disconnect" in str(conn_st).lower():
                result["site_is_off_grid"] = True
                break
        else:
            result["site_is_off_grid"] = False

        result["site_gateway_count"] = len(self._latest)
        result["site_last_update"] = self._last_update
        return result

    def get_gateway_points(self, gateway_id: str) -> dict:
        """Return the latest cached points for a specific gateway."""
        return dict(self._latest.get(gateway_id, {}))

    def clear_gateway(self, gateway_id: str) -> None:
        """Remove cached data for a gateway (called on offboard)."""
        self._latest.pop(gateway_id, None)
