"""Async Modbus poll loop wrapping franklinwh-modbus FranklinWHController."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from franklinwh_bridge.modbus.sample import Sample, SampleBus

logger = logging.getLogger(__name__)

POLL_METHODS = [
    "read_battery_status",
    "read_grid_status",
    "read_solar_status",
    "read_nameplate",
    "read_control_status",
    "read_native_mode",
    "read_alarms",
]


@dataclass
class PollerState:
    connected: bool = False
    last_poll_ts: float | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    polls_total: int = 0
    errors_total: int = 0


class ModbusPoller:
    """Polls a FranklinWH aGate via franklinwh-modbus and emits Samples."""

    def __init__(
        self,
        controller: Any,
        sample_bus: SampleBus,
        gateway_id: str = "default",
        poll_interval: int = 30,
    ) -> None:
        self._controller = controller
        self._bus = sample_bus
        self._gateway_id = gateway_id
        self._poll_interval = poll_interval
        self._state = PollerState()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def state(self) -> PollerState:
        return self._state

    async def _connect(self) -> bool:
        try:
            result = await asyncio.to_thread(self._controller.connect)
            self._state.connected = bool(result)
            if self._state.connected:
                if self._state.consecutive_errors > 0:
                    logger.warning(
                        "Reconnected to aGate at %s after %d errors",
                        self._controller.ip_address,
                        self._state.consecutive_errors,
                    )
                else:
                    logger.info("Connected to aGate at %s", self._controller.ip_address)
            return self._state.connected
        except Exception as exc:
            self._state.connected = False
            self._state.last_error = str(exc)
            logger.error("Connection failed: %s", exc)
            return False

    async def _disconnect(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._controller.disconnect)
        self._state.connected = False

    async def _poll_once(self) -> Sample:
        """Run all read methods and merge into a single Sample."""
        points: dict[str, Any] = {}
        quality = "ok"

        for method_name in POLL_METHODS:
            method = getattr(self._controller, method_name, None)
            if method is None:
                continue
            try:
                result = await asyncio.to_thread(method)
                if isinstance(result, dict):
                    for k, v in result.items():
                        if isinstance(v, dict):
                            points.update(v)
                        else:
                            points[k] = v
            except Exception as exc:
                logger.warning("Read %s failed: %s", method_name, exc)
                quality = "stale"

        try:
            extra = await asyncio.to_thread(self._read_extra_points)
            points.update(extra)
        except Exception as exc:
            logger.warning("Extra point reads failed: %s", exc)

        if not points:
            quality = "error"

        return Sample.now(self._gateway_id, points, quality)

    def _read_extra_points(self) -> dict[str, Any]:
        """Read points not covered by the standard controller methods."""
        points: dict[str, Any] = {}

        # M714 DC energy counters (battery lifetime charge/discharge)
        m714 = self._controller.get_model(714)
        if m714:
            m714.read()
            inj = getattr(m714, "DCWhInj", None)
            if inj and inj.value is not None:
                points["dc_energy_discharged_wh"] = int(inj.value)
            absorb = getattr(m714, "DCWhAbs", None)
            if absorb and absorb.value is not None:
                points["dc_energy_charged_wh"] = int(absorb.value)

        # PVOutputWh at extension register 15510 (32-bit unsigned)
        EXT_PV_OUTPUT_WH = getattr(self._controller, "EXT_BASE", 15500) + 10
        try:
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(
                self._controller.ip_address, port=self._controller.port
            )
            client.connect()
            result = client.read_holding_registers(
                EXT_PV_OUTPUT_WH, count=2, device_id=self._controller.unit_id
            )
            if not result.isError():
                val = (result.registers[0] << 16) | result.registers[1]
                points["pv_energy_total_wh"] = val
            client.close()
        except Exception as exc:
            logger.debug("PVOutputWh read failed: %s", exc)

        return points

    def _backoff_delay(self) -> float:
        return min(5.0 * (2 ** self._state.consecutive_errors), 60.0)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._state.connected and not await self._connect():
                delay = self._backoff_delay()
                logger.info("Reconnect in %.0fs", delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                continue

            try:
                sample = await self._poll_once()
                self._state.last_poll_ts = time.time()
                self._state.polls_total += 1

                if sample.quality == "error":
                    self._state.consecutive_errors += 1
                    self._state.errors_total += 1
                    self._state.last_error = "All reads failed"
                    if self._state.connected:
                        logger.warning("Modbus connection lost — all reads failed")
                    self._state.connected = False
                else:
                    self._state.consecutive_errors = 0
                    self._state.last_error = None

                await self._bus.publish(sample)

            except Exception as exc:
                self._state.consecutive_errors += 1
                self._state.errors_total += 1
                self._state.last_error = str(exc)
                if self._state.connected:
                    logger.warning("Modbus connection lost: %s", exc)
                self._state.connected = False
                logger.error("Poll failed: %s", exc)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Poller started (interval=%ds)", self._poll_interval)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._disconnect()
        logger.info("Poller stopped")

    async def poll_once(self) -> Sample:
        """Single poll for CLI 'bridge run --once'."""
        if not self._state.connected:
            await self._connect()
        sample = await self._poll_once()
        self._state.last_poll_ts = time.time()
        self._state.polls_total += 1
        await self._bus.publish(sample)
        return sample
