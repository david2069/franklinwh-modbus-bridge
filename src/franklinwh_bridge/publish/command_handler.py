"""MQTT command handler — dispatches incoming control messages to the controller.

Handles command topics for battery control (charge/discharge/idle) and
operating mode changes. Includes a software watchdog that auto-releases
battery commands after a timeout.

WSetRvrtTms is written to M704 to keep the aGate in VPP (remote-control)
mode for the command duration.  NOTE: the hardware countdown (WSetRvrtRem)
does count down, but the aGate does NOT revert when it reaches 0 (PICS
Issue 4 — the reversion is cosmetic, the countdown is not).  The *software*
watchdog is the real safety timer.  For an open-ended dispatch the bridge
writes 7200 s, and RvrtRem has been seen staying at 0 for that value while it
counts down for a schedule's shorter durations — not yet confirmed why.
Writing WSetRvrtTms > 0 is still required to prevent the mobile app from
overriding VPP mode while a dispatch is active.

All control actions are logged to the control_log table with hardware state
snapshots. Active command state is persisted to control_state so it survives
restarts.

ORPHAN SAFETY:  On graceful shutdown, ``stop()`` releases any active dispatch
(zeroing WSetEna + WSetRvrtTms, exiting VPP mode).  On a *crash* the registers
stay set, but the next startup's ``_release_stale_commands`` (gateway/instance)
clears them.  The one window we CANNOT cover in software is crash-and-never-
restart: because the aGate never reverts when WSetRvrtRem runs out (PICS
Issue 4), a dispatch left by a bridge that never comes back stays live in
VPP mode until the bridge restarts or the mobile app intervenes.  The running
bridge is the only thing that releases dispatches.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

import aiosqlite

from franklinwh_bridge.gateway import link_stats
from franklinwh_bridge.gateway.battery_rating import BatteryRating
from franklinwh_bridge.store import alerts as alert_store
from franklinwh_bridge.store import dispatch_outcomes, modbus_audit
from franklinwh_bridge.store.db import log_control_event, save_control_state

logger = logging.getLogger(__name__)

#: The library appends an explanation to extension-register write failures that
#: this project cannot stand behind: that the write was refused because "SPAN
#: Modbus" is locked in installer settings. That is a hypothesis, not a finding.
#: What IS established (docs/vendor-issues.md, issue 11) is narrower: writes to
#: 15507-15509 are acknowledged at the protocol level but the register does not
#: change, on every real aGate tested — and an exhaustive sweep, including
#: cloud-shaped payloads, never found a sequence that made them stick. The cause
#: is unknown.
#:
#: Telling a user to go ask their installer to flip a setting sends them after a
#: fix that has never been shown to exist. So the speculation is stripped on the
#: way out; the factual half of the library's message is kept verbatim.
#:
#: Written as a suffix strip rather than a rewrite so it simply stops matching
#: once the library drops the sentence, instead of needing to be removed in
#: lockstep.
_SPECULATION = "Ensure 'SPAN Modbus' is unlocked in installer settings."

_UNKNOWN_CAUSE = (
    "The aGate acknowledged the write but the register did not change. "
    "This affects the mode/reserve extension registers on all hardware tested "
    "so far; the cause is not established. See docs/vendor-issues.md."
)


def _strip_speculation(msg: str) -> str:
    """Replace the library's unverified SPAN-lock explanation with what is known."""
    if not msg or _SPECULATION not in msg:
        return msg
    return f"{msg.replace(_SPECULATION, '').strip()} {_UNKNOWN_CAUSE}".strip()


#: What the aGate's Modbus exception codes mean, in words.
MODBUS_EXCEPTIONS = {
    1: "illegal function: the aGate doesn't support that request",
    2: "illegal data address: the aGate has no such register",
    3: "illegal data value: the aGate refused a value it was sent",
    4: "device failure: the aGate hit an internal error",
    6: "device busy: the aGate was busy",
}
_MODBUS_EXCEPTION_RE = re.compile(r"Modbus exception:?\s*(\d+)", re.IGNORECASE)


def explain_command_failure(msg: str, stopped_previous: bool = False) -> str:
    """A failed command's reason in words: what the code means and that it didn't run.

    "Modbus exception: 3" alone told the owner nothing (#323), and a toast
    that only says "failed" doesn't say whether the battery did anything.
    """
    m = _MODBUS_EXCEPTION_RE.search(msg or "")
    if m:
        code = int(m.group(1))
        meaning = MODBUS_EXCEPTIONS.get(code, "an error code this bridge doesn't know")
        text = f"Rejected by the aGate (Modbus exception {code}: {meaning})."
    else:
        text = (msg or "Failed").rstrip(".") + "."
    if stopped_previous:
        return f"{text} The command did not run, and the previous command has stopped."
    return f"{text} The command did not run; the battery carries on as before."


DEFAULT_WATCHDOG_S = 0  # 0 = no time limit (run until explicitly released)
#: WSetRvrtTms written for an open-ended dispatch. It holds remote control
#: against app overrides; it is not a backstop (PICS Issue 4: no revert at 0).
OPEN_ENDED_RVRT_S = 7200
DEFAULT_MAX_POWER_W = 5000
SOC_CHECK_INTERVAL_S = 5  # Check SoC every N seconds in watchdog loop
#: Overshoot guard (#133): after this long, a battery running above the request
#: by more than the margin means the rating it was sized against is wrong.
OVERSHOOT_CHECK_AFTER_S = 20
OVERSHOOT_FACTOR = 1.25
OVERSHOOT_SLACK_W = 500

OPERATING_MODES = {
    "Emergency Backup": 1,
    "Self-Consumption": 2,
    "TOU": 3,             # matches the library's read vocabulary (NATIVE_MODES)
    "Time of Use": 3,     # accepted alias (legacy HA/dashboard payloads)
}


#: Every slug handle_command acts on. Anything else is refused, not ignored (#97).
COMMAND_SLUGS = frozenset({
    "battery_command", "battery_command_power", "battery_command_power_pct",
    "battery_command_duration", "battery_command_target_soc",
    "operating_mode", "self_reserve_pct", "tou_reserve_pct",
})


#: How a refused command is named in its alert.
_COMMAND_LABELS = {
    "battery_command": "Battery command",
    "battery_command_power": "Battery power (W)",
    "battery_command_power_pct": "Battery power (%)",
    "battery_command_duration": "Command duration (s)",
    "battery_command_target_soc": "Target SoC (%)",
    "operating_mode": "Operating mode",
    "self_reserve_pct": "Self-consumption reserve (%)",
    "tou_reserve_pct": "TOU reserve (%)",
}


def _setpoint_text(hw: dict | None) -> str:
    """The M704 setpoint as the aGate reports it, in the form it holds it.

    WSetMod 1 (WATTS) means WSet carries the power; 0 (W_MAX_PCT) means WSetPct.
    franklinwh-modbus 0.10.1+ sends watts, so WSetPct then reads 0.
    """
    hw = hw or {}
    parts = [f"WSetEna={hw.get('wset_enabled')}"]
    mode = hw.get("wset_mode")
    if mode is not None and int(mode) == 1:
        parts += ["WSetMod=Watts", f"WSet={hw.get('wset_watts')} W"]
    elif mode is not None and int(mode) == 0:
        parts += ["WSetMod=Pct", f"WSetPct={hw.get('wset_pct')}%"]
    else:
        parts += [f"WSetMod={mode}", f"WSet={hw.get('wset_watts')} W",
                  f"WSetPct={hw.get('wset_pct')}%"]
    return ", ".join(parts)


@dataclass
class CommandState:
    active: bool = False
    action: str = ""
    power_w: int = 0
    power_pct: int = 0  # a % command (WSetPct passed through); power_w is 0 then
    started_at: float = 0.0
    watchdog_s: int = DEFAULT_WATCHDOG_S
    last_result: str = ""
    # Whether the most recent command actually succeeded (hardware write
    # verified / library call returned success). False when a write was
    # rejected, the library lacks the setter, or an error occurred — so the
    # UI can show ✗ instead of a misleading ✓.
    last_success: bool = True


@dataclass
class DispatchResult:
    """Outcome of the most recent battery command (design PR #57 §5.1).

    ``ok`` is True only if the library accepted the write AND WSetEna read back
    as 1. A failed send used to leave the command marked active and the
    scheduler audited it "ok" (#56); callers now read this instead.
    """

    ok: bool
    reason: str
    action: str
    requested_w: int
    dispatch_id: str | None = None
    hw: dict | None = None
    ts: float = 0.0


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
        gateway_id: str = "default",
        rating: BatteryRating | None = None,
    ) -> None:
        self._controller = controller
        #: Evidence-backed battery power rating; force commands need it trusted.
        self.rating = rating or BatteryRating(gateway_id)
        self._db = db
        # Which gateway this handler commands. The control log and the persisted
        # command state are per gateway; without it every gateway's dispatches
        # were logged — and their crash-recovery state saved — as "default".
        self._gateway_id = gateway_id
        # Set when the gateway's serial is refused (#296 item 42): no command
        # may reach that aGate.
        self.refused: str | None = None
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
        # Set when the aGate acknowledged a mode write but kept the old mode
        # (FranklinWH extension registers 15507-15509 write-locked on some
        # firmware); cleared by the next mode write that sticks. Lets the
        # scheduler skip a release-time mode write that is known to fail.
        self._mode_write_locked: bool = False
        # Result of the latest battery command, and the dispatch_outcomes row of
        # the command currently active (so a release or supersede can close it).
        self._last_dispatch: DispatchResult | None = None
        self._active_dispatch_id: str | None = None
        #: Who sent the active command (e.g. ``schedule:<entry id>``), so a
        #: release by someone else can be noted against that schedule entry.
        self._active_requester: str | None = None
        self._requester: str = "manual"
        # What this bridge last asked the gateway for, to tell our own dispatch
        # from one sent by something else (another app, a CLI, the cloud).
        self._sent_pct: float | None = None
        #: What we sent, as the hardware holds it in WSet: + discharge, - charge.
        self._sent_w: float | None = None
        self._last_change_ts: float = 0.0
        self._external: dict | None = None
        self._external_change: bool = False

    @property
    def state(self) -> CommandState:
        return self._state

    @property
    def last_dispatch(self) -> DispatchResult | None:
        return self._last_dispatch

    @property
    def active_dispatch_id(self) -> str | None:
        return self._active_dispatch_id

    @property
    def active_requester(self) -> str | None:
        return self._active_requester

    @property
    def mode_write_locked(self) -> bool:
        return self._mode_write_locked

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
    def power_limits(self) -> dict[str, Any]:
        """Limits a force command may use: trusted ratings only, None otherwise (#133)."""
        charge, discharge = self.rating.decision("charge"), self.rating.decision("discharge")
        return {
            "max_charge_w": charge.rating_w if charge.trusted else None,
            "max_discharge_w": discharge.rating_w if discharge.trusted else None,
            "charge_source": charge.source,
            "discharge_source": discharge.source,
            "source": charge.source if charge.source == discharge.source else "mixed",
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
            # 0 = no time limit (the command runs until released or the target
            # SoC is reached) — NOT "expired". The panel needs this to tell the
            # two apart; remain alone reads 0 for both.
            "sw_watchdog_limit_s": self._state.watchdog_s if self._state.active else 0,
            "sw_watchdog_remain_s": remain,
            "command_elapsed_s": elapsed,
            "last_command_result": self._state.last_result or "None",
            # Trusted battery ratings (#133): None until calibrated or confirmed,
            # so the UI can size its controls and say why force is unavailable.
            "battery_rating_charge_w": self._trusted_w("charge"),
            "battery_rating_discharge_w": self._trusted_w("discharge"),
        }

    def _trusted_w(self, direction: str) -> int | None:
        d = self.rating.decision(direction)
        return d.rating_w if d.trusted else None

    async def _read_hw_state(self) -> dict | None:
        try:
            async with self._modbus_lock:
                return await asyncio.to_thread(
                    self._io, "model_read", self._controller.read_control_status,
                )
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
                gateway_id=self._gateway_id,
            )
        except Exception as exc:
            logger.warning("Failed to persist control state: %s", exc)

    async def _log_event(
        self, event: str, action: str = "", power_w: int = 0, detail: str = "",
        hw_state: dict | None = None, by: str | None = None,
    ) -> None:
        if hw_state is None:
            hw_state = await self._read_hw_state()
        if by is not None:
            detail = f"{detail} — by {by}" if detail else f"by {by}"
        try:
            await log_control_event(
                self._db, event=event, action=action, power_w=power_w,
                detail=detail, hw_state=hw_state, gateway_id=self._gateway_id,
            )
        except Exception as exc:
            logger.warning("Failed to write control log: %s", exc)

    async def handle_command(
        self, slug: str, payload: str, requester: str = "manual",
    ) -> dict[str, Any]:
        """Attribute this command's Modbus writes (#342), apply it, and alert on a refusal."""
        with modbus_audit.writing_as(self._gateway_id, requester or "manual"):
            result = await self._handle_command(slug, payload, requester)
        if not result["ok"] and slug in COMMAND_SLUGS:
            await self._alert_refused(slug, payload, result["result"])
        return result

    async def _alert_refused(self, slug: str, payload: str, reason: str) -> None:
        """COMMAND_REFUSED, or WRITE_IGNORED when the aGate took it and kept the old value (#324).

        The reason says who refused: "Rejected by the aGate (Modbus exception
        …)" or the bridge's own "Refused …". The alert store never raises.
        """
        ignored = "Hardware ignored write" in reason or _UNKNOWN_CAUSE in reason
        what = _COMMAND_LABELS.get(slug, slug)
        await alert_store.raise_event(
            self._db, "WRITE_IGNORED" if ignored else "COMMAND_REFUSED",
            f"{what} {payload.strip()} (by {self._requester}): {reason}",
            gateway_id=self._gateway_id)

    async def _handle_command(
        self, slug: str, payload: str, requester: str = "manual",
    ) -> dict[str, Any]:
        """Apply one command and return what THIS call did: ``{ok, result}``.

        ``requester`` (e.g. ``schedule:<entry id>``) is recorded on the
        dispatch_outcomes row of a battery command.

        The REST routes used to answer from ``state`` instead, which an unknown
        slug or action never touched — so it came back ``ok: true`` with an
        earlier command's text (#97). A staging slug (power, duration, target
        SoC) only stores a value; its result says "Staged", not that anything
        was sent to the aGate.
        """
        payload = payload.strip()
        self._requester = requester or "manual"
        logger.info("Command received: %s = %s (by %s)", slug, payload, self._requester)
        if self.refused:
            return {"ok": False, "result": f"Refused: {self.refused}"}

        if slug not in COMMAND_SLUGS:
            logger.warning("Unknown command slug: %s", slug)
            return {"ok": False, "result": f"Unknown command: {slug}"}

        # Optimistic default; handlers that can fail flip this to False.
        self._state.last_success = True
        staged = ""
        resent = False  # a power change re-sends an ACTIVE dispatch
        try:
            if slug == "battery_command":
                await self._handle_battery_command(payload)
            elif slug == "battery_command_power":
                self._command_power_w = max(0, min(int(float(payload)), self.max_power_w))
                self._command_power_pct = 0  # watts takes precedence, clear pct
                staged = f"Staged power {self._command_power_w} W"
                if self._on_state_changed:
                    await self._on_state_changed()
                if self._state.active:
                    resent = True
                    await self._handle_battery_command(self._state.action)
            elif slug == "battery_command_power_pct":
                # % of what this aGate can do: sent as watts (see _battery_command).
                self._command_power_pct = max(0, min(int(float(payload)), 100))
                self._command_power_w = 0  # pct takes precedence, clear watts
                staged = f"Staged power {self._command_power_pct}%"
                if self._on_state_changed:
                    await self._on_state_changed()
                if self._state.active:
                    resent = True
                    await self._handle_battery_command(self._state.action)
            elif slug == "battery_command_duration":
                self._command_duration_s = max(0, min(int(float(payload)), 7200))
                self._state.watchdog_s = self._command_duration_s
                staged = f"Staged duration {self._command_duration_s} s"
                if self._on_state_changed:
                    await self._on_state_changed()
            elif slug == "battery_command_target_soc":
                previous = self._target_soc
                self._target_soc = max(0, min(int(float(payload)), 100))
                staged = (f"Staged target SoC {self._target_soc}%" if self._target_soc
                          else "Staged target SoC off")
                # A target SoC ends a dispatch, so a change belongs in the
                # control log (only a change: the UI may resend the same value).
                if self._target_soc != previous:
                    await self._log_event(
                        "target_soc_set",
                        detail=(f"target SoC {self._target_soc}%" if self._target_soc
                                else "target SoC off"),
                        by=self._requester,
                    )
                if self._on_state_changed:
                    await self._on_state_changed()
            elif slug == "operating_mode":
                await self._handle_operating_mode(payload)
            elif slug == "self_reserve_pct":
                await self._handle_reserve("self", int(float(payload)))
            elif slug == "tou_reserve_pct":
                await self._handle_reserve("tou", int(float(payload)))
        except Exception as exc:
            self._state.last_result = f"Error: {exc}"
            self._state.last_success = False
            if slug == "battery_command":
                self._record_not_sent(payload, self._state.last_result)
            logger.error("Command %s failed: %s", slug, exc)
            return {"ok": False, "result": self._state.last_result}
        # Every dispatching path (battery command, mode, reserve) sets
        # last_result during this call. A staging slug sets it only when it
        # re-sent an active dispatch; otherwise its own text is the result.
        if staged and not resent:
            return {"ok": True, "result": staged}
        if staged:
            return {"ok": self._state.last_success,
                    "result": f"{staged}; {self._state.last_result}"}
        return {"ok": self._state.last_success, "result": self._state.last_result}

    async def _handle_battery_command(self, action: str) -> None:
        from franklinwh_modbus.types import BatteryCommand

        action = action.strip()

        if action in ("Not Active", "Stop", "Release"):
            await self._release_command(reason=action)
            return

        # Canonical UI/state labels are "Force"-prefixed ("Force Charge",
        # "Force Discharge", "Force Standby") so a user-commanded dispatch is
        # visibly distinct from the device's normal charge/discharge.  Accept
        # the prefixed form and the legacy bare verbs (and "Idle").
        base = action[6:].strip() if action.lower().startswith("force ") else action
        if base == "Idle":  # legacy alias
            base = "Standby"

        rated_attr: str | None = None  # the library rating to set for this send
        max_w = 0
        if base == "Standby":
            watts = 0
        elif base in ("Charge", "Discharge"):
            decision = self.rating.decision("charge" if base == "Charge" else "discharge")
            if not decision.trusted:
                self._state.last_result = f"Refused Force {base}: {decision.reason}"
                self._state.last_success = False
                self._record_not_sent(f"Force {base}", self._state.last_result)
                logger.warning("Gateway %s: %s", self._gateway_id, self._state.last_result)
                await self._log_event(
                    "command_refused", detail=self._state.last_result, by=self._requester)
                return
            max_w = int(decision.rating_w)
            # The library clamps to its own rating, so it is given the trusted
            # one (set under the Modbus lock, just before the send: see below).
            rated_attr = "RATED_MAX_CHARGE_W" if base == "Charge" else "RATED_MAX_DISCHARGE_W"
            if self._command_power_pct > 0:
                # Power %: a share of the trusted rating. franklinwh-modbus
                # 0.10.1+ sends watts (WSetMod=1, WSet) and has no WSetPct path,
                # so a % can't be passed through as WSetPct any more. Approved
                # workaround (owner, 2026-10-24), logged on every use; V12's
                # WSetPct limit is tracked in franklinwh-modbus#92.
                watts = round(max_w * self._command_power_pct / 100)
                logger.info(
                    "Workaround: Power %d%% sent as %dW through WSet, not WSetPct "
                    "(V12 refuses WSetPct above 10%% and below 0; approved "
                    "2026-10-24; franklinwh-modbus#92)",
                    self._command_power_pct, watts)
            else:
                watts = min(self._command_power_w or max_w, max_w)
            if base == "Discharge":
                watts = -watts
        else:
            logger.warning("Unknown battery command: %s", action)
            self._state.last_result = f"Unknown battery command: {action}"
            self._state.last_success = False
            self._record_not_sent(action, self._state.last_result)
            return

        display_action = f"Force {base}"  # canonical, prefixed label
        # A % command is sent as watts of the trusted rating; the label keeps
        # the % that was asked for, with the watts it became.
        pct_cmd = self._command_power_pct if base in ("Charge", "Discharge") else 0
        report_w = watts
        power_label = f"{pct_cmd}% ({abs(watts)}W)" if pct_cmd else f"{abs(watts)}W"

        # Capture the prior dispatch so we can log a "superseded" event when a
        # new command replaces a *different* active one (not just a power tweak).
        prev_active = self._state.active
        prev_action = self._state.action
        prev_power = self._state.power_w

        # Validate target SoC against the command direction.  A stale
        # target from a previous Charge (e.g. 75%) would cause an
        # immediate release if SoC is already below that target when
        # Discharging.  Clear it and warn.
        if self._target_soc > 0:
            soc = self._read_soc()
            if soc is not None:
                is_charge = base == "Charge"
                if is_charge and soc >= self._target_soc:
                    logger.warning(
                        "Target SoC %d%% already reached (SoC=%.1f%%) — clearing target",
                        self._target_soc, soc,
                    )
                    self._target_soc = 0
                elif not is_charge and soc <= self._target_soc:
                    logger.warning(
                        "Target SoC %d%% already reached for %s (SoC=%.1f%%) — clearing target",
                        self._target_soc, display_action, soc,
                    )
                    self._target_soc = 0

        self._state.watchdog_s = self._command_duration_s
        cmd = BatteryCommand(power_watts=watts)

        # Acquire Modbus lock for the entire send + revert-timer write
        # to prevent interleaving with poller reads.
        async with self._modbus_lock:
            # Set the library's rating here, under the lock, not before waiting
            # for it: the poller reconnects under the same lock, and the
            # library's connect() rediscovers ratings (nameplate, or 5000 W when
            # M702 can't be read). Set earlier, a reconnect in between could
            # leave the rating below ``watts``, and franklinwh-modbus <= 0.9.5
            # clamps an over-limit request in the OPPOSITE direction (#230,
            # franklinwh-modbus #28): an over-rating charge became a full-rate
            # discharge.
            if rated_attr is not None:
                setattr(self._controller, rated_attr, max_w)
            # No duration_s, so no library timer (#242): franklinwh-modbus runs
            # it on a threading.Timer that calls reset_control_state() without
            # this gateway's Modbus lock (interleaving with a poll) and releases
            # the command a second time, unaudited. The software watchdog
            # releases it, under the lock and in the Activity Log.
            success, msg = await asyncio.to_thread(
                self._io, "write", self._controller.send_command, cmd,
            )

            # Write WSetRvrtTms to keep the aGate in VPP (remote-control) mode.
            # The aGate counts WSetRvrtRem down but doesn't revert at 0 (PICS
            # Issue 4), so this isn't a safety timer; writing WSetRvrtTms > 0
            # is still required to lock out mobile-app overrides.
            # For indefinite commands (duration=0), use max value to keep VPP locked.
            if success:
                rvrt_s = (self._state.watchdog_s if self._state.watchdog_s > 0
                          else OPEN_ENDED_RVRT_S)
                await asyncio.to_thread(self._write_revert_timer, rvrt_s)

        # Read the setpoint back. The library's own "verify" step only logs it
        # (design P2), so an accepted write is not evidence WSetEna is 1.
        hw = await self._read_hw_state()
        if success and hw is not None and hw.get("wset_enabled") not in (None, 1):
            success = False
            msg = f"setpoint not enabled after send ({_setpoint_text(hw)})"

        if not success:
            await self._dispatch_failed(display_action, report_w, msg, hw, prev_active,
                                        power_label=power_label)
            return

        if self._active_dispatch_id:
            await dispatch_outcomes.update(
                self._db, self._active_dispatch_id,
                "superseded", reason=f"replaced by {display_action} {power_label}")
        self._active_dispatch_id = await dispatch_outcomes.create(
            self._db, gateway_id=self._gateway_id, requester=self._requester,
            action=display_action, requested_w=report_w, state="sent", reason=msg)
        self._active_requester = self._requester
        self._last_dispatch = DispatchResult(
            ok=True, reason=msg, action=display_action, requested_w=report_w,
            dispatch_id=self._active_dispatch_id, hw=hw, ts=time.time())
        # Hardware WSet/WSetPct: + discharge, - charge (BatteryCommand: - discharge).
        # franklinwh-modbus 0.10.1+ sends watts (WSetMod=1, WSet), even for a %
        # command, whose watts come from the rating.
        self._sent_w = float(-watts)
        if pct_cmd:
            self._sent_pct = float(pct_cmd if watts < 0 else -pct_cmd)
        else:
            rated = self._max_charge_w if watts > 0 else self._max_discharge_w
            self._sent_pct = (-watts / rated * 100.0) if rated else None
        self._last_change_ts = time.time()
        self._external = None
        self._external_change = False

        self._state.active = True
        self._state.action = display_action
        self._state.power_w = abs(watts)
        self._state.power_pct = pct_cmd
        self._state.started_at = time.time()
        # The dispatch path (M704) is not the one the SPAN sentence is attached
        # to, but route it through the same filter anyway — a message the user
        # reads should not depend on which code path produced it.
        self._state.last_result = _strip_speculation(msg)
        self._state.last_success = success

        # Audit a superseded dispatch — a new command replacing a *different*
        # active one means the previous dispatch was stopped by this one.
        if prev_active and prev_action and prev_action != display_action:
            await self._log_event(
                "command_superseded",
                action=prev_action,
                power_w=prev_power,
                detail=f"superseded by {display_action} {abs(watts)}W",
            )
            logger.info(
                "Battery dispatch superseded: %s → %s", prev_action, display_action
            )

        self._start_watchdog()
        await self._persist_state()
        limits = []
        if self._state.watchdog_s > 0:
            limits.append(f"time limit {self._state.watchdog_s}s")
        else:
            # Name the aGate's timer, so a reader of "no time limit" doesn't
            # assume nothing was written, nor that the aGate will stop it.
            limits.append(
                f"no time limit; aGate revert timer {OPEN_ENDED_RVRT_S}s,"
                " not enforced by the aGate (PICS Issue 4)"
            )
        if self._target_soc > 0:
            limits.append(f"target SoC {self._target_soc}%")
        await self._log_event(
            "command_sent", action=display_action, power_w=abs(report_w),
            detail=f"{msg} [{', '.join(limits)}]", hw_state=hw, by=self._requester,
        )

        if self._on_state_changed:
            await self._on_state_changed()

        logger.info("Battery command: %s %s — %s", display_action, power_label, msg)

    #: Ignore samples this long after our own send/release, so a poll that
    #: still shows the previous state isn't mistaken for someone else's dispatch.
    EXTERNAL_SETTLE_S = 30.0
    #: WSetPct difference (percentage points) that counts as a different setpoint.
    EXTERNAL_PCT_TOLERANCE = 2.0
    #: WSet difference that counts as a different setpoint: 2 % of the rating,
    #: at least this many watts.
    EXTERNAL_W_TOLERANCE = 100.0

    async def observe_sample(self, sample: Any) -> None:
        """Notice a battery dispatch this bridge did not send.

        On 2026-10-12 a 1500 W dispatch from another tool ran for about a
        minute (20:40) and this bridge logged nothing; only the undocumented
        register watch noticed. Compares each poll's setpoint with what we sent,
        in the form the aGate holds it (WSetMod 1: WSet watts; 0: WSetPct), and
        logs the start, end, or a change made underneath ours.
        """
        pts = getattr(sample, "points", None) or {}
        try:
            await self.rating.observe(pts, getattr(sample, "ts", None))
        except Exception:
            logger.exception("Battery rating update failed")
        ena, pct = pts.get("wset_enabled"), pts.get("wset_pct")
        ts = float(getattr(sample, "ts", 0) or 0)
        if ena is None or ts - self._last_change_ts < self.EXTERNAL_SETTLE_S:
            return
        mode, wset = pts.get("wset_mode"), pts.get("wset_watts")
        hw = {"wset_enabled": ena, "wset_mode": mode, "wset_watts": wset, "wset_pct": pct}
        setpoint = _setpoint_text(hw)
        if int(ena) == 1 and not self._state.active:
            if self._external is None:
                self._external = {"since": ts, "pct": pct, "watts": wset}
                logger.warning(
                    "Gateway %s: battery dispatch detected that this bridge did not send "
                    "(%s, battery %s W) — another tool, the app or the cloud",
                    self._gateway_id, setpoint, pts.get("battery_power_w"))
                await self._log_event(
                    "external_dispatch_started", power_w=int(abs(pts.get("battery_power_w") or 0)),
                    detail=f"{setpoint} — not sent by this bridge", hw_state=hw, by="external")
            return
        if int(ena) == 0 and self._external is not None:
            dur = ts - self._external["since"]
            logger.info("Gateway %s: external battery dispatch ended after %.0fs",
                        self._gateway_id, dur)
            await self._log_event(
                "external_dispatch_ended", detail=f"after {dur:.0f}s", hw_state=hw, by="external")
            self._external = None
            return
        changed, sent = None, ""
        if int(ena) == 1 and self._state.active:
            if mode is not None and int(mode) == 1:
                if wset is not None and self._sent_w is not None:
                    rated = max(self._max_charge_w or 0, self._max_discharge_w or 0)
                    tol = max(self.EXTERNAL_W_TOLERANCE, 0.02 * rated)
                    changed = abs(float(wset) - self._sent_w) > tol
                    sent = f"sent {self._sent_w:.0f} W"
            elif pct is not None and self._sent_pct is not None:
                changed = abs(float(pct) - self._sent_pct) > self.EXTERNAL_PCT_TOLERANCE
                sent = f"sent {self._sent_pct:.0f}%"
        if changed is not None:
            if changed and not self._external_change:
                self._external_change = True
                logger.warning(
                    "Gateway %s: battery setpoint changed outside this bridge (%s, %s)",
                    self._gateway_id, setpoint, sent)
                await self._log_event(
                    "external_change", action=self._state.action,
                    detail=f"{setpoint} ({sent})", hw_state=hw, by="external")
            elif not changed:
                self._external_change = False

    async def _dispatch_failed(
        self, display_action: str, watts: int, msg: str, hw: dict | None, prev_active: bool,
        power_label: str = "",
    ) -> None:
        """A battery command that did not take: record it as failed, not active.

        The library's send starts with WSetEna=0 (design P1), so a failure
        partway can also have stopped a previously running command. The state
        follows what the gateway reports, not what was asked for.
        """
        still_enabled = bool(hw and hw.get("wset_enabled") == 1)
        if prev_active and not still_enabled:
            self._cancel_watchdog()
        self._state.active = bool(prev_active and still_enabled)
        if not self._state.active:
            self._state.action = ""
            self._state.power_w = 0
            self._state.power_pct = 0
            if self._active_dispatch_id:
                await dispatch_outcomes.update(
                    self._db, self._active_dispatch_id, "superseded",
                    reason=f"stopped by a failed {display_action} send")
                self._active_dispatch_id = None
                self._active_requester = None
        msg = explain_command_failure(_strip_speculation(msg), stopped_previous=(
            prev_active and not self._state.active))
        self._state.last_result = msg
        self._state.last_success = False
        did = await dispatch_outcomes.create(
            self._db, gateway_id=self._gateway_id, requester=self._requester,
            action=display_action, requested_w=watts, state="failed", reason=msg)
        self._last_dispatch = DispatchResult(
            ok=False, reason=msg, action=display_action, requested_w=watts,
            dispatch_id=did, hw=hw, ts=time.time())
        self._last_change_ts = time.time()
        await self._persist_state()
        await self._log_event(
            "command_failed", action=display_action, power_w=abs(watts),
            detail=msg, hw_state=hw, by=self._requester,
        )
        if self._on_state_changed:
            await self._on_state_changed()
        logger.warning("Battery command FAILED: %s %s — %s", display_action,
                       power_label or f"{abs(watts)}W", msg)

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

        # Shutdown releases skip the library's standby-handshake ramp-down so a
        # multi-gateway shutdown doesn't block past the container's stop grace
        # period. In-session releases (user/watchdog/target-SoC) keep the clean
        # ramp-down (safer for an actively-dispatching battery).
        fast = reason == "shutdown"
        async with self._modbus_lock:
            success = await asyncio.to_thread(self._io, "write", self._full_release, fast)

        # Read back: a release is only done when WSetEna is 0 (design §5.1.5).
        hw = await self._read_hw_state()
        unverified = bool(success and hw is not None and hw.get("wset_enabled") == 1)
        if unverified:
            success = False
            logger.warning(
                "Battery release NOT verified: WSetEna still 1 after release (reason=%s)", reason)

        self._state.active = False
        self._state.action = ""
        self._state.power_w = 0
        self._state.power_pct = 0
        self._state.last_result = (
            "Release not verified (WSetEna still 1)" if unverified
            else "Released" if success else "Release failed"
        )
        self._state.last_success = success
        if self._active_dispatch_id:
            await dispatch_outcomes.update(
                self._db, self._active_dispatch_id,
                "released" if success else "release_unverified",
                reason=f"reason={reason}; {self._state.last_result}")
            self._active_dispatch_id = None
            self._active_requester = None

        self._sent_pct = None
        self._sent_w = None
        self._last_change_ts = time.time()

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
            hw_state=hw,
            by=(f"bridge:{reason}" if reason in ("watchdog_expired", "target_soc_reached",
                                                 "shutdown") else self._requester),
        )

        if self._on_state_changed:
            await self._on_state_changed()

        logger.info("Battery command released: %s (reason=%s)", self._state.last_result, reason)

    def _io(self, kind: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run one blocking aGate call under link-health measurement (Phase
        0b). Only counts: the result and any exception pass through as-is.
        A ``(False, msg)`` or ``False`` result is counted as an error."""
        with link_stats.track(self._gateway_id, kind, "command") as req:
            result = fn(*args, **kwargs)
            ok = result[0] if isinstance(result, tuple) and result else result
            if ok is False:
                req.fail(str(result[1]) if isinstance(result, tuple) and len(result) > 1
                         else "returned False")
            elif kind == "model_read":
                req.check(result)
            return result

    def _reset_control_state(self, fast: bool) -> bool:
        """Call the controller's release, requesting a fast (no-handshake)
        release when ``fast`` and the installed library supports it.

        Newer franklinwh-modbus performs a standby handshake (force 0W, settle,
        then WSetEna=0) via a ``handshake_wait_s`` parameter. We pass 0 only on
        fast paths, and only when the parameter exists — older versions (and
        mocks) are called with no argument, so this stays back-compatible.
        """
        fn = self._controller.reset_control_state
        if fast:
            try:
                if "handshake_wait_s" in inspect.signature(fn).parameters:
                    return fn(handshake_wait_s=0)
            except (TypeError, ValueError):
                pass  # un-introspectable (e.g. a bare Mock) → fall back
        return fn()

    def _full_release(self, fast: bool = False) -> bool:
        """Full release: clear setpoints, disable WSetEna, AND zero revert timer.

        The library's reset_control_state() only clears WSetEna/WSetPct/WSet.
        We must also zero WSetRvrtTms to exit VPP mode — the aGate stays in
        remote-control (VPP) mode as long as WSetRvrtTms > 0, blocking mode
        changes from the FranklinWH mobile app.
        """
        success = self._reset_control_state(fast)

        # Clear the hardware revert timer so the aGate exits VPP mode
        try:
            m704 = self._controller.get_model(704)
            if m704:
                with link_stats.track(self._gateway_id, "write", "command"):
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
        preventing the mobile app from overriding the dispatch.  The aGate
        counts WSetRvrtRem down but does not revert at 0 (PICS Issue 4), so
        the software watchdog is the real safety timer that releases the
        command.
        """
        try:
            m704 = self._controller.get_model(704)
            if m704:
                # Fresh read to avoid writing stale values — the poller may
                # have called m704.read() between send_command and now,
                # overwriting the in-memory model.
                with link_stats.track(self._gateway_id, "write", "command"):
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
                    success, msg = await asyncio.to_thread(self._io, "write", method, mode_val)
                self._state.last_result = _strip_speculation(msg)
                self._state.last_success = success
                if success:
                    self._mode_write_locked = False
                    logger.info("Operating mode set to %s (%d)", mode_name, mode_val)
                else:
                    # Only the read-back verdict proves a lock; a timeout or a
                    # short response says nothing about the register.
                    if "Hardware ignored write" in (msg or ""):
                        self._mode_write_locked = True
                    logger.error("Operating mode failed: %s", self._state.last_result)
            except Exception as exc:
                self._state.last_result = f"Mode change error: {exc}"
                self._state.last_success = False
                logger.error("Operating mode failed: %s", exc)

        await self._log_event(
            "mode_change", action=mode_name, detail=self._state.last_result,
            by=self._requester,
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
                    success, msg = await asyncio.to_thread(self._io, "write", method, pct)
                self._state.last_result = _strip_speculation(msg)
                self._state.last_success = success
                if success:
                    logger.info("%s reserve set to %d%%", reserve_type, pct)
                else:
                    logger.error("Reserve write failed: %s", self._state.last_result)
            except Exception as exc:
                self._state.last_result = f"Reserve write error: {exc}"
                self._state.last_success = False
                logger.error("Reserve write failed: %s", exc)

        await self._log_event(
            "reserve_change",
            action=f"{reserve_type}_reserve",
            power_w=pct,
            detail=self._state.last_result,
            by=self._requester,
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
                # A raised check must not end the watchdog: it is what
                # enforces the duration limit on a running force (#31).
                try:
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

                    if await self._overshoot_guard():
                        return

                    # Check target SoC
                    target = self._target_soc
                    if target > 0:
                        soc = self._read_soc()
                        if soc is not None:
                            is_charge = self._state.action in ("Charge", "Force Charge")
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
                except Exception:
                    logger.exception("Watchdog check failed; retrying next interval")

                await asyncio.sleep(SOC_CHECK_INTERVAL_S)
        except asyncio.CancelledError:
            pass

    def _record_not_sent(self, action: str, reason: str, requested_w: int = 0) -> None:
        """A battery command that was NOT sent must not leave an earlier "ok" behind.

        Callers (the scheduler) judge a dispatch by ``last_dispatch``. A refusal
        that returned without replacing it let the previous command's ok stand,
        so a refused schedule was audited "ok — dispatched" (0.2.8 regression).
        """
        self._last_dispatch = DispatchResult(
            ok=False, reason=reason, action=action, requested_w=requested_w, ts=time.time())

    async def _overshoot_guard(self) -> bool:
        """Release a force command whose battery runs well above the request (#133).

        A rating that is too low makes the aGate apply a larger share of its
        real power than asked for. Returns True when it released the command.
        """
        st = self._state
        if not st.active or st.power_w <= 0:
            return False
        if time.time() - st.started_at < OVERSHOOT_CHECK_AFTER_S:
            return False
        is_charge = st.action in ("Charge", "Force Charge")
        if not is_charge and st.action not in ("Discharge", "Force Discharge"):
            return False
        try:
            bw = float((self._points_getter() if self._points_getter else {})
                       .get("battery_power_w"))
        except (TypeError, ValueError):
            return False
        actual = -bw if is_charge else bw  # telemetry: + discharge, - charge
        if actual <= st.power_w * OVERSHOOT_FACTOR + OVERSHOOT_SLACK_W:
            return False
        direction = "charge" if is_charge else "discharge"
        reason = (f"battery ran at {actual:.0f} W for a {st.power_w} W force {direction}: "
                  f"the power rating it was sized against is wrong")
        await self.rating.contradict(direction, reason)
        await self._release_command(reason="overshoot")
        self._state.last_result = f"Released: {reason}. Recalibrate before the next command"
        self._state.last_success = False
        if self._on_state_changed:
            await self._on_state_changed()
        return True

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
