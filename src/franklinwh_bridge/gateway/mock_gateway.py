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


def synthetic_points(gateway_id: str, tick: int) -> dict:
    """Build one synthetic sample for *gateway_id* at *tick*.

    Each gateway gets a distinct seed that shifts phase, scales amplitudes,
    and picks a different operating-mode profile — so multiple mocks produce
    visually distinct but realistic curves on the same chart.
    """
    seed = sum(ord(c) for c in gateway_id) or 1
    phase = (seed % 360) * math.pi / 180.0

    # Per-gateway amplitude multipliers (0.6 – 1.0 range) so charts differ
    amp_solar   = 0.6 + 0.4 * ((seed * 7)  % 100) / 100.0
    amp_battery = 0.6 + 0.4 * ((seed * 13) % 100) / 100.0
    amp_home    = 0.7 + 0.3 * ((seed * 17) % 100) / 100.0
    soc_base    = 30  + 40  * ((seed * 3)  % 100) / 100.0  # 30–70 %

    wave  = math.sin(tick / 6.0  + phase)
    wave2 = math.sin(tick / 11.0 + phase)
    wave3 = math.sin(tick / 20.0 + phase + 1.0)  # slow drift for temp/mode

    soc       = max(0.0,  min(100.0, round(soc_base + 25 * wave * amp_battery, 1)))
    battery_w = round(2500 * wave * amp_battery, 1)
    solar_w   = max(0.0,  round(4000 * max(0.0, wave2) * amp_solar, 1))
    home_w    = round((600 + 500 * abs(wave)) * amp_home, 1)
    grid_w    = round(home_w - solar_w - battery_w, 1)

    if battery_w > 50:
        bstate = "Discharging"
    elif battery_w < -50:
        bstate = "Charging"
    else:
        bstate = "Standby"

    # Temperature: each gateway runs at a different base, varies slowly
    temp_base_amb = 18 + (seed % 8)          # 18–25 °C base ambient
    temp_base_cab = 28 + (seed % 12)         # 28–39 °C base cabinet
    ambient_temp  = round(temp_base_amb + 3 * wave3, 1)
    cabinet_temp  = round(temp_base_cab + 5 * wave3 + abs(battery_w) / 800, 1)

    # Operating mode: rotate through modes on a slow per-gateway cycle
    _MODES = ["Self-Consumption", "TOU", "Emergency Backup"]
    mode_idx  = (tick // 30 + seed) % len(_MODES)
    mode_name = _MODES[mode_idx]

    # Grid mode: mostly Grid Following, occasionally Grid Forming on slow wave
    grid_mode = "Grid Forming" if wave3 > 0.7 else "Grid Following"

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
