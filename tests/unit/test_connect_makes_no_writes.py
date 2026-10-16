"""Connecting to the aGate writes nothing (#239).

franklinwh-modbus <= 0.9.5 ran a "writability test" inside ``connect()``:
write test values to the operating mode (15507) and both reserves
(15508/15509), then write the originals back. The bridge connects at every
start and reconnect. These tests drive the library's real ``connect()``
against a fake socket and record every Modbus write (function code 6).
"""

from __future__ import annotations

import struct

import pytest
from franklinwh_modbus import controller as lib

from franklinwh_bridge.gateway.instance import disable_connect_writes

MODE, SELF_RES, TOU_RES = 15507, 15508, 15509


class _Socket:
    def __init__(self, regs: dict[int, int], writes: list[tuple[int, int]]) -> None:
        self.regs, self.writes, self._last = regs, writes, b""

    def sendall(self, req: bytes) -> None:
        _tid, _pid, _len, unit, fc, addr, val = struct.unpack(">HHHBBHH", req)
        if fc == 6:
            self.writes.append((addr, val))
            self.regs[addr] = val
            self._last = req  # echo, as a real write reply
        else:  # fc 3: read `val` registers
            data = b"".join(struct.pack(">H", self.regs.get(addr + i, 0)) for i in range(val))
            self._last = struct.pack(">HHHBBB", 0, 0, 3 + len(data), unit, fc, len(data)) + data

    def recv(self, _n: int) -> bytes:
        return self._last


class _Client:
    def __init__(self, sock: _Socket) -> None:
        self.socket = sock

    def connect(self) -> None:
        pass


class _Device:
    """Stands in for pysunspec2's TCP device: no network, no models."""

    regs: dict[int, int] = {}
    writes: list[tuple[int, int]] = []

    def __init__(self, **_kw) -> None:
        self.models: dict = {}
        self.client = _Client(_Socket(_Device.regs, _Device.writes))

    def scan(self, *a, **kw) -> None:
        pass


@pytest.fixture
def fake_agate(monkeypatch):
    _Device.regs = {MODE: 2, SELF_RES: 20, TOU_RES: 30}  # Self-Consumption, 20 %, 30 %
    _Device.writes = []
    monkeypatch.setattr(lib, "SunSpecModbusClientDeviceTCP", _Device)
    return _Device


def _controller() -> lib.FranklinWHController:
    return lib.FranklinWHController(ip_address="192.0.2.50", port=502, unit_id=1, timeout=1)


def test_the_installed_library_writes_on_connect(fake_agate):
    """Pins the hazard: without the guard, connect() writes mode and reserves.
    When the pin moves past the library's fix (#230) this test should flip."""
    c = _controller()
    c.connect()
    written = {addr for addr, _ in fake_agate.writes}
    assert MODE in written
    assert (MODE, 1) in fake_agate.writes  # Emergency Backup, briefly, on a live aGate


def test_bridge_controller_connect_writes_nothing(fake_agate):
    c = _controller()
    disable_connect_writes(c)
    assert c.connect() is True
    assert fake_agate.writes == []
    assert fake_agate.regs == {MODE: 2, SELF_RES: 20, TOU_RES: 30}


def test_guard_is_harmless_without_the_method():
    class Plain:
        pass

    disable_connect_writes(Plain())  # a mock controller, or a fixed library
