"""A real gateway's start() must call the library with only its own arguments.

Regression for the #35 deploy (2026-10-09): ``home_load_source`` was passed to
FranklinWHController instead of ModbusPoller, so the real aGate failed to start
with "unexpected keyword argument" while every unit test (which mocks the
controller) passed.
"""

from __future__ import annotations

import asyncio
import sys
import types

from franklinwh_bridge.gateway.instance import GatewayConfig, GatewayInstance
from franklinwh_bridge.modbus.sample import SampleBus


class _StrictController:
    """Accepts exactly the arguments franklinwh-modbus 0.9.5's controller takes."""

    def __init__(self, ip_address, port=502, unit_id=1, timeout=10.0):
        self.ip_address = ip_address

    def connect(self):
        return False  # never reach a network


async def test_real_gateway_start_passes_only_controller_args(monkeypatch):
    fake = types.ModuleType("franklinwh_modbus")
    fake.FranklinWHController = _StrictController
    monkeypatch.setitem(sys.modules, "franklinwh_modbus", fake)

    cfg = GatewayConfig(gateway_id="gw", name="GW", host="192.0.2.1",
                        home_load_source="high_res")
    inst = GatewayInstance(cfg, db=None, global_bus=SampleBus())
    await inst.start()  # raised TypeError before the fix
    try:
        assert isinstance(inst.controller, _StrictController)
        assert inst.poller._home_load_source() == "high_res"
        cfg.home_load_source = "standard"  # live change reaches the poller
        assert inst.poller._home_load_source() == "standard"
    finally:
        task = getattr(inst, "_init_task", None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
