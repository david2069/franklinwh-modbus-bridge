"""MQTT command handler — dispatches incoming control messages to the controller.

Handles command topics for battery control (charge/discharge/idle) and
operating mode changes. Includes a software watchdog that auto-releases
battery commands after a timeout (hardware WSetRvrtTms is cosmetic on
FranklinWH).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

DEFAULT_WATCHDOG_S = 3600
MAX_POWER_W = 5000

OPERATING_MODES = {
    "Emergency Backup": 0,
    "Time of Use": 1,
    "Self-Consumption": 2,
}


@dataclass
class CommandState:
    active: bool = False
    action: str = ""
    power_w: int = 0
    started_at: float = 0.0
    watchdog_s: int = DEFAULT_WATCHDOG_S
    last_result: str = ""


class CommandHandler:
    """Processes MQTT command messages and dispatches to the controller."""

    def __init__(
        self,
        controller: Any,
        on_state_changed: Callable[[], Coroutine] | None = None,
    ) -> None:
        self._controller = controller
        self._on_state_changed = on_state_changed
        self._state = CommandState()
        self._watchdog_task: asyncio.Task | None = None
        self._command_power_w: int = 0

    @property
    def state(self) -> CommandState:
        return self._state

    @property
    def virtual_points(self) -> dict[str, Any]:
        return {
            "battery_command_state": self._state.action or "Idle",
            "battery_command_power_w": self._command_power_w,
        }

    async def handle_command(self, slug: str, payload: str) -> None:
        payload = payload.strip()
        logger.info("Command received: %s = %s", slug, payload)

        try:
            if slug == "battery_command":
                await self._handle_battery_command(payload)
            elif slug == "battery_command_power":
                self._command_power_w = max(0, min(int(float(payload)), MAX_POWER_W))
                if self._state.active:
                    await self._handle_battery_command(self._state.action)
            elif slug == "operating_mode":
                await self._handle_operating_mode(payload)
            elif slug == "self_reserve_pct":
                await self._handle_reserve("self", int(float(payload)))
            elif slug == "tou_reserve_pct":
                await self._handle_reserve("tou", int(float(payload)))
            else:
                logger.warning("Unknown command slug: %s", slug)
        except Exception as exc:
            self._state.last_result = f"Error: {exc}"
            logger.error("Command %s failed: %s", slug, exc)

    async def _handle_battery_command(self, action: str) -> None:
        from franklinwh_modbus.types import BatteryCommand

        action = action.strip()

        if action == "Idle":
            await self._release_command()
            return

        power = self._command_power_w or MAX_POWER_W

        if action == "Charge":
            watts = power
        elif action == "Discharge":
            watts = -power
        else:
            logger.warning("Unknown battery command: %s", action)
            return

        cmd = BatteryCommand(power_watts=watts)
        success, msg = await asyncio.to_thread(
            self._controller.send_command, cmd, duration_s=self._state.watchdog_s
        )

        self._state.active = True
        self._state.action = action
        self._state.power_w = abs(watts)
        self._state.started_at = time.time()
        self._state.last_result = msg

        self._start_watchdog()

        if self._on_state_changed:
            await self._on_state_changed()

        logger.info("Battery command: %s %dW — %s", action, abs(watts), msg)

    async def _release_command(self) -> None:
        self._cancel_watchdog()
        success = await asyncio.to_thread(self._controller.reset_control_state)

        self._state.active = False
        self._state.action = ""
        self._state.power_w = 0
        self._state.last_result = "Released" if success else "Release failed"

        if self._on_state_changed:
            await self._on_state_changed()

        logger.info("Battery command released: %s", self._state.last_result)

    async def _handle_operating_mode(self, mode_name: str) -> None:
        mode_val = OPERATING_MODES.get(mode_name)
        if mode_val is None:
            self._state.last_result = f"Unknown mode: {mode_name}"
            logger.warning("Unknown operating mode: %s", mode_name)
            return

        EXT_ONGRID_MODE = getattr(self._controller, "EXT_ONGRID_MODE", 15507)
        try:
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(
                self._controller.ip_address, port=self._controller.port
            )
            client.connect()
            result = client.write_register(
                EXT_ONGRID_MODE, mode_val, device_id=self._controller.unit_id
            )
            client.close()
            if result.isError():
                self._state.last_result = f"Mode write failed: {result}"
                logger.error("Mode write failed: %s", result)
            else:
                self._state.last_result = f"Mode set to {mode_name}"
                logger.info("Operating mode set to %s (%d)", mode_name, mode_val)
        except Exception as exc:
            self._state.last_result = f"Mode write error: {exc}"
            logger.error("Operating mode write failed: %s", exc)

    async def _handle_reserve(self, reserve_type: str, pct: int) -> None:
        pct = max(0, min(pct, 100))
        if reserve_type == "self":
            reg = getattr(self._controller, "EXT_SELF_RESERVE", 15508)
        else:
            reg = getattr(self._controller, "EXT_TOU_RESERVE", 15509)

        try:
            from pymodbus.client import ModbusTcpClient
            client = ModbusTcpClient(
                self._controller.ip_address, port=self._controller.port
            )
            client.connect()
            result = client.write_register(
                reg, pct, device_id=self._controller.unit_id
            )
            client.close()
            if result.isError():
                self._state.last_result = f"Reserve write failed: {result}"
            else:
                self._state.last_result = f"{reserve_type} reserve set to {pct}%"
                logger.info("%s reserve set to %d%%", reserve_type, pct)
        except Exception as exc:
            self._state.last_result = f"Reserve write error: {exc}"
            logger.error("Reserve write failed: %s", exc)

    def _start_watchdog(self) -> None:
        self._cancel_watchdog()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            self._watchdog_task = None

    async def _watchdog_loop(self) -> None:
        try:
            await asyncio.sleep(self._state.watchdog_s)
            logger.warning(
                "Watchdog expired after %ds — releasing battery command",
                self._state.watchdog_s,
            )
            await self._release_command()
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._cancel_watchdog()
        if self._state.active:
            await self._release_command()
