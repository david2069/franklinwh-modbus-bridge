"""Admin actions: recorded, applied live, and warned about (#296 follow-up).

- A poll interval change applies to the running poller without a restart.
- Poll interval changes and gateway start / stop / restart are each an app-log
  line and an ADMIN_ACTION alert naming the user.
- /api/lifecycle/check names what a stop or restart would interrupt.
- The whole-bridge restart is Docker only and admin only.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api import gateways_api
from franklinwh_bridge.api.auth import require_admin_for_writes, require_auth
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.modbus.poller import ModbusPoller
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import alerts
from franklinwh_bridge.store.db import create_gateway, create_user, init_db


class FakePoller:
    def __init__(self) -> None:
        self.interval = None

    def set_poll_interval(self, s: int) -> None:
        self.interval = s


class FakeRegistry:
    def __init__(self) -> None:
        self.inst = SimpleNamespace(config=SimpleNamespace(poll_interval=10), poller=FakePoller())
        self.running = {"gw1"}

    def get(self, gid):
        return self.inst if gid in self.running else None

    def list_active(self):
        return sorted(self.running)

    async def start_gateway(self, gid):
        self.running.add(gid)
        return self.inst

    async def stop_gateway(self, gid):
        self.running.discard(gid)


class FakeEngine:
    def __init__(self) -> None:
        self.points = {"dispatch_active": False}

    def dispatch_points(self, gid):
        return self.points


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    conn = await init_db(tmp_path / "api.db")
    await create_user(conn, "root", hash_password("pw"), role="admin")
    await create_user(conn, "vee", hash_password("pw"), role="viewer")
    await create_gateway(conn, "gw1", "One", "192.0.2.10")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = conn
    app.state.config = SimpleNamespace(environment="dev")
    app.state.registry = FakeRegistry()
    app.state.schedule_engine = FakeEngine()
    app.include_router(auth_router)
    app.include_router(gateways_api.router,
                       dependencies=[Depends(require_auth), Depends(require_admin_for_writes)])
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        ac.app, ac.db = app, conn
        await ac.post("/api/auth/login", json={"username": "root", "password": "pw"})
        yield ac
    await conn.close()


async def _admin_alerts(db):
    return [a for a in await alerts.list_alerts(db) if a["code"] == "ADMIN_ACTION"]


def test_poller_interval_changes_live():
    p = ModbusPoller.__new__(ModbusPoller)
    p._poll_interval, p._task = 10, None
    p.set_poll_interval(3)
    assert p._poll_interval == 3


async def test_poll_interval_applied_live_and_recorded(client, caplog):
    with caplog.at_level(logging.WARNING, logger=gateways_api.logger.name):
        r = await client.patch("/api/gateways/gw1", json={"poll_interval": 3})
    assert r.status_code == 200
    inst = client.app.state.registry.inst
    assert inst.poller.interval == 3 and inst.config.poll_interval == 3
    assert "poll interval 10 s → 3 s — below the recommended minimum (by root)" in caplog.text
    (a,) = await _admin_alerts(client.db)
    assert a["severity"] == "warning" and "by root" in a["message"]


async def test_unchanged_poll_interval_records_nothing(client):
    await client.patch("/api/gateways/gw1", json={"poll_interval": 10})
    assert await _admin_alerts(client.db) == []


@pytest.mark.parametrize("action, word", [
    ("stop", "stopped"), ("start", "started"), ("restart", "restarted"),
])
async def test_gateway_lifecycle_is_recorded(client, action, word):
    assert (await client.post(f"/api/gateways/gw1/{action}")).status_code == 200
    (a,) = await _admin_alerts(client.db)
    assert a["message"] == f"Gateway gw1 {word} (by root)" and a["gateway_id"] == "gw1"


async def test_viewer_cannot_stop(client):
    await client.post("/api/auth/login", json={"username": "vee", "password": "pw"})
    assert (await client.post("/api/gateways/gw1/stop")).status_code == 403
    assert await _admin_alerts(client.db) == []


async def test_lifecycle_check_names_a_running_schedule(client, monkeypatch):
    monkeypatch.setattr(gateways_api, "detect_environment", lambda: "ha_addon")
    out = (await client.get("/api/lifecycle/check")).json()
    assert out["busy"] == [] and out["bridge_restart"]["available"] is False
    assert "Home Assistant" in out["bridge_restart"]["reason"]

    client.app.state.schedule_engine.points = {
        "dispatch_active": True, "dispatch_source": "schedule",
        "dispatch_entry": "Evening discharge", "dispatch_action": "Force Discharge"}
    out = (await client.get("/api/lifecycle/check")).json()
    assert out["busy"] == [{"gateway_id": "gw1", "source": "schedule",
                            "entry": "Evening discharge", "action": "Force Discharge"}]


async def test_bridge_restart_refused_outside_docker(client, monkeypatch):
    monkeypatch.setattr(gateways_api, "detect_environment", lambda: "ha_addon")
    r = await client.post("/api/bridge/restart")
    assert r.status_code == 409
    assert await _admin_alerts(client.db) == []


async def test_bridge_restart_on_docker_is_admin_only_and_graceful(client, monkeypatch):
    monkeypatch.setattr(gateways_api, "detect_environment", lambda: "docker")
    calls = []
    monkeypatch.setattr(gateways_api.os, "kill", lambda pid, sig: calls.append(sig))

    await client.post("/api/auth/login", json={"username": "vee", "password": "pw"})
    assert (await client.post("/api/bridge/restart")).status_code == 403

    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    r = await client.post("/api/bridge/restart")
    assert r.json() == {"restarting": True}
    (a,) = await _admin_alerts(client.db)
    assert a["message"] == "Bridge restart requested (by root)"
    await asyncio.sleep(1.2)  # SIGTERM is sent after the response
    assert calls == [signal.SIGTERM]
