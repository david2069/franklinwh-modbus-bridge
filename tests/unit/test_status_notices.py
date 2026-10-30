"""Status notices (owner, 2026-10-30): bridge started and stopping, gateway paused
and resumed, as HA persistent notifications, modelled on the Energipays bridge.

One attempt each and an INFO log line either way; on by default, with a switch.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge import status_notices as sn
from franklinwh_bridge.main import app
from franklinwh_bridge.store.db import init_db

T0 = 1_792_536_000.0


class FakeHa:
    def __init__(self, ok=True, hang=False):
        self.ok, self.hang = ok, hang
        self.name = "This Home Assistant"
        self.calls = []

    async def call_action(self, domain, service, data):
        self.calls.append((f"{domain}.{service}", data))
        if self.hang:
            await asyncio.sleep(60)
        return {"ok": True, "error": None} if self.ok else {"ok": False, "error": "502"}


def _registry(ha):
    return SimpleNamespace(primary=lambda: ha)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "n.db")
    yield conn
    await conn.close()


def test_started_text_counts_gateways_and_names_the_idle_ones():
    title, msg = sn.started_text("9.9.9", [{"name": "Garage", "poll_interval": 10},
                                           {"name": "Demo", "poll_interval": 5}],
                                 [{"name": "aGate"}])
    assert title == "FranklinWH Modbus Bridge: started"
    assert msg.startswith("Bridge is up (v9.9.9): polling 2 gateways: "
                          "Garage (every 10 s), Demo (every 5 s).")
    assert "Not polling: aGate." in msg
    _, one = sn.started_text("9.9.9", [{"name": "Garage", "poll_interval": 10}], [])
    assert "polling 1 gateway: Garage" in one and "Not polling" not in one
    _, none = sn.started_text("9.9.9", [], [{"name": "aGate"}])
    assert "no gateway is polling" in none


async def test_sent_once_as_a_persistent_notification_and_logged(db, caplog):
    ha = FakeHa()
    caplog.set_level(logging.INFO, logger="franklinwh_bridge.status_notices")
    title, msg = sn.stopping_text()
    assert await sn.send(db, _registry(ha), sn.BRIDGE_NOTIFICATION_ID, title, msg, T0)
    [(svc, data)] = ha.calls
    assert svc == "persistent_notification.create"
    assert data["notification_id"] == "fwh_mb_bridge_status"
    assert data["title"] == "FranklinWH Modbus Bridge: stopped"
    assert data["message"].startswith("Bridge is shutting down: polling stops")
    assert "Status notice sent to Home Assistant (This Home Assistant)" in caplog.text


@pytest.mark.parametrize("setup, reason", [
    ("off", "status notices off"),
    ("no_ha", "no Home Assistant configured"),
    ("fails", "502"),
])
async def test_not_sent_says_why_in_the_log(db, caplog, setup, reason):
    caplog.set_level(logging.INFO, logger="franklinwh_bridge.status_notices")
    ha = FakeHa(ok=setup != "fails")
    if setup == "off":
        await sn.set_status_notices(db, False)
    reg = _registry(None if setup == "no_ha" else ha)
    assert not await sn.send(db, reg, "x", "t", "the message", T0)
    assert f"Status notice not sent to Home Assistant ({reason}): the message" in caplog.text
    if setup == "off":
        assert ha.calls == []


async def test_a_hung_home_assistant_never_holds_up_the_bridge(db, monkeypatch):
    monkeypatch.setattr(sn, "SEND_TIMEOUT_S", 0.05)
    assert not await sn.send(db, _registry(FakeHa(hang=True)), "x", "t", "m", T0)


async def test_on_by_default_and_switchable(db):
    assert await sn.status_notices_on(db)
    await sn.set_status_notices(db, False)
    assert not await sn.status_notices_on(db)


# ── Routes: Stop pauses, Start resumes, the switch ─────────────


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_stop_and_start_tell_home_assistant(client, monkeypatch):
    ha = FakeHa()
    monkeypatch.setattr(app.state.ha_registry, "primary", lambda: ha)
    resp = await client.post("/api/gateways", json={
        "gateway_id": "demo", "name": "Demo", "mock": True})
    assert resp.status_code == 201

    assert (await client.post("/api/gateways/demo/stop")).status_code == 200
    assert (await client.post("/api/gateways/demo/start")).status_code == 200
    notices = [d for s, d in ha.calls if d["notification_id"] == "fwh_mb_gateway_demo"]
    assert [d["title"] for d in notices] == ["FranklinWH Modbus Bridge: Demo paused",
                                             "FranklinWH Modbus Bridge: Demo polling"]
    assert "Polling of Demo is paused (Stop, by " in notices[0]["message"]

    # A restart of a running gateway is not a resume: no notice.
    assert (await client.post("/api/gateways/demo/restart")).status_code == 200
    assert len([d for s, d in ha.calls if d["notification_id"] == "fwh_mb_gateway_demo"]) == 2
    # A restart of a paused one is.
    assert (await client.post("/api/gateways/demo/stop")).status_code == 200
    assert (await client.post("/api/gateways/demo/restart")).status_code == 200
    titles = [d["title"] for s, d in ha.calls if d["notification_id"] == "fwh_mb_gateway_demo"]
    assert titles[-2:] == ["FranklinWH Modbus Bridge: Demo paused",
                           "FranklinWH Modbus Bridge: Demo polling"]


async def test_settings_switch(client):
    assert (await client.get("/api/alerts/settings")).json()["status_notices"] is True
    r = await client.put("/api/alerts/settings", json={"status_notices": False})
    assert r.status_code == 200
    body = r.json()
    assert body["status_notices"] is False and body["ha_channel"] is True  # untouched
