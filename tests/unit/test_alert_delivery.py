"""#324 §6 / §11 D: critical alerts reach Home Assistant, and a failed call never loses one.

Each critical alert gets an HA persistent notification (fwh_mb_alert_<id>),
retried with back-off, dismissed when acknowledged in the bridge. Switched off,
or with no HA, the delivery is recorded as skipped with the reason.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge import alert_delivery
from franklinwh_bridge.alert_delivery import AlertDelivery
from franklinwh_bridge.api.alerts_api import router as alerts_router
from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import alerts as alert_store
from franklinwh_bridge.store.db import create_user, init_db

T0 = 1_792_536_000.0


class FakeHa:
    def __init__(self, ok=True):
        self.ok = ok
        self.name = "This Home Assistant"
        self.calls = []

    async def call_action(self, domain, service, data):
        self.calls.append((f"{domain}.{service}", data))
        return {"ok": True, "error": None} if self.ok else {"ok": False, "error": "502"}


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "deliver.db")
    yield conn
    await conn.close()


def _registry(ha):
    return SimpleNamespace(primary=lambda: ha)


async def _deliveries(db):
    [a] = await alert_store.list_alerts(db)
    return a["deliveries"]


async def test_critical_alert_is_notified_then_dismissed_on_ack(db):
    ha = FakeHa()
    aid = await alert_store.raise_event(db, "COMMAND_REFUSED", "Force Charge refused",
                                        gateway_id="gw1", now=T0)
    await alert_store.open_condition(db, "DATA_MISSING_PARTIAL", "warning only", now=T0)
    d = AlertDelivery(db, _registry(ha))
    await d.run_once(now=T0 + 1)
    [(svc, data)] = ha.calls
    assert svc == "persistent_notification.create"
    assert data["notification_id"] == f"fwh_mb_alert_{aid}"
    assert data["title"] == "FranklinWH Modbus Bridge: Battery command refused"
    assert "Force Charge refused" in data["message"] and "(gw1)" in data["message"]
    [row] = (await alert_store.list_alerts(db, kind="event"))[0]["deliveries"]
    assert (row["channel"], row["status"], row["attempts"], row["delivered_ts"]) == (
        "ha", "delivered", 1, T0 + 1)

    await d.run_once(now=T0 + 2)
    assert len(ha.calls) == 1  # once only

    await alert_store.ack(db, aid, "user:root")
    await d.run_once(now=T0 + 3)
    assert ha.calls[-1] == ("persistent_notification.dismiss",
                            {"notification_id": f"fwh_mb_alert_{aid}"})
    [row] = (await alert_store.list_alerts(db, kind="event"))[0]["deliveries"]
    assert row["status"] == "dismissed" and row["delivered_ts"] == T0 + 1


async def test_failure_backs_off_retries_and_delivers(db):
    ha = FakeHa(ok=False)
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "down", now=T0)
    d = AlertDelivery(db, _registry(ha))
    await d.run_once(now=T0)
    [row] = await _deliveries(db)
    assert (row["status"], row["attempts"], row["last_error"]) == ("pending", 1, "502")
    await d.run_once(now=T0 + 29)  # inside the 30 s back-off
    assert len(ha.calls) == 1
    await d.run_once(now=T0 + 30)
    await d.run_once(now=T0 + 89)  # second wait is 60 s
    assert len(ha.calls) == 2
    ha.ok = True
    await d.run_once(now=T0 + 90)
    [row] = await _deliveries(db)
    assert (row["status"], row["attempts"]) == ("delivered", 3)


async def test_owed_delivery_goes_out_at_once_after_restart(db):
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "down", now=T0)
    await AlertDelivery(db, _registry(FakeHa(ok=False))).run_once(now=T0)
    ha = FakeHa()
    await AlertDelivery(db, _registry(ha)).run_once(now=T0 + 1)  # a new process
    assert len(ha.calls) == 1
    assert (await _deliveries(db))[0]["status"] == "delivered"


async def test_gives_up_after_a_day(db):
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "down", now=T0)
    await AlertDelivery(db, _registry(FakeHa(ok=False))).run_once(now=T0 + 86_400)
    assert (await _deliveries(db))[0]["status"] == "failed"


async def test_channel_off_and_no_ha_are_recorded_as_skipped(db):
    await alert_delivery.set_ha_channel(db, False)
    await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "down", gateway_id="a")
    ha = FakeHa()
    await AlertDelivery(db, _registry(ha)).run_once()
    assert ha.calls == []
    [row] = (await alert_store.list_alerts(db))[0]["deliveries"]
    assert (row["status"], row["last_error"], row["attempts"]) == ("skipped", "HA channel off", 0)

    await alert_delivery.set_ha_channel(db, True)
    await alert_store.open_condition(db, "WORKER_STALLED", "stuck", gateway_id="b")
    await AlertDelivery(db, _registry(None)).run_once()
    by_gw = {a["gateway_id"]: a["deliveries"] for a in await alert_store.list_alerts(db)}
    assert by_gw["b"][0]["last_error"] == "no Home Assistant configured"


async def test_acknowledged_before_it_went_out_is_not_sent(db):
    aid = await alert_store.open_condition(db, "GATEWAY_UNREACHABLE", "down", now=T0)
    ha = FakeHa(ok=False)
    d = AlertDelivery(db, _registry(ha))
    await d.run_once(now=T0)
    await alert_store.ack(db, aid, "user:root")
    ha.ok = True
    await d.run_once(now=T0 + 60)
    assert len(ha.calls) == 1
    assert (await _deliveries(db))[0]["status"] == "skipped"


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    conn = await init_db(tmp_path / "api.db")
    await create_user(conn, "root", hash_password("pw"), role="admin")
    await create_user(conn, "vee", hash_password("pw"), role="viewer")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = conn
    app.state.config = SimpleNamespace(environment="dev")
    app.state.ha_registry = _registry(FakeHa())
    app.include_router(auth_router)
    app.include_router(alerts_router)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await conn.close()


async def test_settings_api(client):
    await client.post("/api/auth/login", json={"username": "vee", "password": "pw"})
    r = await client.get("/api/alerts/settings")
    assert r.json() == {"ha_channel": True, "ha_configured": True, "warning": None}
    assert (await client.put("/api/alerts/settings", json={"ha_channel": False})
            ).status_code == 403
    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    r = await client.put("/api/alerts/settings", json={"ha_channel": False})
    assert r.json()["warning"] == "Critical alerts reach only the bridge UI."
    assert (await client.get("/api/alerts/settings")).json()["ha_channel"] is False
