"""#307: the nameplate (firmware) is re-read after a reconnect, not only at start.

An aGate firmware update reboots the device; the bridge reconnects and kept
reporting the old firmware until it was restarted.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from franklinwh_bridge.gateway import instance as inst_mod
from franklinwh_bridge.gateway.instance import (
    GatewayConfig,
    GatewayInstance,
    note_reconnects,
)
from franklinwh_bridge.modbus.sample import Sample, SampleBus


class _Ctrl:
    def __init__(self):
        self.version = "V10R01B04D00"
        self.connect_result = True

    def connect(self):
        return self.connect_result

    def reconnect(self):
        return self.connect_result

    def read_nameplate(self):
        return {"serial": "<serial8>", "model": "aGate X", "version": self.version}


def test_callback_after_each_successful_connect_or_reconnect():
    calls = []
    c = _Ctrl()
    note_reconnects(c, lambda: calls.append(1))
    note_reconnects(c, lambda: calls.append(2))  # idempotent: no double wrap
    c.connect()
    c.reconnect()
    c.connect_result = False
    c.connect()  # failed: no callback
    assert calls == [1, 1]


@pytest.fixture
def gw(monkeypatch):
    updates = []

    async def fake_update(db, gateway_id, **kw):
        updates.append(kw)

    async def no_row(db, gateway_id):
        return None

    async def no_rows(db):
        return []

    monkeypatch.setattr(inst_mod, "update_gateway", fake_update)
    monkeypatch.setattr(inst_mod, "get_gateway", no_row)
    monkeypatch.setattr(inst_mod, "get_gateways", no_rows)
    g = GatewayInstance(GatewayConfig(gateway_id="gw", name="GW", host="192.0.2.1"),
                        db=None, global_bus=SampleBus())
    g.controller = _Ctrl()
    note_reconnects(g.controller, g._note_reconnect)
    g.updates = updates
    return g


async def _sample(g):
    await g._rediscover_if_due(Sample.now("gw", {}))
    if g._rediscover_task is not None:
        await g._rediscover_task


async def test_start_up_connect_does_not_trigger_a_second_read(gw):
    gw.controller.connect()  # before discovery: start-up reads it anyway
    await _sample(gw)
    assert gw.updates == []


async def test_firmware_follows_a_reconnect_and_the_change_is_logged(gw, caplog):
    await gw._discover_device()
    assert gw.status.firmware == "V10R01B04D00"

    gw.controller.version = "V12R02B60D00"  # the aGate updated and rebooted
    await _sample(gw)  # no reconnect yet: nothing re-read
    assert gw.status.firmware == "V10R01B04D00"

    with caplog.at_level(logging.WARNING):
        gw.controller.reconnect()
        await _sample(gw)
    assert gw.status.firmware == "V12R02B60D00"
    assert gw.device_info.firmware == "V12R02B60D00"
    assert gw.updates[-1]["firmware"] == "V12R02B60D00"
    assert "firmware changed V10R01B04D00 -> V12R02B60D00" in caplog.text

    n = len(gw.updates)
    await _sample(gw)  # once per reconnect, not every sample
    assert len(gw.updates) == n


async def test_the_reread_waits_for_the_modbus_lock(gw):
    await gw._discover_device()
    gw.controller.version = "V12R02B60D00"
    async with gw.modbus_lock:
        gw.controller.reconnect()
        await gw._rediscover_if_due(Sample.now("gw", {}))
        await asyncio.sleep(0)
        assert gw.status.firmware == "V10R01B04D00"  # held off by the poll
    await gw._rediscover_task
    assert gw.status.firmware == "V12R02B60D00"
