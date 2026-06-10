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

    A per-gateway phase offset makes multiple mocks produce distinct but
    realistic, smoothly varying curves.  Emits the canonical point keys the
    dashboard and Site aggregator read (so a mock renders the standard cards).
    """
    seed = sum(ord(c) for c in gateway_id) or 1
    phase = (seed % 360) * math.pi / 180.0
    wave = math.sin(tick / 6.0 + phase)
    wave2 = math.sin(tick / 11.0 + phase)

    soc = max(0.0, min(100.0, round(55 + 30 * wave, 1)))
    battery_w = round(2200 * wave, 1)  # positive = discharging
    solar_w = max(0.0, round(3500 * max(0.0, wave2), 1))
    home_w = round(700 + 400 * abs(wave), 1)
    grid_w = round(home_w - solar_w - battery_w, 1)  # balances the rest

    if battery_w > 50:
        bstate = "Discharging"
    elif battery_w < -50:
        bstate = "Charging"
    else:
        bstate = "Standby"

    return {
        "soc": soc,
        "battery_power_w": battery_w,
        "battery_dc_power_w": battery_w,
        "grid_power_w": grid_w,
        "total_solar": solar_w,
        "home_load_ext": home_w,
        "battery_state": bstate,
        "connection_state": "Connected",
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
