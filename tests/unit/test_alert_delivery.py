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
    assert r.json() == {"ha_channel": True, "ha_configured": True, "warning": None,
                        "status_notices": True}
    assert (await client.put("/api/alerts/settings", json={"ha_channel": False})
            ).status_code == 403
    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    r = await client.put("/api/alerts/settings", json={"ha_channel": False})
    assert r.json()["warning"] == "Critical alerts reach only the bridge UI."
    assert (await client.get("/api/alerts/settings")).json()["ha_channel"] is False


# ── #499 step 3: the alert on the phones, with Acknowledge (as DC) ─────────────


async def test_the_phones_are_asked_once_on_the_first_try_whatever_ha_does(db):
    ha = FakeHa(ok=False)
    asked = []

    async def ask(a):
        asked.append(a["id"])
        return ""

    aid = await alert_store.raise_event(db, "COMMAND_REFUSED", "Force Charge refused",
                                        gateway_id="gw1", now=T0)
    d = AlertDelivery(db, _registry(ha))
    d.ask_phones = ask
    await d.run_once(now=T0 + 1)          # HA fails: the phones are still asked
    await d.run_once(now=T0 + 3600)       # a retry does not ask them again
    assert asked == [aid]
    assert "tap Acknowledge on your phone" in ha.calls[0][1]["message"]


async def test_an_alert_acknowledged_before_it_went_out_asks_nobody(db):
    asked = []

    async def ask(a):
        asked.append(a["id"])
        return ""

    aid = await alert_store.raise_event(db, "COMMAND_REFUSED", "x", now=T0)
    await alert_store.ack(db, aid, "user:admin", now=T0)
    d = AlertDelivery(db, _registry(FakeHa()))
    d.ask_phones = ask
    await d.run_once(now=T0 + 1)
    assert asked == []


async def test_acknowledge_tapped_on_a_phone_acknowledges_for_everyone(db, tmp_path):
    from franklinwh_bridge.actionable import ACK, Actionable

    aid = await alert_store.raise_event(db, "COMMAND_REFUSED", "Force Charge refused", now=T0)
    act = Actionable(db, tmp_path / "unused.db")
    req = SimpleNamespace(meta={"alert_id": aid})
    assert await act._on_alert_tap(req, SimpleNamespace(verb=ACK)) == "acknowledged"
    again = await act._on_alert_tap(req, SimpleNamespace(verb=ACK))
    assert again == "already acknowledged, or gone"
    [a] = await alert_store.list_alerts(db)
    assert a["acked_by"] == "source:ha"
    gone = SimpleNamespace(meta={"alert_id": 99999})
    missing = await act._on_alert_tap(gone, SimpleNamespace(verb=ACK))
    assert missing == "already acknowledged, or gone"


async def test_ask_alert_goes_to_phones_only_and_follows_the_switch(db, tmp_path):
    from franklinwh_ha_notify import Target

    from franklinwh_bridge.actionable import EVENT_ALERT, Actionable

    sent = []

    class FakeNotifier:
        async def ask(self, event, targets, buttons, **kw):
            sent.append((event, [t.service for t in targets], [b.title for b in buttons], kw))
            return SimpleNamespace(id="r1")

    act = Actionable(db, tmp_path / "unused.db")
    act.notifier = FakeNotifier()
    act._enabled = True

    async def targets(ids=None):
        return [Target("ha1", "mobile_app_pixel", "Pixel"), Target("ha1", "living_room_tv", "TV")]

    act.targets = targets
    alert = {"id": 7, "title": "aGate unreachable", "message": "No answer for 5 min",
             "gateway_id": "gw1"}
    req, why = await act.ask_alert(alert, None)
    assert req.id == "r1" and why == ""
    event, services, buttons, kw = sent[0]
    assert (event, services, buttons) == (EVENT_ALERT, ["mobile_app_pixel"], ["Acknowledge"])
    assert kw["meta"] == {"alert_id": 7} and kw["ttl_s"] == 3600
    act._enabled = False
    assert await act.ask_alert(alert, None) == (None, "Push notifications are off")


# ── #499 step 6: Test critical alert (as DC #464) ──────────────────────────────


async def test_test_critical_alert_reports_each_step_and_the_tap(db, tmp_path):
    from franklinwh_ha_notify import Target

    from franklinwh_bridge.actionable import EVENT_TEST_ALERT, TEST_ALERT_ID, Actionable

    asked = []

    class FakeNotifier:
        async def ask(self, event, targets, buttons, **kw):
            asked.append((event, [t.service for t in targets], [b.title for b in buttons]))
            return SimpleNamespace(id="t1")

    act = Actionable(db, tmp_path / "unused.db")
    act.notifier = FakeNotifier()
    act._enabled = True

    async def targets(ids=None):
        return [Target("ha1", "mobile_app_pixel", "Pixel"), Target("ha1", "tv", "TV")]

    act.targets = targets
    ha = FakeHa()
    out = await act.test_alert(ha, None)
    states = [out["steps"][k]["state"] for k in ("ha", "devices", "ack")]
    assert states == ["pass", "pass", "waiting"]
    assert out["request_id"] == "t1" and out["ttl_s"] == 300
    assert ha.calls[0][0] == "persistent_notification.create"
    assert ha.calls[0][1]["notification_id"] == TEST_ALERT_ID
    assert asked == [(EVENT_TEST_ALERT, ["mobile_app_pixel"], ["Acknowledge"])]
    assert act.test_alert_result("t1") == {"answered": False, "expired": False}
    await act._on_test_alert_tap(SimpleNamespace(id="t1"), SimpleNamespace(verb="ACK"))
    assert act.test_alert_result("t1")["answered"] is True
    assert ha.calls[-1] == ("persistent_notification.dismiss", {"notification_id": TEST_ALERT_ID})
    assert await alert_store.list_alerts(db) == []          # no alert recorded

    act._enabled = False
    off = await act.test_alert(None, None)
    assert off["steps"]["ha"]["state"] == "unset"
    assert off["steps"]["devices"]["text"] == "Push notifications are off"
    assert off["steps"]["ack"]["state"] == "skipped" and off["request_id"] is None
