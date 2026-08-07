"""Destructive live E2E: v2 engine dispatch → exit-condition release, verified
on a REAL aGate through the REAL CommandHandler.

Exercises slice-1's exit-criteria path end to end at the hardware boundary:
  tick 1 → engine dispatches ``Force Standby`` (0 W, benign) via the real handler
           → the aGate enters VPP/remote control
  tick 2 → the exit condition (SOC ≤ live_SOC+5, already satisfied) fires
           → engine issues ``Release`` → the aGate returns to native control

WRITES to the battery (a brief 0 W standby, then release). Gated behind BOTH
``hardware`` AND ``destructive``. The ``finally`` block ALWAYS force-releases
regardless of outcome — hardware auto-revert is cosmetic on this device
(docs/vendor-issues.md Issue 1), so we never rely on it.

    pytest tests/hardware/test_scheduler_engine_dispatch_live.py -v -m destructive -s
"""

import asyncio
import contextlib
from datetime import datetime, timedelta

import pytest

from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import init_db

pytestmark = [pytest.mark.hardware, pytest.mark.destructive]

_ALLDAY = {"windows": [{"start": "00:00", "end": "23:59"}]}


def _wset_enabled(hw: dict):
    """Control-status 'is a VPP command engaged?' flag, tolerant of key name."""
    return hw.get("wset_enabled", hw.get("wset_ena"))


async def test_engine_dispatch_and_exit_release_live(controller, tmp_path):
    db = await init_db(tmp_path / "sched.db")
    handler = CommandHandler(controller, db, modbus_lock=asyncio.Lock())

    soc = (await asyncio.to_thread(controller.read_battery_status))["soc"]
    print(f"\nlive SOC = {soc}")
    points = {"soc": soc}  # minimal snapshot; SOC drives the exit condition

    audits: list[str] = []

    async def on_audit(sid, action, target, result, detail):
        audits.append(result)
        print(f"  audit: {result} — {detail}")

    engine = ScheduleEngine(
        db=None,
        resolver=lambda tt, tid: [("default", handler)],
        on_audit=on_audit,
        points_fn=lambda gw: points,
    )
    # force_standby (0 W) in an always-open window, with an exit condition the
    # live SOC already satisfies so it fires on the tick after dispatch.
    engine._entries = [{
        "id": "live", "name": "live-standby", "enabled": True,
        "when_spec": _ALLDAY, "action": "force_standby", "params": {},
        "target_type": "gateway", "target_id": "default",
        "release": "release", "conflict": "defer", "priority": 0,
        "entry_conditions": None,
        "exit_conditions": {"conditions": [
            {"sensor": "battery.soc_pct", "op": "<=", "value": soc + 5}]},
    }]

    now = datetime.now()
    try:
        # ── tick 1: engine dispatches Force Standby via the real handler ──
        await engine.tick(now)
        assert handler.state.action == "Force Standby"
        assert handler.state.last_success, (
            f"standby dispatch did not succeed: {handler.state.last_result!r}"
        )
        hw = await asyncio.to_thread(controller.read_control_status)
        print(f"after dispatch: wset_enabled={_wset_enabled(hw)} "
              f"result={handler.state.last_result!r}")
        assert _wset_enabled(hw), "aGate did not enter VPP control after dispatch"

        # ── tick 2: exit condition met → engine issues Release ──
        await engine.tick(now)
        assert "exit_condition_met" in audits, "exit criteria did not fire"
        assert handler.state.active is False

        # ── verify the aGate actually returned to native (allow ramp-down) ──
        released = False
        for _ in range(10):
            hw2 = await asyncio.to_thread(controller.read_control_status)
            if not _wset_enabled(hw2):
                released = True
                break
            await asyncio.sleep(1)
        assert released, "aGate did not release after the exit condition fired"
        print("after exit: aGate released to native ✓")
    finally:
        # Always force-release — never leave a dispatch armed on exit.
        with contextlib.suppress(Exception):
            await handler.stop()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(controller.reset_control_state)
        with contextlib.suppress(Exception):
            m704 = controller.get_model(704)
            if m704:
                m704.read()
                m704.WSetRvrtTms.value = 0
                m704.WSetEnaRvrt.value = 0
                m704.write()
        await db.close()


async def test_engine_restore_prior_mode_live(controller, tmp_path):
    """Slice-2 restore_prior_mode round-trip on real hardware.

    Uses a NO-OP restore (prior mode == current mode) so the aGate's mode is
    never actually changed — this verifies the mechanism (capture mode at
    dispatch → set_native_mode(prior) on exit succeeds) without altering the
    user's configured mode.
    """
    db = await init_db(tmp_path / "sched2.db")
    handler = CommandHandler(controller, db, modbus_lock=asyncio.Lock())

    soc = (await asyncio.to_thread(controller.read_battery_status))["soc"]
    mode0 = await asyncio.to_thread(controller.read_native_mode)
    mode_name = mode0.get("mode_name")
    mode_raw = mode0.get("mode_raw")
    print(f"\nlive SOC={soc} mode={mode_name!r} (raw {mode_raw})")
    assert mode_name, "aGate did not report mode_name"
    points = {"soc": soc, "mode_name": mode_name}

    audits: list[str] = []

    async def on_audit(sid, action, target, result, detail):
        audits.append(result)

    engine = ScheduleEngine(
        db=None,
        resolver=lambda tt, tid: [("default", handler)],
        on_audit=on_audit,
        points_fn=lambda gw: points,
    )
    engine._entries = [{
        "id": "live", "name": "live-restore", "enabled": True,
        "when_spec": _ALLDAY, "action": "force_standby", "params": {},
        "target_type": "gateway", "target_id": "default",
        "release": "release", "conflict": "defer", "priority": 0,
        "entry_conditions": None,
        "exit_conditions": {"conditions": [
            {"sensor": "battery.soc_pct", "op": "<=", "value": soc + 5}]},
        "release_policy": "restore_prior_mode",
    }]

    now = datetime.now()
    try:
        await engine.tick(now)  # dispatch standby, capture prior mode
        assert handler.state.action == "Force Standby"
        await engine.tick(now)  # exit → Release, then restore prior mode
        assert "exit_condition_met" in audits
        # last command was the mode re-assert — confirm it succeeded on hardware
        assert handler.state.last_success, (
            f"mode restore failed on hardware: {handler.state.last_result!r}"
        )

        released = False
        for _ in range(10):
            hw = await asyncio.to_thread(controller.read_control_status)
            if not _wset_enabled(hw):
                released = True
                break
            await asyncio.sleep(1)
        assert released, "aGate did not release after exit"

        after = (await asyncio.to_thread(controller.read_native_mode)).get("mode_name")
        print(f"after restore: mode={after!r}")
        assert after == mode_name, f"mode not restored: {mode_name!r} -> {after!r}"
        print("restore_prior_mode round-trip verified ✓")
    finally:
        with contextlib.suppress(Exception):
            await handler.stop()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(controller.reset_control_state)
        with contextlib.suppress(Exception):
            if mode_raw is not None:
                await asyncio.to_thread(controller.set_native_mode, mode_raw)
        with contextlib.suppress(Exception):
            m704 = controller.get_model(704)
            if m704:
                m704.read()
                m704.WSetRvrtTms.value = 0
                m704.WSetEnaRvrt.value = 0
                m704.write()
        await db.close()


async def test_oneoff_trigger_duration_release_live(controller, tmp_path):
    """Slice-3 fire-based trigger + duration on real hardware.

    A oneoff trigger fires → dispatch Force Standby (0 W); after the duration
    elapses (simulated by passing an advanced `now` to tick — no real waiting)
    the window exit releases with restore_prior_mode. Verifies fire activation,
    duration bound, and v2 window-exit restore end to end on the aGate.
    """
    db = await init_db(tmp_path / "sched3.db")
    handler = CommandHandler(controller, db, modbus_lock=asyncio.Lock())

    soc = (await asyncio.to_thread(controller.read_battery_status))["soc"]
    mode0 = await asyncio.to_thread(controller.read_native_mode)
    mode_name = mode0.get("mode_name")
    mode_raw = mode0.get("mode_raw")
    print(f"\nlive SOC={soc} mode={mode_name!r}")
    points = {"soc": soc, "mode_name": mode_name}

    audits: list[str] = []

    async def on_audit(sid, action, target, result, detail):
        audits.append(result)

    engine = ScheduleEngine(
        db=None,
        resolver=lambda tt, tid: [("default", handler)],
        on_audit=on_audit,
        points_fn=lambda gw: points,
    )
    t0 = datetime.now().replace(second=0, microsecond=0)  # minute-aligned fire
    engine._entries = [{
        "id": "live", "name": "live-oneoff", "enabled": True,
        "when_spec": {}, "action": "force_standby", "params": {},
        "target_type": "gateway", "target_id": "default",
        "release": "release", "conflict": "defer", "priority": 0,
        "entry_conditions": None, "exit_conditions": None,
        "trigger_kind": "oneoff", "trigger_spec": {"fire_at": t0.isoformat()},
        "duration_s": 120, "release_policy": "restore_prior_mode",
    }]

    try:
        await engine.tick(t0)  # trigger fires → dispatch standby
        assert handler.state.action == "Force Standby"
        hw = await asyncio.to_thread(controller.read_control_status)
        assert _wset_enabled(hw), "aGate did not engage on trigger fire"
        print("trigger fired → standby engaged ✓")

        # advance past the 120 s duration → duration-elapsed release + restore
        await engine.tick(t0 + timedelta(minutes=3))
        assert handler.state.last_success, (
            f"mode restore failed on hardware: {handler.state.last_result!r}"
        )
        released = False
        for _ in range(10):
            hw2 = await asyncio.to_thread(controller.read_control_status)
            if not _wset_enabled(hw2):
                released = True
                break
            await asyncio.sleep(1)
        assert released, "aGate did not release after duration elapsed"

        after = (await asyncio.to_thread(controller.read_native_mode)).get("mode_name")
        assert after == mode_name, f"mode not restored: {mode_name!r} -> {after!r}"
        print(f"duration elapsed → released + mode restored ({after}) ✓")
    finally:
        with contextlib.suppress(Exception):
            await handler.stop()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(controller.reset_control_state)
        with contextlib.suppress(Exception):
            if mode_raw is not None:
                await asyncio.to_thread(controller.set_native_mode, mode_raw)
        with contextlib.suppress(Exception):
            m704 = controller.get_model(704)
            if m704:
                m704.read()
                m704.WSetRvrtTms.value = 0
                m704.WSetEnaRvrt.value = 0
                m704.write()
        await db.close()
