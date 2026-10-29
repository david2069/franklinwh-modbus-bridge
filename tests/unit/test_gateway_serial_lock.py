"""A gateway is locked to its aGate's serial, one serial per gateway (#296 item 42).

Owner's rule: the internal id is the identity, the name is only a label, and
the gateway is permanently linked to one serial, 1:1. Until now the serial was
an optional field overwritten by whatever answered at the gateway's address,
and two gateways could hold the same one.
"""

from __future__ import annotations

import logging

import pytest

from franklinwh_bridge.gateway.instance import (
    GatewayConfig,
    GatewayInstance,
    refuse_connects,
)
from franklinwh_bridge.modbus.sample import Sample, SampleBus
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.store.db import create_gateway, get_gateway, init_db, update_gateway

A, B = "AG0000000001", "AG0000000002"


class _Ctrl:
    def __init__(self, serials):
        self.serials = list(serials)  # one per nameplate read; the last repeats
        self.connects = 0
        self.disconnects = 0

    def connect(self):
        self.connects += 1
        return True

    def reconnect(self):
        return self.connect()

    def disconnect(self):
        self.disconnects += 1

    def read_nameplate(self):
        s = self.serials.pop(0) if len(self.serials) > 1 else self.serials[0]
        return {"serial": s, "model": "aGate X", "version": "V12R02B60D00"}


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "s.db")
    await create_gateway(conn, "gw", "aGate", "192.0.2.10")
    await create_gateway(conn, "other", "aGate 2", "192.0.2.11")
    yield conn
    await conn.close()


def gateway(db, *serials):
    g = GatewayInstance(GatewayConfig(gateway_id="gw", name="aGate", host="192.0.2.10"),
                        db=db, global_bus=SampleBus())
    g.controller = _Ctrl(serials)
    refuse_connects(g.controller, lambda: g.status.serial_refused)
    g.command_handler = CommandHandler(g.controller, db, gateway_id="gw")
    return g


async def test_first_serial_reported_is_stored(db):
    g = gateway(db, A)
    await g._discover_device()
    assert (await get_gateway(db, "gw"))["serial"] == A
    assert g.status.serial_refused is None


async def test_same_serial_again_is_accepted(db):
    await update_gateway(db, "gw", serial=A)
    g = gateway(db, A)
    await g._discover_device()
    assert g.status.serial == A
    assert g.status.serial_refused is None


async def test_a_different_aGate_at_the_address_is_refused_and_logged(db, caplog):
    await update_gateway(db, "gw", serial=A)
    g = gateway(db, B)
    with caplog.at_level(logging.ERROR):
        await g._discover_device()
    reason = g.status.serial_refused
    assert reason == ("the aGate at 192.0.2.10:502 reports serial AG0000000002, "
                      "but this gateway is locked to serial AG0000000001")
    assert f"Gateway gw refused: {reason}" in caplog.text
    assert g.status.last_error == f"Refused: {reason}"
    assert (await get_gateway(db, "gw"))["serial"] == A  # the lock is kept
    assert g.status.serial is None and g.device_info is None


async def test_one_bad_read_is_read_again_before_refusing(db):
    await update_gateway(db, "gw", serial=A)
    g = gateway(db, "AG000000000?", A)
    await g._discover_device()
    assert g.status.serial_refused is None
    assert g.status.serial == A


async def test_a_serial_another_gateway_holds_is_refused(db):
    await update_gateway(db, "other", serial=A)
    g = gateway(db, A)
    await g._discover_device()
    assert g.status.serial_refused == "serial AG0000000001 already belongs to gateway 'other'"
    assert not (await get_gateway(db, "gw"))["serial"]


async def test_a_refused_gateway_takes_no_commands(db):
    await update_gateway(db, "gw", serial=A)
    g = gateway(db, B)
    await g._discover_device()
    out = await g.command_handler.handle_command("battery_command", "Force Charge",
                                                 requester="user:david")
    assert out == {"ok": False, "result": f"Refused: {g.status.serial_refused}"}


async def test_a_refused_gateway_cannot_connect_again(db):
    await update_gateway(db, "gw", serial=A)
    g = gateway(db, B)
    await g._discover_device()
    before = g.controller.connects
    assert g.controller.connect() is False
    assert g.controller.reconnect() is False
    assert g.controller.connects == before


async def test_start_up_stops_before_writing_to_a_refused_aGate(db, monkeypatch):
    await update_gateway(db, "gw", serial=A)
    g = gateway(db, B)
    steps = []

    async def mark(name):
        steps.append(name)

    monkeypatch.setattr(g, "_log_startup_state", lambda: mark("startup_state"))
    monkeypatch.setattr(g, "_release_stale_commands", lambda: mark("release"))
    await g._init_and_poll()
    assert steps == []  # no read of control state, no Release written
    assert g.controller.disconnects == 1
    assert g.status.polling is False and g.status.connected is False


async def test_a_reconnect_to_a_different_aGate_stops_polling(db):
    await update_gateway(db, "gw", serial=A)
    g = gateway(db, A)
    await g._discover_device()

    class _Poller:
        stopped = False

        async def stop(self):
            self.stopped = True

    g.poller = _Poller()
    g.status.polling = True
    g.controller.serials = [B]  # the address now answers with another aGate
    g._note_reconnect()
    await g._rediscover_if_due(Sample.now("gw", {}))
    await g._rediscover_task
    assert g.poller.stopped is True
    assert g.status.polling is False
    assert "locked to serial AG0000000001" in g.status.serial_refused


async def test_the_refusal_is_in_the_gateway_list(db):
    await update_gateway(db, "gw", serial=A)
    g = gateway(db, B)
    await g._discover_device()
    assert g.to_dict()["serial_refused"] == g.status.serial_refused
