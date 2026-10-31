"""#426: vendor registers (15000-15039, 15510-15513, 16000-16002) are read over the
library's connection, not a second TCP connection of the bridge's own.

An aGate that serves one Modbus client refused the second connection, and the reads
failed with only a DEBUG line: 16001 (the Emergency Backup reserve, #311), 16000
(home load hi-res) and the PV energy counters were simply missing.
"""

from __future__ import annotations

import logging
import struct
from unittest.mock import MagicMock

import pymodbus.client
import pytest

from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus

#: Captured at import, before the suite's autouse stub (conftest) replaces it.
_REAL_READ_REGS = ModbusPoller._read_regs


def _poller(read):
    ctrl = MagicMock()
    ctrl.ip_address, ctrl.port, ctrl.unit_id = "192.0.2.10", 502, 1
    ctrl.dev.client.read.side_effect = read
    p = ModbusPoller(controller=ctrl, sample_bus=SampleBus(), gateway_id="gw")
    p._read_regs = _REAL_READ_REGS.__get__(p)
    return p, ctrl


def _regs(addr: int, count: int) -> bytes:
    return struct.pack(f">{count}H", *range(addr % 1000, addr % 1000 + count))


def test_vendor_reads_use_the_library_connection(monkeypatch):
    def second_connection(*a, **kw):
        raise AssertionError("a second Modbus TCP connection was opened")

    monkeypatch.setattr(pymodbus.client, "ModbusTcpClient", second_connection)
    p, ctrl = _poller(_regs)
    points: dict = {}
    p._read_vendor_registers(points)

    assert [c.args for c in ctrl.dev.client.read.call_args_list] == [
        (15000, 40), (15510, 4), (16000, 3)]
    assert points["vreg_16001"] == 1 and points["vreg_16000"] == 0
    assert points["vreg_15039"] == 39
    assert points["pv_energy_total_wh"] == (510 << 16) | 511


def test_a_failed_range_warns_once_and_the_others_still_read(caplog):
    fail = {"on": True}

    def read(addr, count):
        if addr == 16000 and fail["on"]:
            raise ConnectionError("refused")
        return _regs(addr, count)

    p, _ = _poller(read)
    caplog.set_level(logging.DEBUG, logger="franklinwh_bridge.modbus.poller")
    for _ in range(3):
        points: dict = {}
        p._read_vendor_registers(points)
        assert "vreg_15000" in points and "vreg_16001" not in points
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "16000-16002: refused" in warnings[0].getMessage()

    fail["on"] = False
    points = {}
    p._read_vendor_registers(points)
    assert points["vreg_16001"] == 1
    fail["on"] = True
    caplog.clear()
    p._read_vendor_registers({})
    assert [r.levelno for r in caplog.records].count(logging.WARNING) == 1


@pytest.mark.parametrize("payload", [b"", b"\x00"])
def test_a_short_reply_is_a_failure_not_a_crash(payload, caplog):
    p, _ = _poller(lambda addr, count: payload)
    points: dict = {}
    p._read_vendor_registers(points)
    assert not any(k.startswith("vreg_150") for k in points)
