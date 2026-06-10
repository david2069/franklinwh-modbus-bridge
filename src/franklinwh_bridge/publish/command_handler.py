"""MQTT command handler — dispatches incoming control messages to the controller.

Handles command topics for battery control (charge/discharge/idle) and
operating mode changes. Includes a software watchdog that auto-releases
battery commands after a timeout.

WSetRvrtTms is written to M704 to keep the aGate in VPP (remote-control)
mode for the command duration.  NOTE: the hardware countdown (WSetRvrtRem)
is cosmetic on the aGate — it stays at 0 regardless of the configured
value (PICS Issue 4).  The *software* watchdog is the real safety timer.
Writing WSetRvrtTms > 0 is still required to prevent the mobile app from
overriding VPP mode while a dispatch is active.

All control actions are logged to the control_log table with hardware state
snapshots. Active command state is persisted to control_state so it survives
restarts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import aiosqlite

from franklinwh_bridge.store.db import log_control_event, save_control_state

logger = logging.getLogger(__name__)

DEFAULT_WATCHDOG_S = 0  # 0 = no time limit (run until explicitly released)
DEFAULT_MAX_POWER_W = 5000
SOC_CHECK_INTERVAL_S = 5  # Check SoC every N seconds in watchdog loop

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
    # Whether the most recent command actually succeeded (hardware write
    # verified / library call returned success). False when a write was
    # rejected, the library lacks the setter, or an error occurred — so the
    # UI can show ✗ instead of a misleading ✓.
    last_success: bool = True


class CommandHandler:
    """Processes MQTT command messages and dispatches to the controller."""

    def __init__(
        self,
        controller: Any,
        db: aiosqlite.Connection,
        on_state_changed: Callable[[], Coroutine] | None = None,
        max_charge_w: int = DEFAULT_MAX_POWER_W,
        max_discharge_w: int = DEFAULT_MAX_POWER_W,
        points_getter: Callable[[], dict[str, Any]] | None = None,
        modbus_lock: asyncio.Lock | None = None,
    ) -> None:
        self._controller = controller
        self._db = db
        self._on_state_changed = on_state_changed
        self._points_getter = points_getter
        self._state = CommandState()
        self._watchdog_task: asyncio.Task | None = None
        self._command_power_w: int = 0
        self._command_power_pct: int = 0
        self._command_duration_s: int = DEFAULT_WATCHDOG_S
        self._target_soc: int = 0  # 0 = disabled; otherwise stop at this SoC
        self._max_charge_w: int = max_charge_w
        self._max_discharge_w: int = max_discharge_w
        self._modbus_lock = modbus_lock or asyncio.Lock()

    @property
    def state(self) -> CommandState:
        return self._state

    @property
    def max_charge_w(self) -> int:
        return self._max_charge_w

    @property
    def max_discharge_w(self) -> int:
        return self._max_discharge_w

    @property
    def max_power_w(self) -> int:
        """Symmetric max: the larger of charge/discharge limits."""
        return max(self._max_charge_w, self._max_discharge_w)

    def set_power_limits(self, charge_w: int, discharge_w: int) -> None:
        """Update max power limits from hardware nameplate (M702)."""
        self._max_charge_w = charge_w
        self._max_discharge_w = discharge_w
        logger.info(
            "Power limits updated: charge=%dW, discharge=%dW",
            charge_w, discharge_w,
        )

    @property
    def power_limits(self) -> dict[str, int]:
        """Return current power limits for REST/dashboard."""
        return {
            "max_charge_w": self._max_charge_w,
            "max_discharge_w": self._max_discharge_w,
            "source": (
                "hardware"
                if self._max_charge_w != DEFAULT_MAX_POWER_W
                else "default"
            ),
        }

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
            "battery_command_target_soc": self._target_soc,
            "sw_watchdog_remain_s": remain,
            "command_elapsed_s": elapsed,
            "last_command_result": self._state.last_result or "None",
        }

    async def _read_hw_state(self) -> dict | None:
        try:
            async with self._modbus_lock:
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

        # Optimistic default; handlers that can fail flip this to False.
        self._state.last_success = True
        try:
            if slug == "battery_command":
                await self._handle_battery_command(payload)
            elif slug == "battery_command_power":
                self._command_power_w = max(0, min(int(float(payload)), self.max_power_w))
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
                self._command_duration_s = max(0, min(int(float(payload)), 7200))
                self._state.watchdog_s = self._command_duration_s
                if self._on_state_changed:
                    await self._on_state_changed()
            elif slug == "battery_command_target_soc":
                self._target_soc = max(0, min(int(float(payload)), 100))
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
            self._state.last_success = False
            logger.error("Command %s failed: %s", slug, exc)

    async def _handle_battery_command(self, action: str) -> None:
        from franklinwh_modbus.types import BatteryCommand

        action = action.strip()

        if action in ("Not Active", "Stop", "Release"):
            await self._release_command(reason=action)
            return

        if action in ("Standby", "Idle"):  # "Idle" kept for backwards compat
            watts = 0
            action = "Standby"  # normalise to canonical name
        elif action in ("Charge", "Discharge"):
            # Pick directional limit
            max_w = (
                self._max_charge_w if action == "Charge"
                else self._max_discharge_w
            )
            if self._command_power_pct > 0:
                # Percentage mode: convert to watts using directional max
                watts = int(max_w * self._command_power_pct / 100)
            else:
                watts = min(self._command_power_w or max_w, max_w)
            if action == "Discharge":
                watts = -watts
        else:
            logger.warning("Unknown battery command: %s", action)
            return

        # Validate target SoC against the command direction.  A stale
        # target from a previous Charge (e.g. 75%) would cause an
        # immediate release if SoC is already below that target when
        # Discharging.  Clear it and warn.
        if self._target_soc > 0:
            soc = self._read_soc()
            if soc is not None:
                is_charge = action == "Charge"
                if is_charge and soc >= self._target_soc:
                    logger.warning(
                        "Target SoC %d%% already reached (SoC=%.1f%%) — clearing target",
                        self._target_soc, soc,
                    )
                    self._target_soc = 0
                elif not is_charge and soc <= self._target_soc:
                    logger.warning(
                        "Target SoC %d%% already reached for %s (SoC=%.1f%%) — clearing target",
                        self._target_soc, action, soc,
                    )
                    self._target_soc = 0

        self._state.watchdog_s = self._command_duration_s
        cmd = BatteryCommand(power_watts=watts)
        # Pass duration to library only when > 0 (0 = no library timer)
        lib_duration = self._state.watchdog_s if self._state.watchdog_s > 0 else None

        # Acquire Modbus lock for the entire send + revert-timer write
        # to prevent interleaving with poller reads.
        async with self._modbus_lock:
            success, msg = await asyncio.to_thread(
                self._controller.send_command, cmd, duration_s=lib_duration
            )

            # Write WSetRvrtTms to keep the aGate in VPP (remote-control) mode.
            # The hardware countdown (WSetRvrtRem) is cosmetic (stays at 0) on
            # the aGate, but writing WSetRvrtTms > 0 is still required to lock
            # out mobile-app overrides during the dispatch.
            # For indefinite commands (duration=0), use max value to keep VPP locked.
            if success:
                rvrt_s = self._state.watchdog_s if self._state.watchdog_s > 0 else 7200
                await asyncio.to_thread(self._write_revert_timer, rvrt_s)

        self._state.active = True
        self._state.action = action
        self._state.power_w = abs(watts)
        self._state.started_at = time.time()
        self._state.last_result = msg
        self._state.last_success = success

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

        # Cancel watchdog only for external releases (user/shutdown).
        # When called FROM the watchdog itself (watchdog_expired,
        # target_soc_reached), _cancel_watchdog() would self-cancel the
        # running task, aborting this release before state is updated.
        if reason in ("watchdog_expired", "target_soc_reached"):
            self._watchdog_task = None  # clear ref; task is already exiting
        else:
            self._cancel_watchdog()

        async with self._modbus_lock:
            success = await asyncio.to_thread(self._full_release)

        self._state.active = False
        self._state.action = ""
        self._state.power_w = 0
        self._state.last_result = "Released" if success else "Release failed"
        self._state.last_success = success

        # Clear target SoC so it doesn't poison the next command.
        # Without this, a target set for Charge (e.g. 75%) would cause
        # a subsequent Discharge to release immediately if SoC is already
        # below the target.
        self._target_soc = 0

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

    def _full_release(self) -> bool:
        """Full release: clear setpoints, disable WSetEna, AND zero revert timer.

        The library's reset_control_state() only clears WSetEna/WSetPct/WSet.
        We must also zero WSetRvrtTms to exit VPP mode — the aGate stays in
        remote-control (VPP) mode as long as WSetRvrtTms > 0, blocking mode
        changes from the FranklinWH mobile app.
        """
        success = self._controller.reset_control_state()

        # Clear the hardware revert timer so the aGate exits VPP mode
        try:
            m704 = self._controller.get_model(704)
            if m704:
                m704.WSetRvrtTms.value = 0
                m704.WSetEnaRvrt.value = 0
                m704.write()
                logger.info(
                    "Cleared revert timer: WSetRvrtTms=0, WSetEnaRvrt=0 (exit VPP mode)"
                )
        except Exception as exc:
            logger.warning("Failed to clear revert timer: %s", exc)

        return success

    def _write_revert_timer(self, duration_s: int) -> None:
        """Write WSetRvrtTms to M704 to keep the aGate in VPP mode.

        WSetRvrtTms > 0 locks the aGate in remote-control (VPP) mode,
        preventing the mobile app from overriding the dispatch.  The
        hardware countdown (WSetRvrtRem) is cosmetic on the aGate — it
        stays at 0 (PICS Issue 4).  The software watchdog is the real
        safety timer that releases the command.
        """
        try:
            m704 = self._controller.get_model(704)
            if m704:
                # Fresh read to avoid writing stale values — the poller may
                # have called m704.read() between send_command and now,
                # overwriting the in-memory model.
                m704.read()
                m704.WSetRvrtTms.value = duration_s
                m704.write()
                logger.info(
                    "Wrote WSetRvrtTms=%ds (VPP mode activated)", duration_s
                )
        except Exception as exc:
            logger.warning("Failed to write WSetRvrtTms: %s", exc)

    async def _handle_operating_mode(self, mode_name: str) -> None:
        mode_val = OPERATING_MODES.get(mode_name)
        if mode_val is None:
            self._state.last_result = f"Unknown mode: {mode_name}"
            self._state.last_success = False
            logger.warning("Unknown operating mode: %s", mode_name)
            return

        method = getattr(self._controller, "set_native_mode", None)
        if method is None:
            self._state.last_result = (
                "Mode control not available (library does not support "
                "set_native_mode — upgrade franklinwh-modbus)"
            )
            self._state.last_success = False
            logger.warning("%s", self._state.last_result)
        else:
            try:
                async with self._modbus_lock:
                    success, msg = await asyncio.to_thread(method, mode_val)
                self._state.last_result = msg
                self._state.last_success = success
                if success:
                    logger.info("Operating mode set to %s (%d)", mode_name, mode_val)
                else:
                    logger.error("Operating mode failed: %s", msg)
            except Exception as exc:
                self._state.last_result = f"Mode change error: {exc}"
                self._state.last_success = False
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
            self._state.last_success = False
            logger.warning("%s", self._state.last_result)
        else:
            try:
                async with self._modbus_lock:
                    success, msg = await asyncio.to_thread(method, pct)
                self._state.last_result = msg
                self._state.last_success = success
                if success:
                    logger.info("%s reserve set to %d%%", reserve_type, pct)
                else:
                    logger.error("Reserve write failed: %s", msg)
            except Exception as exc:
                self._state.last_result = f"Reserve write error: {exc}"
                self._state.last_success = False
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

    def _read_soc(self) -> float | None:
        """Read current SoC from the poller's cached points.

        Uses the ``points_getter`` callback instead of making a separate
        Modbus call — avoids racing with the poller on the same TCP
        connection (which caused silent read failures and missed target
        SoC thresholds).
        """
        if self._points_getter is None:
            return None
        try:
            pts = self._points_getter()
            soc = pts.get("soc")
            return float(soc) if soc is not None else None
        except Exception as exc:
            logger.debug("SoC read from cached points failed: %s", exc)
            return None

    async def _watchdog_loop(self) -> None:
        """Software watchdog: enforces duration timeout and target SoC.

        Checks SoC every ``SOC_CHECK_INTERVAL_S`` seconds and releases
        the command when the target is reached.  Charging stops when SoC
        >= target; discharging stops when SoC <= target.

        When duration is 0 (no limit), only the target SoC check applies.
        If both are 0, the command runs until explicitly released.
        """
        try:
            # Initial delay: let _handle_battery_command finish returning
            # to the API before we start checking.  Without this, a stale
            # target SoC can trigger an immediate release that overwrites
            # last_result before the API reads it.
            await asyncio.sleep(SOC_CHECK_INTERVAL_S)

            while True:
                # Duration timeout (skip when 0 = no time limit)
                if self._state.watchdog_s > 0:
                    elapsed = time.time() - self._state.started_at
                    if elapsed >= self._state.watchdog_s:
                        logger.warning(
                            "Watchdog expired after %ds — releasing battery command",
                            self._state.watchdog_s,
                        )
                        await self._release_command(reason="watchdog_expired")
                        return

                # Check target SoC
                target = self._target_soc
                if target > 0:
                    soc = self._read_soc()
                    if soc is not None:
                        is_charge = self._state.action == "Charge"
                        if is_charge and soc >= target:
                            logger.info(
                                "Target SoC reached: %.1f%% >= %d%% — releasing",
                                soc, target,
                            )
                            self._state.last_result = (
                                f"Target SoC {target}% reached (actual {soc:.1f}%)"
                            )
                            await self._release_command(reason="target_soc_reached")
                            return
                        if not is_charge and soc <= target:
                            logger.info(
                                "Target SoC reached: %.1f%% <= %d%% — releasing",
                                soc, target,
                            )
                            self._state.last_result = (
                                f"Target SoC {target}% reached (actual {soc:.1f}%)"
                            )
                            await self._release_command(reason="target_soc_reached")
                            return

                await asyncio.sleep(SOC_CHECK_INTERVAL_S)
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
