"""Auto-notify triggers through the API and the real Notifier: Send to, the switch, counts,
Send test, bridge started / restarted / stopped. Home Assistant is mocked with respx."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from franklinwh_bridge.notify_triggers import Triggers

from .test_notify_api import HA, _add_phone, client  # noqa: F401

PIXEL = f"{HA}/api/services/notify/mobile_app_pixel"
TV = f"{HA}/api/services/notify/living_room_tv"


@pytest.fixture
async def trg(client):  # noqa: F811
    t = Triggers(client.db, client.act)
    await t.load()
    client._transport.app.state.notify_triggers = t
    return t


async def _add_tv(client) -> str:  # noqa: F811
    r = await client.post("/api/notify/devices", json={
        "alias": "TV", "instance_id": "ha_home", "service": "notify.living_room_tv"})
    return r.json()["id"]


@respx.mock
async def test_off_grid_goes_to_every_enabled_device_with_its_figures(client, trg):  # noqa: F811
    pixel = respx.post(PIXEL).mock(return_value=httpx.Response(200, json=[]))
    await _add_phone(client)
    await trg.on_points("gw1", "Shed", {"connection_state": "Connected", "soc": 40,
                                        "wh_available": 6800, "home_load_ext": 1200})
    await trg.on_points("gw1", "Shed", {"connection_state": "Disconnected"})
    body = json.loads(pixel.calls.last.request.content)
    assert body["title"] == "FranklinWH off-grid"
    assert body["message"] == ("Gateway went OFF-GRID [Shed] — running on battery: SoC 40%, "
                               "stored 6.8 kWh, home 1.2 kW — about 5 h 40 min left at "
                               "current usage.")
    stats = (await client.get("/api/notify/triggers")).json()["stats"]
    assert stats["off_grid"]["count"] == 1


@respx.mock
async def test_send_to_chosen_devices_and_switched_off_trigger(client, trg):  # noqa: F811
    pixel = respx.post(PIXEL).mock(return_value=httpx.Response(200, json=[]))
    tv = respx.post(TV).mock(return_value=httpx.Response(200, json=[]))
    await _add_phone(client)
    tv_id = await _add_tv(client)
    r = await client.put("/api/notify/triggers", json={"triggers": {
        "aGate_offline": {"target": {"type": "device", "ids": [tv_id]}}}})
    assert r.status_code == 200
    assert await trg.fire("aGate_offline", gateway="Shed") == 1
    assert tv.called and not pixel.called
    off = {"triggers": {"aGate_offline": {"enabled": False}}}
    await client.put("/api/notify/triggers", json=off)
    assert await trg.fire("aGate_online", gateway="Shed") == 0
    assert tv.call_count == 1
    # Kept across a restart.
    again = Triggers(client.db, client.act)
    cfg = await again.load()
    assert cfg["triggers"]["aGate_offline"] == {**cfg["triggers"]["aGate_offline"],
                                                "enabled": False,
                                                "target": {"type": "device", "ids": [tv_id]}}


async def test_a_bad_save_is_refused_and_viewers_cannot_save(client, trg):  # noqa: F811
    r = await client.put("/api/notify/triggers", json={"triggers": {"soc_low": {"threshold": 150}}})
    assert r.status_code == 400
    await client.post("/api/auth/logout")
    await client.post("/api/auth/login", json={"username": "vee", "password": "pw"})
    assert (await client.get("/api/notify/triggers")).status_code == 200
    r = await client.put("/api/notify/triggers", json={"triggers": {"soc_low": {"enabled": True}}})
    assert r.status_code == 403


@respx.mock
async def test_push_switch_off_sends_nothing(client, trg):  # noqa: F811
    pixel = respx.post(PIXEL).mock(return_value=httpx.Response(200, json=[]))
    await _add_phone(client)
    await client.put("/api/notify/settings", json={"push": False})
    assert await trg.fire("off_grid", gateway="Shed", metrics="") == 0
    assert not pixel.called
    assert (await client.post("/api/notify/triggers/test", json={})).status_code == 409


@respx.mock
async def test_send_test(client, trg):  # noqa: F811
    pixel = respx.post(PIXEL).mock(return_value=httpx.Response(200, json=[]))
    await _add_phone(client)
    r = await client.post("/api/notify/triggers/test", json={})
    assert r.status_code == 200 and r.json()["delivered"] == 1
    assert json.loads(pixel.calls.last.request.content)["title"] == "FranklinWH — test"


@respx.mock
async def test_bridge_started_then_restarted_after_a_clean_stop(client, trg):  # noqa: F811
    pixel = respx.post(PIXEL).mock(return_value=httpx.Response(200, json=[]))
    await _add_phone(client)
    await trg.bridge_up("0.2.66")
    assert json.loads(pixel.calls.last.request.content)["message"] == "Bridge started (v0.2.66)."
    await trg.bridge_down("0.2.66")
    assert "Bridge stopping (v0.2.66)" in json.loads(pixel.calls.last.request.content)["message"]
    await trg.bridge_up("0.2.67")
    msg = json.loads(pixel.calls.last.request.content)["message"]
    assert msg.startswith("Bridge restarted (v0.2.67, updated from 0.2.66) after ")
