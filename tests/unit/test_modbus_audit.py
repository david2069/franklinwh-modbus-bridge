"""#342 part 1: every SunSpec write is recorded as sent and as answered, with who asked."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from franklinwh_bridge.api.auth import router as auth_router
from franklinwh_bridge.api.diagnostics_api import router as diag_router
from franklinwh_bridge.security import hash_password
from franklinwh_bridge.store import modbus_audit
from franklinwh_bridge.store.db import create_user, init_db

sunspec2 = pytest.importorskip("sunspec2.modbus.client")
modbus = pytest.importorskip("sunspec2.modbus.modbus")

T0 = 1_792_536_000.0


@pytest.fixture
async def db(tmp_path):
    modbus_audit._queue.clear()
    conn = await init_db(tmp_path / "audit.db")
    yield conn
    await conn.close()


@pytest.fixture
def device(monkeypatch):
    """The bridge's SunSpec device class, its client replaced by a fake aGate."""
    from franklinwh_modbus import controller

    from franklinwh_bridge.gateway.instance import prefer_sunspec_base_0

    monkeypatch.setattr(controller, "SunSpecModbusClientDeviceTCP",
                        sunspec2.SunSpecModbusClientDeviceTCP)
    prefer_sunspec_base_0()
    dev = controller.SunSpecModbusClientDeviceTCP(slave_id=1, ipaddr="192.0.2.50", ipport=502)

    def fake_write(addr, data):  # V12: WSetPct above raw 100 refused
        value = int.from_bytes(data[:2], "big", signed=True)
        if addr == 324 and abs(value) > 100:
            raise modbus.ModbusClientException(
                "Modbus exception 3: addr: 324 count: 1")

    dev.client = SimpleNamespace(write=fake_write)
    return dev


async def test_writes_recorded_with_reply_and_initiator(db, device):
    with modbus_audit.writing_as("default", "schedule:sch_1"):
        await asyncio.to_thread(device.write, 324, (100).to_bytes(2, "big", signed=True))
        with pytest.raises(Exception, match="exception 3"):
            await asyncio.to_thread(device.write, 324, (500).to_bytes(2, "big", signed=True))
    device.write(318, (0).to_bytes(2, "big"))  # no context: the library's own write

    assert await modbus_audit.flush(db) == 3
    ok, refused, lib = await modbus_audit.query(db)
    assert (ok["address"], ok["values"], ok["result"], ok["initiator"], ok["gateway_id"]) == (
        324, [100], "ok", "schedule:sch_1", "default")
    assert refused["values"] == [500] and refused["result"] == "error"
    assert "Modbus exception 3" in refused["detail"]
    assert lib["initiator"] == "library" and lib["address"] == 318


async def test_negative_values_and_multi_register(db):
    modbus_audit.record_write(318, b"\x00\x01\x00\x00\xff\x9c", None, now=T0)
    await modbus_audit.flush(db)
    [row] = await modbus_audit.query(db)
    assert row["values"] == [1, 0, -100]


async def test_prune_by_age_and_row_cap(db, monkeypatch):
    modbus_audit.record_write(1, b"\x00\x01", None, now=T0 - modbus_audit.RETENTION_S - 1)
    for i in range(5):
        modbus_audit.record_write(2, b"\x00\x02", None, now=T0 + i)
    await modbus_audit.flush(db)
    monkeypatch.setattr(modbus_audit, "MAX_ROWS", 3)
    assert await modbus_audit.prune(db, now=T0) == 3  # 1 by age + 2 over the cap
    assert [r["ts"] for r in await modbus_audit.query(db)] == [T0 + 2, T0 + 3, T0 + 4]


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    modbus_audit._queue.clear()
    conn = await init_db(tmp_path / "api.db")
    await create_user(conn, "root", hash_password("pw"), role="admin")
    await create_user(conn, "vee", hash_password("pw"), role="viewer")
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.db = conn
    app.state.config = SimpleNamespace(environment="dev")
    app.include_router(auth_router)
    app.include_router(diag_router)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    await conn.close()


async def test_export_admin_only_json_and_csv(client):
    with modbus_audit.writing_as("default", "user:root"):
        modbus_audit.record_write(324, (100).to_bytes(2, "big"), None, now=T0)
    await client.post("/api/auth/login", json={"username": "vee", "password": "pw"})
    assert (await client.get("/api/diagnostics/modbus-writes")).status_code == 403
    await client.post("/api/auth/login", json={"username": "root", "password": "pw"})
    j = (await client.get("/api/diagnostics/modbus-writes")).json()
    assert j["schema_version"] == 1 and j["count"] == 1  # the queued write was flushed
    assert j["writes"][0]["initiator"] == "user:root"
    r = await client.get("/api/diagnostics/modbus-writes?format=csv")
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text.splitlines()[1].endswith(',default,user:root,324,[100],ok,')
