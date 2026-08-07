"""Live hardware check: scheduler sensor registry over a REAL aGate sample.

Read-only (a single poll, no register writes). Exercises the exact path the
scheduler engine will use — real aGate → ModbusPoller → ``sample.points`` →
``scheduler_sensors.snapshot`` — to confirm the registry's canonical point-key
assumptions hold against real hardware, not just the mock. A wrong key name
would surface here as a critical sensor reading ``None``.

Run:
    pytest tests/hardware/test_scheduler_sensors_live.py -m "hardware and not destructive" -v -s

Uses its own controller (not the shared session fixture) so there is exactly one
Modbus session against the single-session aGate.
"""

import asyncio
import contextlib
import os

import pytest
from franklinwh_modbus import FranklinWHController

from franklinwh_bridge.gateway.scheduler_sensors import snapshot
from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus

pytestmark = pytest.mark.hardware

# Sensors that MUST resolve from a healthy real sample (price/time excluded:
# price is a stub, time comes from the clock not the sample).
CRITICAL_SENSORS = [
    "battery.soc_pct",
    "battery.power_w",
    "solar.power_w",
    "grid.power_w",
    "load.power_w",
    "mode.name",
    "grid.connected",
]


async def test_snapshot_resolves_over_live_sample():
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

    points = sample.points
    snap = snapshot(points)

    # Visibility (run with -s): the real published key set + resolved snapshot.
    print("\n--- real sample.points keys ---")
    print(sorted(points))
    print("--- scheduler snapshot ---")
    for k in sorted(snap):
        print(f"  {k:28} = {snap[k]!r}")

    missing = [s for s in CRITICAL_SENSORS if snap.get(s) is None]
    assert not missing, (
        f"critical sensors read None from a real sample: {missing}; "
        f"published keys were {sorted(points)}"
    )

    # Sanity on the resolved values.
    assert 0 <= snap["battery.soc_pct"] <= 100
    assert isinstance(snap["mode.name"], str) and snap["mode.name"]
    assert isinstance(snap["grid.connected"], bool)
    assert isinstance(snap["pv.is_generating"], bool)  # derived from solar, must resolve
