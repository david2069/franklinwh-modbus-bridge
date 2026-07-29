"""Tests for gateway/diagnostics.py — read-only connectivity root-cause tool.

Uses monkeypatched tcp_probe (no real sockets) plus a mocked SunSpec
controller, following the same pattern as test_poller.py.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from franklinwh_bridge.gateway import diagnostics
from franklinwh_bridge.gateway.net_probe import ProbeResult
from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.modbus.sample import SampleBus


@pytest.fixture
def mock_controller():
    ctrl = MagicMock()
    ctrl.ip_address = "127.0.0.1"
    ctrl.connect.return_value = True
    ctrl.get_model.return_value = None
    return ctrl


@pytest.fixture
def poller(mock_controller):
    return ModbusPoller(mock_controller, SampleBus(), poll_interval=1)


def _make_instance(poller, host="127.0.0.1", port=502, mock=False):
    config = SimpleNamespace(host=host, port=port, mock=mock)
    status = SimpleNamespace(health="unknown")
    return SimpleNamespace(gateway_id="test_gw", config=config, poller=poller, status=status)


def _stub_tcp_probe(monkeypatch, ok: bool, error: str | None = None):
    async def fake(host, port, timeout=5.0):
        return ProbeResult(ok=ok, host=host, port=port, latency_ms=1.0 if ok else None, error=error)

    monkeypatch.setattr(diagnostics, "tcp_probe", fake)


async def test_mock_gateway_short_circuits():
    inst = _make_instance(poller=None, mock=True)
    result = await diagnostics.run_diagnostics(inst)
    assert result["mock"] is True
    assert result["checks"] == {}
    assert "mock gateway" in result["verdict"]


async def test_both_tcp_ports_unreachable(poller, monkeypatch):
    _stub_tcp_probe(monkeypatch, ok=False, error="Connection refused")
    inst = _make_instance(poller=poller)
    result = await diagnostics.run_diagnostics(inst)
    assert result["checks"]["tcp_502_modbus"]["ok"] is False
    assert result["checks"]["tcp_9000_local_api"]["ok"] is False
    assert "unreachable on the network" in result["verdict"]


async def test_dead_session_reported_distinctly_from_tcp_failure(
    poller, mock_controller, monkeypatch
):
    """The exact bug we root-caused: TCP is fine, but the live Modbus read
    raises a broken pipe because the poller's cached session is dead."""
    await poller._connect()
    model = MagicMock()
    model.read.side_effect = BrokenPipeError("[Errno 32] Broken pipe")
    mock_controller.get_model.return_value = model

    _stub_tcp_probe(monkeypatch, ok=True)
    inst = _make_instance(poller=poller)
    result = await diagnostics.run_diagnostics(inst)

    assert result["checks"]["tcp_502_modbus"]["ok"] is True
    assert result["checks"]["modbus_protocol"]["ok"] is False
    assert result["checks"]["modbus_protocol"]["error_type"] == "dead_session"
    assert "dead" in result["verdict"]


async def test_flags_wedged_poller_from_recent_warnings(poller, mock_controller, monkeypatch):
    """TCP + a live protocol read both succeed right now, but the log buffer
    shows recent "Extra point reads failed" warnings — this is the actual
    signature of the bug we root-caused: the poller kept self-reporting
    connected=True while silently failing every cycle. (Note: PollerState
    .connected can't be forced False here and still see protocol.ok=True —
    probe_protocol() deliberately refuses to probe when the poller doesn't
    think it has a session, so recent_warnings is the reachable signal.)
    """
    await poller._connect()
    model = MagicMock()
    model.read.return_value = None
    mock_controller.get_model.return_value = model

    _stub_tcp_probe(monkeypatch, ok=True)
    inst = _make_instance(poller=poller)

    log_buffer = [{
        "ts": time.time(),
        "level": "WARNING",
        "name": "franklinwh_bridge.modbus.poller",
        "message": "Extra point reads failed: Socket write error: [Errno 32] Broken pipe",
        "gateway_id": "",
    }]
    result = await diagnostics.run_diagnostics(inst, log_buffer=log_buffer)
    assert result["checks"]["modbus_protocol"]["ok"] is True
    assert result["poller_state"]["connected"] is True
    assert "wedged" in result["verdict"]


async def test_healthy_when_everything_agrees(poller, mock_controller, monkeypatch):
    await poller._connect()
    model = MagicMock()
    model.read.return_value = None
    mock_controller.get_model.return_value = model

    _stub_tcp_probe(monkeypatch, ok=True)
    inst = _make_instance(poller=poller)
    result = await diagnostics.run_diagnostics(inst)
    assert result["verdict"] == (
        "Modbus connection is healthy: TCP and protocol-level reads both succeeded."
    )


async def test_counts_recent_extra_read_warnings_from_log_buffer(
    poller, mock_controller, monkeypatch
):
    await poller._connect()
    model = MagicMock()
    model.read.return_value = None
    mock_controller.get_model.return_value = model

    _stub_tcp_probe(monkeypatch, ok=True)
    inst = _make_instance(poller=poller)

    log_buffer = [
        {
            "ts": time.time(),
            "level": "WARNING",
            "name": "franklinwh_bridge.modbus.poller",
            "message": "Extra point reads failed: Socket write error: [Errno 32] Broken pipe",
            "gateway_id": "",
        }
        for _ in range(3)
    ] + [
        {"ts": time.time(), "level": "INFO", "name": "x", "message": "unrelated", "gateway_id": ""},
        {"ts": time.time() - 10_000, "level": "WARNING", "name": "x",
         "message": "Extra point reads failed: too old", "gateway_id": ""},
    ]
    result = await diagnostics.run_diagnostics(inst, log_buffer=log_buffer)
    assert result["recent_extra_read_warnings"] == 3


async def test_probe_protocol_not_connected(poller):
    result = await poller.probe_protocol()
    assert result["ok"] is False
    assert result["error_type"] == "not_connected"


async def test_probe_protocol_does_not_mutate_poller_state(poller, mock_controller):
    """Diagnostics is read-only — a failed probe must not touch error counters."""
    await poller._connect()
    model = MagicMock()
    model.read.side_effect = BrokenPipeError("broken")
    mock_controller.get_model.return_value = model

    before_errors = poller.state.consecutive_errors
    result = await poller.probe_protocol()
    assert result["ok"] is False
    assert poller.state.consecutive_errors == before_errors
    assert poller.state.connected is True  # unchanged by the probe itself
