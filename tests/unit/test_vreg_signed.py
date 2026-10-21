"""15013 is signed (int16): it tracks M714 DCW, negative = charging (owner, 2026-10-21).

As uint16 the Explorer showed 65496 W for -40 W, and history buckets averaged
across zero were nonsense. 0xFFFF stays as read: it is also the aGate's
not-available marker.
"""

from unittest.mock import MagicMock

from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus


def _read(regs_15000):
    ctrl = MagicMock()
    ctrl.unit_id = 1
    p = ModbusPoller(controller=ctrl, sample_bus=SampleBus(), gateway_id="gw")
    client = MagicMock()

    def read(addr, count, device_id):
        r = MagicMock()
        r.isError.return_value = False
        r.registers = list(regs_15000) if addr == 15000 else [0] * count
        return r

    client.read_holding_registers.side_effect = read
    p._get_vreg_client = lambda: client
    points = {}
    p._read_vendor_registers(points)
    return points


def _regs(v15013):
    regs = [0] * 40
    regs[13] = v15013
    regs[16] = 0xFFFE  # a uint16 register keeps its raw value
    return regs


def test_15013_negative_is_charging():
    pts = _read(_regs(0xFFD8))
    assert pts["vreg_15013"] == -40
    assert pts["vreg_15016"] == 0xFFFE


def test_15013_positive_and_sentinel_unchanged():
    assert _read(_regs(500))["vreg_15013"] == 500
    assert _read(_regs(0xFFFF))["vreg_15013"] == 0xFFFF  # not-available, skipped by history
