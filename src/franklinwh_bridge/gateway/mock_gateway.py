"""Mock gateway — synthetic data source for multi-gateway demo/testing.

A mock gateway emits simulated ``Sample`` records on its poll interval
WITHOUT opening any Modbus connection.  It therefore never contends for a
real aGate's single Modbus session and gets a synthetic, collision-free
serial — making it safe to run alongside the real gateway purely to exercise
the multi-gateway selector and Site aggregation in the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time

from franklinwh_bridge.modbus.sample import Sample, SampleBus

logger = logging.getLogger(__name__)


def mock_serial(gateway_id: str) -> str:
    """Synthetic, collision-free serial for a mock gateway."""
    return f"MOCK-{gateway_id.upper()}"


class MockController:
    """Stand-in controller for a mock gateway — no real Modbus I/O."""

    def __init__(self, gateway_id: str) -> None:
        self.gateway_id = gateway_id

    @property
    def serial(self) -> str:
        return mock_serial(self.gateway_id)

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def read_nameplate(self) -> dict:
        return {
            "serial": self.serial,
            "model": "aGate (mock)",
            "manufacturer": "FranklinWH (mock)",
            "version": "MOCK",
        }


def synthetic_points(gateway_id: str, tick: int, ts: float | None = None) -> dict:
    """Build one synthetic sample for *gateway_id* at *tick*.

    Uses wall-clock time-of-day to drive realistic diurnal patterns:
    solar follows a daytime bell curve, home load peaks at breakfast and
    dinner, battery charges on solar surplus and discharges in the evening.
    Per-gateway seed shifts capacity/timing so multiple mocks look distinct.
    """
    seed = sum(ord(c) for c in gateway_id) or 1

    # Per-gateway capacity/timing variations
    solar_peak_kw  = 3.5 + 2.5 * ((seed * 7)  % 100) / 100.0   # 3.5–6 kW peak solar
    home_base_w    = 400 + 400 * ((seed * 17) % 100) / 100.0    # 400–800 W base load
    batt_cap_kw    = 2.0 + 1.5 * ((seed * 13) % 100) / 100.0   # 2–3.5 kW battery limit
    soc_morning    = 25.0 + 20.0 * ((seed * 11) % 100) / 100.0  # morning SOC 25–45%

    # Wall-clock fractional hour (UTC; good enough for demo patterns)
    now_ts = ts if ts is not None else time.time()
    day_sec = now_ts % 86400
    hour = day_sec / 3600.0

    # ── Solar: smooth bell curve, zero before dawn / after dusk ──
    # Half-sine from 6 h to 20 h; width per-gateway slightly varies
    dawn, dusk = 6.0, 20.0
    solar_factor = max(0.0, math.sin((hour - dawn) * math.pi / (dusk - dawn)))
    # Small cloud-cover ripple (slow tick-based noise, NOT a sine rollercoster)
    cloud_noise = 0.88 + 0.12 * math.sin(tick / 15.0 + seed * 0.7)
    solar_w = round(solar_peak_kw * 1000 * solar_factor ** 1.3 * cloud_noise, 1)

    # ── Home load: base + gaussian morning (8h) + evening (19h) peaks ──
    morning_peak = 900 * math.exp(-((hour - 8.0) ** 2) / 1.5)
    evening_peak = 1400 * math.exp(-((hour - 19.0) ** 2) / 3.5)
    load_jitter  = 1.0 + 0.04 * math.sin(tick / 4.0 + seed * 1.3)
    home_w = round((home_base_w + morning_peak + evening_peak) * load_jitter, 1)

    # ── Battery: absorb solar surplus during day, discharge in evening ──
    # net_surplus > 0 → charge; < 0 → discharge
    net_surplus = solar_w - home_w
    raw_batt = -net_surplus * 0.75   # negative = charging (convention)
    battery_w = round(max(-batt_cap_kw * 1000, min(batt_cap_kw * 1000, raw_batt)), 1)

    # ── SOC: integrate battery power across the day from a morning base ──
    # Approximate: morning low → afternoon high (solar charging) → evening low
    soc_noon_peak = min(95.0, soc_morning + 55.0)
    soc_curve = soc_morning + (soc_noon_peak - soc_morning) * math.sin(
        max(0.0, (hour - 6.0) * math.pi / 14.0)
    ) ** 0.7
    soc_jitter = 0.3 * math.sin(tick / 25.0 + seed * 0.5)
    soc = round(max(5.0, min(98.0, soc_curve + soc_jitter)), 1)

    grid_w = round(home_w - solar_w - battery_w, 1)

    if battery_w > 80:
        bstate = "Discharging"
    elif battery_w < -80:
        bstate = "Charging"
    else:
        bstate = "Standby"

    # ── Temperatures: vary with solar heat and battery activity ──
    temp_base_amb = 18 + (seed % 8)
    temp_base_cab = 28 + (seed % 12)
    heat_factor   = solar_factor * 0.5 + abs(battery_w) / 8000.0
    temp_drift    = math.sin(tick / 40.0 + seed * 0.4) * 1.5
    ambient_temp  = round(temp_base_amb + 4.0 * solar_factor + temp_drift, 1)
    cabinet_temp  = round(temp_base_cab + 6.0 * heat_factor + temp_drift, 1)

    # ── Operating mode: stable blocks (Self-Consumption day, TOU evening) ──
    if hour >= 22.0 or hour < 6.0:
        mode_name = "Emergency Backup"   # overnight reserve mode
    elif hour >= 16.0:
        mode_name = "TOU"                # evening peak tariff
    else:
        mode_name = "Self-Consumption"   # daytime self-use

    # Grid mode: Forming only when island/backup mode active, rare otherwise
    grid_mode = "Grid Forming" if mode_name == "Emergency Backup" else "Grid Following"

    return {
        "soc":                soc,
        "battery_power_w":    battery_w,
        "battery_dc_power_w": battery_w,
        "grid_power_w":       grid_w,
        "total_solar":        solar_w,
        "home_load_ext":      home_w,
        "battery_state":      bstate,
        "connection_state":   "Connected",
        "inverter_state":     "Running",
        "mode_name":          mode_name,
        "grid_mode":          grid_mode,
        "ambient_temp_c":     ambient_temp,
        "cabinet_temp_c":     cabinet_temp,
    }


class _MockPollerState:
    """Minimal stand-in for ModbusPoller.state (read by to_dict/list)."""

    last_poll_ts: float | None = None


class MockPoller:
    """Publishes synthetic samples to *sample_bus* every *poll_interval* s."""

    def __init__(
        self, sample_bus: SampleBus, gateway_id: str, poll_interval: int
    ) -> None:
        self._bus = sample_bus
        self._gateway_id = gateway_id
        self._interval = max(1, poll_interval)
        self._task: asyncio.Task | None = None
        self._tick = 0
        self.ac_type = 0
        self.state = _MockPollerState()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            pts = synthetic_points(self._gateway_id, self._tick)
            self._tick += 1
            self.state.last_poll_ts = time.time()
            await self._bus.publish(Sample.now(self._gateway_id, pts))
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
