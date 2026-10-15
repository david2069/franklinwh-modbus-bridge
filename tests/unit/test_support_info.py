"""GET /api/support/info: useful, admin-only, and free of personal details.

The install is seeded with fictitious identifying values everywhere they can
live — gateway row, live status, MQTT config, an HA instance, users, the site,
the PV location and a log line quoting all of them — and none may appear in
the JSON or the Markdown.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app

HOST = "192.0.2.10"
SERIAL = "TESTSERIAL00000000001"
SHORT_ID = SERIAL[-8:]
GW_NAME = "Garage Battery"
HA_URL = "http://ha.example:8123"
HA_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJmYWtlIjoidGVzdCJ9.ZmFrZXNpZ25hdHVyZQ"
HA_NAME = "Holiday House HA"
BROKER = "broker.example"
MQTT_USER = "mqtt-person"
MQTT_PASS = "not-a-real-password"
USERNAME = "jdoe_test"
EMAIL = "jdoe@example.com"
SITE = "Smith Residence"
LAT, LON = 12.34, 56.78

PII = [
    HOST,
    SERIAL,
    SHORT_ID,
    GW_NAME,
    HA_URL,
    "ha.example",
    HA_TOKEN,
    HA_NAME,
    BROKER,
    MQTT_USER,
    MQTT_PASS,
    USERNAME,
    EMAIL,
    SITE,
    str(LAT),
    str(LON),
    "/Users/jdoe",
]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def _seed_pii() -> str:
    from franklinwh_bridge.security import hash_password
    from franklinwh_bridge.store.db import create_user, get_mqtt_config, set_mqtt_config

    db = app.state.db
    await db.execute(
        "UPDATE gateways SET host = ?, serial = ?, name = ?, model = ?, firmware = ? "
        "WHERE id = 'default'",
        (HOST, SERIAL, GW_NAME, "aGate X-01", "V10R01B04D00"),
    )
    await db.execute(
        "INSERT INTO ha_instances (id, name, base_url, token, is_default, enabled, "
        "created_at, updated_at) VALUES ('ha1', ?, ?, ?, 1, 1, 0, 0)",
        (HA_NAME, HA_URL, HA_TOKEN),
    )
    await db.execute("UPDATE site_config SET name = ? WHERE id = 1", (SITE,))
    await db.execute(
        "INSERT INTO logs (ts, level, name, message, gateway_id) VALUES (?, ?, ?, ?, ?)",
        (
            time.time() - 60,
            "ERROR",
            "franklinwh_bridge.modbus.poller",
            f"Gateway default: {GW_NAME} {SERIAL} at {HOST}:502 failed; topic "
            f"franklinwh_{SHORT_ID}/soc; HA {HA_URL} token {HA_TOKEN}; broker {BROKER} "
            f"user {MQTT_USER}; by {USERNAME} <{EMAIL}>; site {SITE} at {LAT},{LON}; "
            "file /Users/jdoe/x.py",
            "default",
        ),
    )
    await db.commit()
    await set_mqtt_config(db, {"host": BROKER, "username": MQTT_USER, "password": MQTT_PASS})
    for name in (USERNAME, EMAIL):
        await create_user(
            db, username=name, password_hash=hash_password("pw12345678"), role="viewer"
        )
    app.state.solar_forecast.settings.pv_latitude = LAT
    app.state.solar_forecast.settings.pv_longitude = LON
    inst = app.state.registry.get("default")
    if inst is not None:
        inst.status.serial = SERIAL
        inst.status.last_error = f"timeout talking to {HOST}:502 ({SERIAL})"
    # In memory, not yet flushed: the bundle must scrub these too.
    app.state.log_buffer.append(
        {
            "ts": time.time(),
            "level": "WARNING",
            "name": "franklinwh_bridge.publish",
            "message": f"MQTT {BROKER}:1883 as {MQTT_USER} lost",
            "gateway_id": "",
        }
    )
    return (await get_mqtt_config(db))["client_id"]


async def test_bundle_has_no_personal_details(client):
    client_id = await _seed_pii()
    resp = await client.get("/api/support/info")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    blob = json.dumps(body)
    for value in [*PII, client_id]:
        assert value not in blob, f"leaked: {value}"
        assert value not in body["markdown"], f"leaked in markdown: {value}"


async def test_bundle_still_says_something_useful(client):
    await _seed_pii()
    body = (await client.get("/api/support/info")).json()
    info, md = body["info"], body["markdown"]
    from franklinwh_bridge import __version__

    assert info["bridge"]["version"] == __version__
    assert info["bridge"]["environment"] == "dev"
    assert info["bridge"]["schema_version"] > 0
    assert info["bridge"]["install_date"]
    assert info["bridge"]["utc_offset"].startswith("UTC")
    assert "timezone" not in info["bridge"]  # zone name omitted, like the Local Bridge
    gw = info["gateways"][0]
    assert gw["label"] == "gateway 1"
    assert gw["model"] == "aGate X-01" and gw["firmware"] == "V10R01B04D00"
    assert set(gw) >= {"health", "poll_interval_s", "home_load_source", "publish_to_ha"}
    assert info["mqtt"]["topic_prefix"] and "host" not in info["mqtt"]
    assert info["home_assistant"]["instances"] == 1
    assert info["features"]["solar_forecast"]["configured"] is True
    assert info["features"]["users_by_role"]["viewer"] == 2
    problems = info["recent_problems"]
    assert problems["errors"] >= 1 and problems["warnings"] >= 1
    last = " ".join(e["message"] for e in problems["last"])
    assert "<serial>" in last and "<ip>" in last and "<url>" in last
    assert "<gateway 1>" in last
    assert md.startswith("### Support info") and "review before posting" in md.lower()


async def test_install_date_is_set_once(client):
    from franklinwh_bridge.store.db import get_app_config, set_app_config
    from franklinwh_bridge.support.bundle import INSTALL_DATE_KEY, ensure_install_date

    db = app.state.db
    first = await get_app_config(db, INSTALL_DATE_KEY)
    assert first
    await set_app_config(db, INSTALL_DATE_KEY, "2020-01-02")
    assert await ensure_install_date(db) == "2020-01-02"


@pytest.fixture
def real_auth():
    from franklinwh_bridge.api.auth import get_current_user, require_auth

    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.parametrize("role", ["viewer", "user", "api"])
async def test_admin_only(real_auth, client, role):
    from franklinwh_bridge.security import hash_password
    from franklinwh_bridge.store.db import create_user

    assert (await client.get("/api/support/info")).status_code == 401
    await create_user(
        app.state.db, username=f"s_{role}", password_hash=hash_password("pw12345678"), role=role
    )
    r = await client.post(
        "/api/auth/login", json={"username": f"s_{role}", "password": "pw12345678"}
    )
    assert r.status_code == 200, r.text
    assert (await client.get("/api/support/info")).status_code == 403


async def test_admin_may_read(real_auth, client):
    from franklinwh_bridge.security import hash_password
    from franklinwh_bridge.store.db import create_user

    await create_user(
        app.state.db, username="s_admin", password_hash=hash_password("pw12345678"), role="admin"
    )
    r = await client.post("/api/auth/login", json={"username": "s_admin", "password": "pw12345678"})
    assert r.status_code == 200, r.text
    assert (await client.get("/api/support/info")).status_code == 200


async def test_bundle_covers_a_demo_gateway(client):
    """A mock gateway's poller is a MockPoller, without the real poller's
    attributes; the bundle must still build (it 500'd on battery_port_count)."""
    r = await client.post("/api/setup/demo", json={})
    assert r.status_code in (200, 201), r.text
    for _ in range(100):
        if app.state.registry.get("demo") is not None:
            break
        await asyncio.sleep(0.05)
    resp = await client.get("/api/support/info")
    assert resp.status_code == 200, resp.text
    demo = [g for g in resp.json()["info"]["gateways"] if g["mock"]]
    assert demo and demo[0]["running"] is True


async def test_startup_disclaimer_is_not_a_problem(client):
    """The notice is a WARNING at every start by design, and 40 lines long."""
    body = (await client.get("/api/support/info")).json()
    assert not any("PLEASE READ" in e["message"] for e in body["info"]["recent_problems"]["last"])
    assert "PLEASE READ" not in body["markdown"]
