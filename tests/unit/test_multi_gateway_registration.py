"""Extra gateways reach Home Assistant even when they connect after startup (#128).

A user with three gateways saw one HA device: extra gateways were registered
with MQTT once at startup, before a real aGate had been read, and never again.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo


@pytest.fixture
async def started(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MQTT_HOST", "127.0.0.1")
    monkeypatch.setenv("MQTT_PORT", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test"),
        app.router.lifespan_context(app),
    ):
        yield app


async def _add_real_gateway(app, gw_id: str, monkeypatch):
    from franklinwh_bridge.store.db import create_gateway

    await create_gateway(app.state.db, gw_id, gw_id, "192.0.2.20", 502)
    inst = SimpleNamespace(device_info=None, command_handler=object(),
                           status=SimpleNamespace(ac_type=0))
    real_get = app.state.registry.get
    monkeypatch.setattr(app.state.registry, "get",
                        lambda g: inst if g == gw_id else real_get(g))
    return inst


async def test_gateway_read_after_startup_is_registered(started, monkeypatch):
    app = started
    pub = app.state.mqtt_publisher
    inst = await _add_real_gateway(app, "shed", monkeypatch)

    await app.state.sync_mqtt_devices()  # serial not read yet → nothing to register
    assert pub.get_device("shed") is None

    inst.device_info = DeviceInfo(serial="TESTSERIAL00000002", gateway_id="shed")
    await app.state.sync_mqtt_devices()  # what the reconcile loop now triggers
    dev = pub.get_device("shed")
    assert dev is not None and dev.device_info is inst.device_info


async def test_restarted_gateway_is_rebound(started, monkeypatch):
    app = started
    pub = app.state.mqtt_publisher
    inst = await _add_real_gateway(app, "shed", monkeypatch)
    inst.device_info = DeviceInfo(serial="TESTSERIAL00000002", gateway_id="shed")
    await app.state.sync_mqtt_devices()
    first = pub.get_device("shed")

    # A restart replaces the instance's handler and device info.
    inst.command_handler = object()
    inst.device_info = DeviceInfo(serial="TESTSERIAL00000002", gateway_id="shed")
    await app.state.sync_mqtt_devices()
    dev = pub.get_device("shed")
    assert dev is not first
    assert dev.command_handler is inst.command_handler


def test_every_gateway_is_named_by_its_short_serial():
    primary = DeviceInfo(serial="TESTSERIAL0000ABCD", gateway_id="default")
    extra = DeviceInfo(serial="TESTSERIAL0000EF01", gateway_id="Shed GW")
    mock = DeviceInfo(serial="MOCK-DEMO", gateway_id="demo", firmware="MOCK",
                      manufacturer="FranklinWH (mock)")
    assert primary.ha_device_block()["name"] == "FranklinWH Modbus 0000ABCD"
    assert extra.ha_device_block()["name"] == "FranklinWH Modbus 0000EF01"
    assert mock.ha_device_block()["name"] == "FranklinWH Modbus demo (mock)"
    # Distinct identifiers: never one merged device.
    ids = {d.ha_device_block()["identifiers"][0] for d in (primary, extra, mock)}
    assert len(ids) == 3
