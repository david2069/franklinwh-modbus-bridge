"""#296 item 48 phase 1: notification devices, the Push notifications switch, the
Notifications check and Recent notifications, through franklinwh-ha-notify.

Home Assistant's REST API is mocked with respx; the tap listener is not started
(a tap is fed to the Notifier as the listener would).
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx
from fastapi import FastAPI
from franklinwh_ha_notify import Notifier, Tap
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge import actionable as act_mod
from franklinwh_bridge.actionable import Actionable, device_kind, mark_duplicates
from franklinwh_bridge.alert_delivery import ha_channel_on
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.notify_api import router as notify_router
from franklinwh_bridge.config.supervisor import SUPERVISOR_CORE_URL
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store.db import (
    create_ha_instance,
    create_user,
    delete_ha_instance,
    get_notify_devices,
    init_db,
)

HA = "http://ha1:8123"
SERVICES = [{"domain": "notify", "services": {
    "mobile_app_pixel": {"name": "Send a notification via mobile_app_pixel"},
    "mobile_app_pixel_2": {},
    "living_room_tv": {},
    "send_message": {},
}}]


class _Primary:
    name = "Home"
    connected = True
    last_error = None

    async def refresh(self):
        return None


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    monkeypatch.setattr(Notifier, "listen", lambda self, ids=None: None)
    path = tmp_path / "notify.db"
    db = await init_db(path)
    await create_user(db, "root", hash_password("pw"), role="admin")
    await create_user(db, "vee", hash_password("pw"), role="viewer")
    await create_ha_instance(db, "Home", HA, token="tok", is_default=True, ha_id="ha_home")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = db
    app.state.config = SimpleNamespace(environment="dev")
    app.state.ha_registry = SimpleNamespace(primary=lambda: _Primary())
    act = Actionable(db, str(path))
    await act.start()
    app.state.actionable = act
    app.include_router(auth_router)
    app.include_router(notify_router)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.act = act
        ac.db = db
        await ac.post("/api/auth/login", json={"username": "root", "password": "pw"})
        yield ac
    await act.stop()
    await db.close()


async def _add_phone(client) -> str:
    r = await client.post("/api/notify/devices", json={
        "alias": "Phone", "instance_id": "ha_home", "service": "notify.mobile_app_pixel"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_kind_and_duplicates():
    assert device_kind("mobile_app_davids_iphone") == "phone"
    assert device_kind("mobile_app_ipad") == "tablet"
    assert device_kind("living_room_tv") == "tv"
    assert device_kind("email") == "other"
    assert mark_duplicates(["mobile_app_x", "mobile_app_x_2", "lone_3"]) == {
        "mobile_app_x_2": "mobile_app_x"}


def test_supervisor_instance_reads_its_token_on_every_call(monkeypatch):
    inst = act_mod._ha_instance({"id": "local", "base_url": SUPERVISOR_CORE_URL,
                                 "token": None, "name": "This HA"})
    monkeypatch.setenv("SUPERVISOR_TOKEN", "first")
    assert inst.bearer() == "first"
    monkeypatch.setenv("SUPERVISOR_TOKEN", "second")
    assert inst.bearer() == "second"
    assert act_mod._ha_instance({"id": "x", "base_url": HA, "token": "t"}).bearer() == "t"


async def test_devices_add_edit_remove_and_admin_only(client):
    did = await _add_phone(client)
    rows = (await client.get("/api/notify/devices")).json()["devices"]
    assert [(r["alias"], r["service"], r["buttons"], r["kind"], r["instance_name"])
            for r in rows] == [("Phone", "mobile_app_pixel", True, "phone", "Home")]
    again = await client.post("/api/notify/devices", json={
        "alias": "Again", "instance_id": "ha_home", "service": "mobile_app_pixel"})
    assert again.status_code == 409
    nowhere = await client.post("/api/notify/devices", json={
        "alias": "X", "instance_id": "nope", "service": "mobile_app_pixel"})
    assert nowhere.status_code == 400
    r = await client.patch(f"/api/notify/devices/{did}", json={"enabled": False})
    assert r.json()["enabled"] is False

    await client.post("/api/auth/login", json={"username": "vee", "password": "pw"})
    assert (await client.get("/api/notify/devices")).status_code == 200
    assert (await client.delete(f"/api/notify/devices/{did}")).status_code == 403
    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    assert (await client.delete(f"/api/notify/devices/{did}")).status_code == 200
    assert (await client.get("/api/notify/devices")).json()["devices"] == []


async def test_removing_an_ha_instance_removes_its_devices(client):
    await _add_phone(client)
    await delete_ha_instance(client.db, "ha_home")
    assert await get_notify_devices(client.db) == []


@respx.mock
async def test_discover_lists_phones_first_marks_duplicates_and_saved(client):
    respx.get(f"{HA}/api/services").mock(return_value=httpx.Response(200, json=SERVICES))
    await _add_phone(client)
    r = await client.get("/api/notify/discover", params={"instance_id": "ha_home"})
    got = {s["service"]: s for s in r.json()["services"]}
    assert set(got) == {"mobile_app_pixel", "mobile_app_pixel_2", "living_room_tv"}
    assert [s["buttons"] for s in r.json()["services"]] == [True, True, False]
    assert got["mobile_app_pixel_2"]["duplicate_of"] == "mobile_app_pixel"
    assert got["mobile_app_pixel"]["saved"] is True
    assert got["living_room_tv"]["kind"] == "tv"


@respx.mock
async def test_test_sends_with_the_switch_off_but_broadcast_does_not(client):
    route = respx.post(f"{HA}/api/services/notify/mobile_app_pixel").mock(
        return_value=httpx.Response(200, json=[]))
    did = await _add_phone(client)
    assert (await client.put("/api/notify/settings", json={"push": False})).json() == {
        "push": False}
    assert await ha_channel_on(client.db) is False  # one switch: the alert channel's setting
    r = await client.post(f"/api/notify/devices/{did}/test")
    assert r.json()["delivered"] == 1 and route.call_count == 1
    b = await client.post("/api/notify/broadcast", json={"message": "hello"})
    assert b.status_code == 409 and b.json()["detail"] == "Push notifications are off"

    await client.put("/api/notify/settings", json={"push": True})
    b = await client.post("/api/notify/broadcast", json={"message": "hello", "title": "Hi"})
    assert b.json()["delivered"] == 1
    assert json.loads(route.calls[-1].request.content) == {"title": "Hi", "message": "hello"}
    log = (await client.get("/api/notify/log")).json()["entries"]
    assert [(e["event"], e["direction"]) for e in log] == [("broadcast", "SENT"), ("test", "SENT")]


@respx.mock
async def test_test_with_buttons_round_trip_and_check(client):
    route = respx.post(f"{HA}/api/services/notify/mobile_app_pixel").mock(
        return_value=httpx.Response(200, json=[]))
    did = await _add_phone(client)
    lines = {ln["name"]: ln for ln in (await client.get("/api/notify/checks")).json()["lines"]}
    assert lines["Last Test with buttons"]["status"] == "not_set_up"
    assert lines["Phones (mobile_app_*)"]["status"] == "pass"

    rid = (await client.post(f"/api/notify/devices/{did}/test-buttons")).json()["request_id"]
    body = json.loads(route.calls[-1].request.content)
    assert [a["action"] for a in body["data"]["actions"]] == [f"FWH_YES_{rid}", f"FWH_NO_{rid}"]
    assert (await client.get(f"/api/notify/test-buttons/{rid}")).json()["result"] == "waiting"

    await client.act.notifier.on_tap(Tap("ha_home", "YES", rid, None, {}))
    for _ in range(50):
        if (await client.get(f"/api/notify/test-buttons/{rid}")).json()["result"] != "waiting":
            break
        await asyncio.sleep(0.02)
    assert (await client.get(f"/api/notify/test-buttons/{rid}")).json()["result"] == "yes"
    lines = {ln["name"]: ln for ln in (await client.get("/api/notify/checks")).json()["lines"]}
    assert lines["Last Test with buttons"]["status"] == "pass"
    assert "tapped Yes" in lines["Last Test with buttons"]["detail"]


async def test_test_with_buttons_needs_the_switch_and_a_phone(client):
    did = await _add_phone(client)
    await client.put("/api/notify/settings", json={"push": False})
    r = await client.post(f"/api/notify/devices/{did}/test-buttons")
    assert r.status_code == 409 and r.json()["detail"] == "Push notifications are off"
    await client.put("/api/notify/settings", json={"push": True})
    tv = (await client.post("/api/notify/devices", json={
        "alias": "TV", "instance_id": "ha_home", "service": "living_room_tv"})).json()["id"]
    r = await client.post(f"/api/notify/devices/{tv}/test-buttons")
    assert r.status_code == 409 and "mobile_app" in r.json()["detail"]
