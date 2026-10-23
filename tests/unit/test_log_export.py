"""#320: the app log export — the Logs tab's filter as a file, serials and IPs masked."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.diagnostics_api import router as diag_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store.db import create_gateway, create_user, init_db, insert_logs
from franklinwh_bridge.support import log_export

T0 = 1_792_536_000.0
SERIAL = "TESTSERIAL00000000001"


def test_redactor_masks_serials_and_ips_only():
    known = log_export.known_identifiers(
        [{"id": "gw1", "host": "192.0.2.10", "serial": SERIAL}],
        SimpleNamespace(get=lambda _id: SimpleNamespace(status=SimpleNamespace(serial=None))),
    )
    red = log_export.Redactor(known)
    assert red(f"Connected to aGate {SERIAL} at 192.0.2.10:502") == (
        "Connected to aGate <serial> at <ip>:502")
    # The short id in MQTT topics, and an IP not on file.
    assert red("fwh_mb/franklinwh_00000001/soc via 198.51.100.7") == (
        "fwh_mb/franklinwh_<serial>/soc via <ip>")
    # Everything else reads as it did: loopback, firmware, schedule ids, registers.
    line = ("Modbus write @324 [1000] -> Modbus exception: 3 (schedule:sch_c467603d) "
            "fw V12R02B60D00 on 127.0.0.1")
    assert red(line) == line
    assert red.lines_changed == 2


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    conn = await init_db(tmp_path / "api.db")
    await create_user(conn, "root", hash_password("pw"), role="admin")
    await create_user(conn, "vee", hash_password("pw"), role="viewer")
    await create_gateway(conn, "gw1", "Garage", "192.0.2.10")
    await insert_logs(conn, [
        {"ts": T0, "level": "INFO", "name": "franklinwh_bridge.main",
         "message": "Bridge started", "gateway_id": None},
        {"ts": T0 + 1, "level": "WARNING", "name": "franklinwh_bridge.store.modbus_audit",
         "message": "Modbus write @324 [1000] -> Modbus exception: 3 on 192.0.2.10",
         "gateway_id": "gw1"},
        {"ts": T0 + 2, "level": "WARNING", "name": "franklinwh_bridge.publish.command_handler",
         "message": f"Battery command FAILED for {SERIAL}", "gateway_id": "gw1"},
    ])
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = conn
    app.state.config = SimpleNamespace(environment="dev")
    app.state.registry = SimpleNamespace(
        get=lambda _id: SimpleNamespace(status=SimpleNamespace(serial=SERIAL)))
    app.include_router(auth_router)
    app.include_router(diag_router)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await conn.close()


async def test_export_admin_only(client):
    await client.post("/api/auth/login", json={"username": "vee", "password": "pw"})
    assert (await client.get("/api/diagnostics/logs")).status_code == 403


async def test_export_text_follows_the_filter_masked(client):
    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    r = await client.get("/api/diagnostics/logs?level=WARNING")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "attachment; filename=bridge_log_" in r.headers["content-disposition"]
    text = r.text
    assert "# Filter: level=WARNING" in text
    body = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(body) == 2  # INFO left out; oldest first
    assert "modbus_audit [gw1]" in body[0] and body[0].endswith("exception: 3 on <ip>")
    assert body[1].endswith("Battery command FAILED for <serial>")
    assert "192.0.2.10" not in text and SERIAL not in text


async def test_export_json_search_and_range(client):
    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    j = (await client.get(
        f"/api/diagnostics/logs?format=json&search=exception&start_ts={T0 + 0.5}")).json()
    assert j["schema_version"] == 1 and j["count"] == 1 and j["matched"] == 1
    assert j["filters"] == {"start_ts": T0 + 0.5, "search": "exception"}
    assert j["logs"][0]["level"] == "WARNING" and j["lines_masked"] == 1
    assert j["masked"] == "serials and IP addresses"


async def test_export_keeps_the_newest_past_the_limit(client):
    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    j = (await client.get("/api/diagnostics/logs?format=json&limit=2")).json()
    assert (j["count"], j["matched"]) == (2, 3)
    assert [e["ts"] for e in j["logs"]] == [T0 + 1, T0 + 2]
