"""First-run setup wizard API (docs/setup-wizard-design.md).

Discovery itself is franklinwh-modbus' (tested there, and against a real aGate);
here ``discovery.probe`` / ``discovery.scan`` are replaced with fakes, so these
tests cover the bridge's side: which subnets, how results are classified, what
connect/demo configure, and that a polled gateway is never probed.

API tests share one app boot where they can: each lifespan boot is slow.
"""

from __future__ import annotations

import asyncio

import pytest
from franklinwh_modbus import discovery
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.api import setup_api
from franklinwh_bridge.config import environment
from franklinwh_bridge.main import app
from franklinwh_bridge.store.db import (
    MIGRATIONS,
    get_app_config,
    init_db,
)

AGATE = dict(
    status=discovery.SUNSPEC, base_address=0,
    manufacturer="FranklinWH Technologies Co., Ltd", model="aGate X",
    serial="10060006A02F24170091", version="V10R01B04D00",
)


def _result(host: str, **kw) -> discovery.DiscoveryResult:
    return discovery.DiscoveryResult(host=host, port=502, **{"status": discovery.UNKNOWN, **kw})


# ── subnets ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("addr", "prefix", "expected"), [
    ("192.168.0.50", 24, "192.168.0.0/24"),
    ("10.1.2.3", 16, "10.1.2.0/24"),          # wider → the /24 containing it
    ("192.168.4.9", 22, "192.168.4.0/24"),
    ("192.168.1.130", 25, "192.168.1.128/25"),  # narrower stays narrow
    ("127.0.0.1", 8, None),
    ("169.254.3.4", 16, None),
    ("100.101.102.103", 10, None),            # Tailscale: not a LAN to guess
    ("8.8.8.8", 24, None),
    ("not-an-ip", 24, None),
])
def test_lan_subnet_for(addr, prefix, expected):
    assert environment.lan_subnet_for(addr, prefix) == expected


async def test_candidate_subnets_docker_prefers_the_browser_address(monkeypatch):
    monkeypatch.setenv("APP_ENV", "docker")
    monkeypatch.setenv("LAN_SUBNET", "10.9.8.0/24, garbage")
    monkeypatch.setattr(environment, "_primary_ipv4", lambda: "172.18.0.5")  # Docker bridge
    got = await environment.candidate_subnets("192.168.0.50:8100")
    assert got == [
        {"subnet": "192.168.0.0/24", "source": "browser_address"},
        {"subnet": "10.9.8.0/24", "source": "lan_subnet_env"},
    ]
    # Opened via localhost: nothing to learn from the Host header.
    monkeypatch.delenv("LAN_SUBNET")
    assert await environment.candidate_subnets("localhost:8100") == []


async def test_candidate_subnets_docker_host_networking(monkeypatch):
    monkeypatch.setenv("APP_ENV", "docker")
    monkeypatch.delenv("LAN_SUBNET", raising=False)
    monkeypatch.setattr(environment, "_primary_ipv4", lambda: "192.168.0.247")
    got = await environment.candidate_subnets("127.0.0.1:8100")
    assert got == [{"subnet": "192.168.0.0/24", "source": "host_network"}]


async def test_candidate_subnets_addon_asks_the_supervisor(monkeypatch):
    from franklinwh_bridge.config import supervisor

    async def fake_info():
        return {"interfaces": [
            {"enabled": True, "connected": True, "ipv4": {"address": ["192.168.4.20/22"]}},
            {"enabled": True, "connected": False, "ipv4": {"address": ["10.0.0.2/24"]}},
            {"enabled": True, "connected": True, "ipv4": {"address": []}},
        ]}

    monkeypatch.setenv("APP_ENV", "ha_addon")
    monkeypatch.setattr(supervisor, "supervisor_network_info", fake_info)
    got = await environment.candidate_subnets("homeassistant.local:8123")
    assert got == [{"subnet": "192.168.4.0/24", "source": "supervisor"}]


@pytest.mark.parametrize(("raw", "expected"), [
    ("192.168.0.0/24", ("192.168.0.0/24", False)),
    ("192.168.0.77/16", ("192.168.0.0/24", False)),
    ("192.168.5.0/16", ("192.168.5.0/24", False)),
    ("100.64.1.0/24", ("100.64.1.0/24", True)),
])
def test_normalise_subnet(raw, expected):
    assert setup_api.normalise_subnet(raw) == expected


@pytest.mark.parametrize("raw", ["8.8.8.0/24", "fd00::/120", "nonsense"])
def test_normalise_subnet_refuses(raw):
    with pytest.raises(setup_api.HTTPException) as exc:
        setup_api.normalise_subnet(raw)
    assert exc.value.status_code == 400


# ── classification ────────────────────────────────────────────────────────────

def test_result_rows():
    row = setup_api.result_row(_result("192.168.0.110", **AGATE))
    assert row["kind"] == "agate"
    assert row["device_type"] == "agate"
    assert row["suggested_name"] == "aGate 0091"

    mac = setup_api.result_row(_result("192.168.0.111", **{**AGATE, "model": "MAC-1"}))
    assert (mac["device_type"], mac["suggested_name"]) == ("mac1", "MAC-1 0091")

    other = setup_api.result_row(_result(
        "192.168.0.20", status=discovery.SUNSPEC, manufacturer="SolarEdge", model="SE10K"))
    assert other["kind"] == "sunspec_other"

    unknown = setup_api.result_row(_result("192.168.0.30", error="no Modbus reply"))
    assert unknown["kind"] == "unknown"
    assert unknown["summary"] == "Unknown device listening on TCP port 502 at 192.168.0.30"


# ── migration 49 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(("rows", "expected"), [
    ([], "pending"),
    ([("default", "192.168.0.110", 0, 1_700_000_000.0)], "done_real"),
    ([("default", "", 0, None), ("demo", "mock", 1, None)], "done_demo"),
    ([("default", "192.168.0.110", 0, None)], "pending"),  # never connected
])
async def test_migration_49_existing_installs_skip_the_wizard(tmp_path, rows, expected):
    db = await init_db(tmp_path / "bridge.db")
    try:
        # Fresh database: no gateways yet.
        assert await get_app_config(db, "setup_state") == "pending"
        # Re-run the migration as an upgrade would, over an existing install.
        await db.execute("DELETE FROM app_config WHERE key = 'setup_state'")
        await db.execute("DELETE FROM gateways")
        for gw_id, host, mock, connected in rows:
            await db.execute(
                "INSERT INTO gateways (id, name, host, mock, last_connected_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (gw_id, gw_id, host, mock, connected),
            )
        await db.commit()
        await db.executescript(MIGRATIONS[49])
        assert await get_app_config(db, "setup_state") == expected
    finally:
        await db.close()


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture
async def fresh_client(tmp_path, monkeypatch):
    """A fresh install: no MODBUS_HOST, so the primary gateway is unconfigured."""
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("MODBUS_HOST", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def _wait_scan(client, scan_id: str) -> dict:
    for _ in range(100):
        body = (await client.get(f"/api/setup/scan/{scan_id}")).json()
        if body["status"] != "running":
            return body
        await asyncio.sleep(0.05)
    raise AssertionError("scan did not finish")


async def test_wizard_real_path_end_to_end(fresh_client, monkeypatch):
    client = fresh_client
    probed: list[str] = []

    def fake_probe(host, port=502, unit_id=1, timeout=2.0, attempts=1, **kw):
        probed.append(host)
        if host == "192.0.2.110":
            return _result(host, **AGATE)
        return _result(host, error="no Modbus reply")

    monkeypatch.setattr(discovery, "probe", fake_probe)

    state = (await client.get("/api/setup/state")).json()
    assert state["state"] == "pending"
    assert state["has_real_gateway"] is False

    # Enter address → Test.
    row = (await client.post("/api/setup/probe", json={"host": "192.0.2.110"})).json()
    assert row["kind"] == "agate" and row["suggested_name"] == "aGate 0091"

    # Confirm: fills the primary gateway (id "default") and starts it.
    resp = await client.post("/api/setup/connect", json={
        "host": "192.0.2.110", "name": "aGate 0091", "device_type": "agate"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["gateway_id"] == "default"
    assert body["gateway"]["host"] == "192.0.2.110"
    assert body["gateway"]["name"] == "aGate 0091"
    assert body["gateway"]["description"] == ""   # no "Not configured" left behind
    assert app.state.registry.get("default") is not None
    assert (await client.get("/api/setup/state")).json()["state"] == "done_real"

    # The same aGate twice is refused.
    resp = await client.post("/api/setup/connect", json={
        "host": "192.0.2.110", "name": "again"})
    assert resp.status_code == 409

    # A polled gateway's address is reported, never probed.
    probed.clear()
    row = (await client.post("/api/setup/probe", json={"host": "192.0.2.110"})).json()
    assert row["kind"] == "configured" and row["gateway_id"] == "default"
    assert probed == []

    # A second aGate is added alongside, not over the primary.
    resp = await client.post("/api/setup/connect", json={
        "host": "192.0.2.111", "name": "Garage aGate"})
    assert resp.json()["gateway_id"] == "garage-agate"

    # A demo after a real setup leaves the state as done_real.
    resp = await client.post("/api/setup/demo", json={"name": "Demo aGate", "ac_type": 1})
    assert resp.status_code == 200, resp.text
    demo = resp.json()["gateway"]
    assert resp.json()["gateway_id"] == "demo" and demo["mock"]
    assert (await client.get("/api/setup/state")).json()["state"] == "done_real"


async def test_wizard_scan_demo_skip_and_checklist(fresh_client, monkeypatch):
    client = fresh_client
    swept: list[str] = []

    def fake_scan(subnet, *, port_check, on_result, allow_public=False, **kw):
        swept.append(subnet)
        for host in ("192.168.77.110", "192.168.77.20"):
            if port_check(host, 502, 0.3):
                on_result(_result(host, **AGATE) if host.endswith(".110")
                          else _result(host, error="no Modbus reply"))
        return []

    monkeypatch.setattr(discovery, "scan", fake_scan)
    monkeypatch.setattr(discovery, "port_open", lambda h, p, t: True)

    assert (await client.post("/api/setup/scan", json={"subnets": ["8.8.8.0/24"]})).status_code == 400

    resp = await client.post("/api/setup/scan", json={"subnets": ["192.168.77.5/16"]})
    assert resp.status_code == 202
    body = await _wait_scan(client, resp.json()["scan_id"])
    assert swept == ["192.168.77.0/24"]
    assert body["status"] == "done"
    assert [(r["host"], r["kind"]) for r in body["results"]] == [
        ("192.168.77.20", "unknown"), ("192.168.77.110", "agate")]

    # Demo path from a fresh install.
    resp = await client.post("/api/setup/demo", json={})
    assert resp.status_code == 200, resp.text
    assert (await client.get("/api/setup/state")).json()["state"] == "done_demo"
    # The primary gateway is untouched: still unconfigured, never a mock.
    primary = (await client.get("/api/gateways/default")).json()
    assert primary["host"] == "" and not primary["mock"]

    # Skip / run again.
    resp = await client.post("/api/setup/state", json={"state": "skipped"})
    assert resp.json()["state"] == "skipped"
    assert (await client.post("/api/setup/state", json={"state": "bogus"})).status_code == 422

    checklist = (await client.get("/api/setup/checklist")).json()
    ids = [i["id"] for i in checklist["items"]]
    assert ids == ["mqtt_broker", "ha_connection", "ha_mqtt_integration", "ha_entities"]
    assert all(i["status"] in ("ok", "fail", "unknown") for i in checklist["items"])


async def test_setup_writes_are_admin_only(fresh_client):
    from franklinwh_bridge.api.auth import get_current_user, require_auth

    viewer = {"id": "v", "username": "v", "role": "viewer", "enabled": True}
    app.dependency_overrides[require_auth] = lambda: viewer
    app.dependency_overrides[get_current_user] = lambda: viewer
    client = fresh_client
    assert (await client.get("/api/setup/state")).status_code == 200
    for method, path, payload in [
        ("post", "/api/setup/state", {"state": "skipped"}),
        ("get", "/api/setup/subnets", None),
        ("post", "/api/setup/probe", {"host": "192.0.2.1"}),
        ("post", "/api/setup/connect", {"host": "192.0.2.1", "name": "x"}),
        ("post", "/api/setup/demo", {}),
        ("get", "/api/setup/checklist", None),
    ]:
        kwargs = {"json": payload} if payload is not None else {}
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 403, (path, resp.status_code)
