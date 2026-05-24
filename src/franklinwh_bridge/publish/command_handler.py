"""MQTT command handler — dispatches incoming control messages to the controller.

Handles command topics for battery control (charge/discharge/idle) and
operating mode changes. Includes a software watchdog that auto-releases
battery commands after a timeout (hardware WSetRvrtTms is cosmetic on
FranklinWH).

All control actions are logged to the control_log table with hardware state
snapshots. Active command state is persisted to control_state so it survives
restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

import aiosqlite

from franklinwh_bridge.store.db import log_control_event, save_control_state

logger = logging.getLogger(__name__)

DEFAULT_WATCHDOG_S = 3600
MAX_POWER_W = 5000

OPERATING_MODES = {
    "Emergency Backup": 1,
    "Time of Use": 3,
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
        db: aiosqlite.Connection,
        on_state_changed: Callable[[], Coroutine] | None = None,
    ) -> None:
        self._controller = controller
        self._db = db
        self._on_state_changed = on_state_changed
        self._state = CommandState()
        self._watchdog_task: asyncio.Task | None = None
        self._command_power_w: int = 0
        self._command_power_pct: int = 0
        self._command_duration_s: int = DEFAULT_WATCHDOG_S

    @property
    def state(self) -> CommandState:
        return self._state

    @property
    def virtual_points(self) -> dict[str, Any]:
        now = time.time()
        elapsed = int(now - self._state.started_at) if self._state.active else 0
        remain = max(0, self._state.watchdog_s - elapsed) if self._state.active else 0
        return {
            "battery_command_state": self._state.action if self._state.active else "Not Active",
            "battery_command_power_w": self._command_power_w,
            "battery_command_power_pct": self._command_power_pct,
            "battery_command_duration_s": self._command_duration_s,
            "sw_watchdog_remain_s": remain,
            "command_elapsed_s": elapsed,
            "last_command_result": self._state.last_result or "None",
        }

    async def _read_hw_state(self) -> dict | None:
        try:
            return await asyncio.to_thread(self._controller.read_control_status)
        except Exception as exc:
            logger.debug("Could not read hw state for audit: %s", exc)
            return None

    async def _persist_state(self) -> None:
        try:
            await save_control_state(
                self._db,
                active=self._state.active,
                action=self._state.action,
                power_w=self._state.power_w,
                started_at=self._state.started_at,
                watchdog_s=self._state.watchdog_s,
            )
        except Exception as exc:
            logger.warning("Failed to persist control state: %s", exc)

    async def _log_event(
        self, event: str, action: str = "", power_w: int = 0, detail: str = "",
    ) -> None:
        hw_state = await self._read_hw_state()
        try:
            await log_control_event(
                self._db, event=event, action=action, power_w=power_w,
                detail=detail, hw_state=hw_state,
            )
        except Exception as exc:
            logger.warning("Failed to write control log: %s", exc)

    async def handle_command(self, slug: str, payload: str) -> None:
        payload = payload.strip()
        logger.info("Command received: %s = %s", slug, payload)

        try:
            if slug == "battery_command":
                await self._handle_battery_command(payload)
            elif slug == "battery_command_power":
                self._command_power_w = max(0, min(int(float(payload)), MAX_POWER_W))
                self._command_power_pct = 0  # watts takes precedence, clear pct
                if self._on_state_changed:
                    await self._on_state_changed()
                if self._state.active:
                    await self._handle_battery_command(self._state.action)
            elif slug == "battery_command_power_pct":
                self._command_power_pct = max(0, min(int(float(payload)), 100))
                self._command_power_w = 0  # pct takes precedence, clear watts
                if self._on_state_changed:
                    await self._on_state_changed()
                if self._state.active:
                    await self._handle_battery_command(self._state.action)
            elif slug == "battery_command_duration":
                self._command_duration_s = max(60, min(int(float(payload)), 7200))
                self._state.watchdog_s = self._command_duration_s
                if self._on_state_changed:
                    await self._on_state_changed()
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

        if action in ("Not Active", "Stop", "Release"):
            await self._release_command(reason=action)
            return

        if action == "Idle":
            watts = 0
        elif action in ("Charge", "Discharge"):
            if self._command_power_pct > 0:
                # Percentage mode: convert to watts using max rate
                watts = int(MAX_POWER_W * self._command_power_pct / 100)
            else:
                watts = self._command_power_w or MAX_POWER_W
            if action == "Discharge":
                watts = -watts
        else:
            logger.warning("Unknown battery command: %s", action)
            return

        self._state.watchdog_s = self._command_duration_s
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
        await self._persist_state()
        await self._log_event(
            "command_sent", action=action, power_w=abs(watts), detail=msg,
        )

        if self._on_state_changed:
            await self._on_state_changed()

        logger.info("Battery command: %s %dW — %s", action, abs(watts), msg)

    async def _release_command(self, reason: str = "release") -> None:
        was_active = self._state.active
        prev_action = self._state.action
        prev_power = self._state.power_w

        self._cancel_watchdog()
        success = await asyncio.to_thread(self._controller.reset_control_state)

        self._state.active = False
        self._state.action = ""
        self._state.power_w = 0
        self._state.last_result = "Released" if success else "Release failed"

        await self._persist_state()
        await self._log_event(
            "command_released",
            action=prev_action,
            power_w=prev_power,
            detail=f"reason={reason}, was_active={was_active}, result={self._state.last_result}",
        )

        if self._on_state_changed:
            await self._on_state_changed()

        logger.info("Battery command released: %s (reason=%s)", self._state.last_result, reason)

    async def _handle_operating_mode(self, mode_name: str) -> None:
        mode_val = OPERATING_MODES.get(mode_name)
        if mode_val is None:
            self._state.last_result = f"Unknown mode: {mode_name}"
            logger.warning("Unknown operating mode: %s", mode_name)
            return

        method = getattr(self._controller, "set_native_mode", None)
        if method is None:
            self._state.last_result = (
                "Mode control not available (library does not support "
                "set_native_mode — upgrade franklinwh-modbus)"
            )
            logger.warning("%s", self._state.last_result)
        else:
            try:
                success, msg = await asyncio.to_thread(method, mode_val)
                self._state.last_result = msg
                if success:
                    logger.info("Operating mode set to %s (%d)", mode_name, mode_val)
                else:
                    logger.error("Operating mode failed: %s", msg)
            except Exception as exc:
                self._state.last_result = f"Mode change error: {exc}"
                logger.error("Operating mode failed: %s", exc)

        await self._log_event(
            "mode_change", action=mode_name, detail=self._state.last_result,
        )

    async def _handle_reserve(self, reserve_type: str, pct: int) -> None:
        pct = max(0, min(pct, 100))

        if reserve_type == "self":
            method = getattr(self._controller, "set_self_consumption_reserve", None)
        else:
            method = getattr(self._controller, "set_tou_reserve", None)

        if method is None:
            self._state.last_result = (
                f"Reserve control not available (library does not support "
                f"set_{reserve_type}_reserve — upgrade franklinwh-modbus)"
            )
            logger.warning("%s", self._state.last_result)
        else:
            try:
                success, msg = await asyncio.to_thread(method, pct)
                self._state.last_result = msg
                if success:
                    logger.info("%s reserve set to %d%%", reserve_type, pct)
                else:
                    logger.error("Reserve write failed: %s", msg)
            except Exception as exc:
                self._state.last_result = f"Reserve write error: {exc}"
                logger.error("Reserve write failed: %s", exc)

        await self._log_event(
            "reserve_change",
            action=f"{reserve_type}_reserve",
            power_w=pct,
            detail=self._state.last_result,
        )

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
            await self._release_command(reason="watchdog_expired")
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        """Graceful shutdown: release active commands and log the event."""
        self._cancel_watchdog()
        if self._state.active:
            logger.info(
                "Shutdown: releasing active command %s %dW",
                self._state.action, self._state.power_w,
            )
            await self._release_command(reason="shutdown")
        else:
            await self._log_event("shutdown", detail="no active command")
