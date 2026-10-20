"""#308: the library's SunSpec scan binds base 0, not the V12 mirror at 40000.

aGate firmware V12R02B60D00 answers "SunS" at both PDU 0 and 40000.
pysunspec2 1.3.6 probes 40000 first, so without the bridge's device class the
library would bind the mirror. The fake device below answers at every base.
"""

from __future__ import annotations

import pytest

sunspec2 = pytest.importorskip("sunspec2.modbus.client")


def _scan_base(cls) -> int | None:
    dev = cls(slave_id=1, ipaddr="192.0.2.50", ipport=502)
    dev.connect = lambda *a, **k: None
    # "SunS" then the end-model id: a SunSpec map at every base, no models.
    dev.read = lambda addr, count, *a, **k: b"SunS\xff\xff"
    dev.scan(connect=True)
    return dev.base_addr


@pytest.fixture
def lib(monkeypatch):
    from franklinwh_modbus import controller

    monkeypatch.setattr(controller, "SunSpecModbusClientDeviceTCP",
                        sunspec2.SunSpecModbusClientDeviceTCP)
    return controller


def test_pysunspec2_alone_binds_the_40000_mirror():
    """The defect: when 40000 answers, pysunspec2 binds it before 0."""
    assert _scan_base(sunspec2.SunSpecModbusClientDeviceTCP) == 40000


def test_the_bridge_binds_base_0_when_both_answer(lib):
    from franklinwh_bridge.gateway.instance import prefer_sunspec_base_0

    prefer_sunspec_base_0()
    assert _scan_base(lib.SunSpecModbusClientDeviceTCP) == 0


def test_40000_and_50000_stay_as_fallbacks(lib):
    from franklinwh_bridge.gateway.instance import prefer_sunspec_base_0

    prefer_sunspec_base_0()
    prefer_sunspec_base_0()  # idempotent: no double subclass
    cls = lib.SunSpecModbusClientDeviceTCP
    assert cls.__bases__ == (sunspec2.SunSpecModbusClientDeviceTCP,)
    dev = cls(slave_id=1, ipaddr="192.0.2.50", ipport=502)
    assert dev.base_addr_list == [0, 40000, 50000]


def test_the_gateway_installs_it_before_making_the_controller(lib):
    import inspect

    from franklinwh_bridge.gateway import instance

    src = inspect.getsource(instance.GatewayInstance)
    assert src.index("prefer_sunspec_base_0()") < src.index("FranklinWHController(")
