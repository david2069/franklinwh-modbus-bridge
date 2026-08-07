"""Live hardware check: the v2 engine's entry gate over a REAL aGate snapshot.

Verifies the new decision path end-to-end against live sensor values —
real aGate → poll → sample.points → engine snapshot → entry-condition gate —
WITHOUT writing to the battery. The engine dispatches to a SPY command handler,
not the real one, so no Modbus write is ever issued. (The dispatch/release
mechanics themselves are unchanged SCH1 behaviour, already hardware-proven.)

Read-only / non-destructive.

Run:
    pytest tests/hardware/test_scheduler_engine_live.py -m "hardware and not destructive" -v -s
"""

import asyncio
import contextlib
import os
from datetime import datetime

import pytest
from franklinwh_modbus import FranklinWHController

from franklinwh_bridge.gateway.scheduler import ScheduleEngine
from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus

pytestmark = pytest.mark.hardware

# All-day window so "now" is inside it regardless of when the test runs.
_ALLDAY = {"windows": [{"start": "00:00", "end": "23:59"}]}


class SpyHandler:
    """Records commands; never touches hardware. Mirrors CommandHandler.state."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

        class _S:
            active = False
            action = ""

        self.state = _S()

    async def handle_command(self, slug: str, value: str) -> None:
        self.calls.append((slug, value))
        if slug == "battery_command":
            if value in ("Release", "Not Active", "Stop"):
                self.state.active = False
                self.state.action = ""
            else:
                base = value[6:] if value.lower().startswith("force ") else value
                self.state.active = True
                self.state.action = f"Force {base}"


def _entry(**kw):
    base = {
        "id": "live", "name": "live", "enabled": True,
        "when_spec": _ALLDAY, "action": "force_standby", "params": {},
        "target_type": "gateway", "target_id": "default",
        "release": "release", "conflict": "defer", "priority": 0,
        "entry_conditions": None, "exit_conditions": None,
    }
    base.update(kw)
    return base


async def _live_points():
    host = os.environ.get("FRANKLINWH_TEST_HOST", "192.168.1.100")
    port = int(os.environ.get("FRANKLINWH_TEST_PORT", "502"))
    unit = int(os.environ.get("FRANKLINWH_TEST_UNIT", "1"))
    ctrl = FranklinWHController(host, port=port, unit_id=unit)
    poller = ModbusPoller(ctrl, SampleBus(), gateway_id="default")
    try:
        try:
            sample = await poller.poll_once()
        except Exception as exc:
            pytest.skip(f"Cannot poll aGate at {host}:{port}: {exc}")
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(ctrl.disconnect)
    return sample.points


def _engine(entries, spy, points, audits):
    async def on_audit(sid, action, target, result, detail):
        audits.append({"result": result, "detail": detail})

    eng = ScheduleEngine(
        db=None,
        resolver=lambda tt, tid: [("default", spy)],
        on_audit=on_audit,
        points_fn=lambda gw: points,
    )
    eng._entries = entries
    return eng


async def test_entry_gate_decides_on_live_snapshot():
    points = await _live_points()
    soc = points.get("soc")
    print(f"\nlive SOC = {soc!r}")
    assert soc is not None, "live sample missing 'soc' — sensor mapping would be wrong"
    now = datetime.now()

    # Gate FAILS on an impossible live condition → no dispatch, audited gated.
    spy_fail, audits_fail = SpyHandler(), []
    e_fail = _entry(entry_conditions={"conditions": [
        {"sensor": "battery.soc_pct", "op": ">", "value": 200}]})
    await _engine([e_fail], spy_fail, points, audits_fail).tick(now)
    assert not any(c[0] == "battery_command" for c in spy_fail.calls), \
        "gate should have blocked dispatch"
    assert any(a["result"] == "gated" for a in audits_fail)
    print("gate FAIL path: no dispatch, gated audit recorded ✓")

    # Gate PASSES on the live SOC (>=0) → dispatches to the SPY (not the battery).
    spy_pass, audits_pass = SpyHandler(), []
    e_pass = _entry(entry_conditions={"conditions": [
        {"sensor": "battery.soc_pct", "op": ">=", "value": 0}]})
    await _engine([e_pass], spy_pass, points, audits_pass).tick(now)
    assert ("battery_command", "Force Standby") in spy_pass.calls, \
        "gate should have opened on the live SOC"
    print("gate PASS path: dispatched to spy on live SOC ✓ (no battery write)")
