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

    # Power keys bucketed into per-phase site totals (canonical → site alias).
    _PHASE_SUM_KEYS = {
        "total_solar": "site_solar",
        "battery_power_w": "site_battery_power",
        "grid_power_w": "site_grid_power",
        "home_load_ext": "site_home_load",
    }
    _PHASES = ("L1", "L2", "L3")

    def __init__(self) -> None:
        # Latest points per gateway: {gateway_id: {key: value, ...}}
        self._latest: dict[str, dict] = {}
        # Gateway → assigned phase tag ('all' | 'L1' | 'L2' | 'L3' | combo).
        self._phases: dict[str, str] = {}
        self._last_update: float = 0

    def set_gateway_phase(self, gateway_id: str, phase: str | None) -> None:
        """Record a gateway's assigned phase for per-phase site aggregation."""
        self._phases[gateway_id] = phase or "all"

    async def on_sample(self, sample: Sample) -> None:
        """SampleBus subscriber callback — stores latest per-gateway points."""
        self._latest[sample.gateway_id] = dict(sample.points)
        self._last_update = time.time()

    @property
    def gateway_count(self) -> int:
        return len(self._latest)

    @property
    def last_update(self) -> float:
        """Unix timestamp of the most recent sample from any gateway."""
        return self._last_update

    @property
    def site_points(self) -> dict:
        """Compute aggregated site-level virtual points.

        Sums power values across gateways, averages SoC, and provides
        min/max SoC.  Returns an empty dict if no gateway data is
        available.

        Each aggregate is emitted under BOTH a ``site_*`` alias (for future
        site-level MQTT entities) AND the canonical point key the dashboard
        already binds to (``total_solar``, ``battery_power_w``, ``grid_power_w``,
        ``home_load_ext``, ``soc`` …), so selecting "Site" in the gateway
        dropdown renders the standard dashboard cards directly.  Per-device
        electrical detail (voltage, frequency, SoH, temperatures) is not
        aggregatable and is intentionally left absent.
        """
        if not self._latest:
            return {}

        # Source key -> (site alias, canonical dashboard key)
        sum_keys = {
            "total_solar": ("site_total_solar_w", "total_solar"),
            "battery_power_w": ("site_battery_power_w", "battery_power_w"),
            "grid_power_w": ("site_grid_power_w", "grid_power_w"),
            "home_load_ext": ("site_home_load_w", "home_load_ext"),
        }

        result: dict = {}
        for src_key, (alias_key, canon_key) in sum_keys.items():
            total = 0.0
            has_value = False
            for pts in self._latest.values():
                val = pts.get(src_key)
                if val is not None:
                    total += val
                    has_value = True
            if has_value:
                total = round(total, 1)
                result[alias_key] = total
                result[canon_key] = total

        # Battery: expose the DC-power alias the battery card reads and derive
        # an aggregate state from the summed power (positive = discharging,
        # matching the per-gateway convention) with a 50 W deadband.
        if "battery_power_w" in result:
            bp = result["battery_power_w"]
            result["battery_dc_power_w"] = bp
            if bp > 50:
                result["battery_state"] = "Discharging"
            elif bp < -50:
                result["battery_state"] = "Charging"
            else:
                result["battery_state"] = "Standby"

        # SoC: average, min, max across gateways
        socs = [
            pts["soc"]
            for pts in self._latest.values()
            if pts.get("soc") is not None
        ]
        if socs:
            avg = round(sum(socs) / len(socs), 1)
            result["site_soc_avg"] = avg
            result["site_soc_min"] = min(socs)
            result["site_soc_max"] = max(socs)
            result["soc"] = avg  # canonical key for the SoC ring + live points

        # Off-grid detection: True if any gateway is islanded
        off_grid = any(
            (cs := pts.get("connection_state")) and "disconnect" in str(cs).lower()
            for pts in self._latest.values()
        )
        result["site_is_off_grid"] = off_grid
        # Canonical grid indicator for the topbar dot.
        result["connection_state"] = "Disconnected" if off_grid else "Connected"

        # ── Per-phase site totals (Topology A) ────────────────────
        # Each gateway's *canonical* aggregate power is bucketed into the leg
        # it's tagged with. Only gateways pinned to a single phase (L1/L2/L3)
        # contribute; an 'all'/combo unit spans legs and stays in the grand
        # total only. (A single-phase aGate reports its data in its own L1
        # slot regardless of the service leg, so we bucket the canonical
        # aggregate by the user's tag — not by reading 701.WLn.)
        for ph in self._PHASES:
            for src_key, alias in self._PHASE_SUM_KEYS.items():
                total = 0.0
                has_value = False
                for gw_id, pts in self._latest.items():
                    if self._phases.get(gw_id, "all") != ph:
                        continue
                    val = pts.get(src_key)
                    if val is not None:
                        total += val
                        has_value = True
                if has_value:
                    result[f"{alias}_{ph}"] = round(total, 1)

        result["site_gateway_count"] = len(self._latest)
        result["site_last_update"] = self._last_update
        return result

    def get_gateway_points(self, gateway_id: str) -> dict:
        """Return the latest cached points for a specific gateway."""
        return dict(self._latest.get(gateway_id, {}))

    def clear_gateway(self, gateway_id: str) -> None:
        """Remove cached data for a gateway (called on offboard)."""
        self._latest.pop(gateway_id, None)
        self._phases.pop(gateway_id, None)
