"""No route falls back to the gateway with ID 'default' (#296 item 43): a
request that names no gateway gets 400 with the reason, logged; a malformed
one 422, an unknown one 404; a serial resolves to its gateway."""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app

#: (method, path, json body or None) for every route that used to fall back.
NO_GATEWAY = [
    ("GET", "/api/points", None),
    ("GET", "/api/points/soc", None),
    ("GET", "/api/battery/limits", None),
    ("GET", "/api/gateway", None),
    ("GET", "/api/gateway/test", None),
    ("GET", "/api/models", None),
    ("GET", "/api/catalog/points", None),
    ("GET", "/api/models/export", None),
    ("POST", "/api/models/1/read", None),
    ("POST", "/api/models/refresh", None),
    ("GET", "/api/sensors", None),
    ("POST", "/api/scheduler/evaluate", {"match": "all", "conditions": []}),
    ("GET", "/api/point-history/series?points=soc", None),
    ("GET", "/api/energy/flow", None),
    ("GET", "/api/energy/battery-hours", None),
    ("GET", "/api/energy/totals", None),
    ("GET", "/api/tariff/overview", None),
    ("GET", "/api/tariff/history", None),
    ("GET", "/api/tariff/history.csv", None),
    ("GET", "/api/solar/forecast", None),
    ("POST", "/api/command", {"slug": "battery_command", "value": "Stop"}),
    ("POST", "/api/sequence/execute", {"inline": '{"DERCtlAC.WSetEna": 0}', "dry_run": True}),
    ("POST", "/api/reader/run", {"raw": "15500:1"}),
]


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


async def test_every_route_needs_a_named_gateway(client, caplog):
    for module in ("energy_costs", "explorer"):
        r = await client.patch(f"/api/modules/{module}", json={"enabled": True})
        assert r.status_code == 200, module
    caplog.set_level(logging.WARNING, logger="franklinwh_bridge.api.gateway_ref")
    for method, path, body in NO_GATEWAY:
        r = await client.request(method, path, json=body)
        assert r.status_code == 400, (path, r.status_code, r.text)
        assert r.json()["detail"].startswith("This request names no gateway: add "), path
    logged = [m for m in caplog.messages if "rejected (400): This request names no gateway" in m]
    assert len(logged) == len(NO_GATEWAY)

    # Named: the same routes answer for that gateway.
    r = await client.get("/api/points?gateway=default")
    assert r.status_code == 200 and r.json()["gateway_id"] == "default"
    r = await client.get("/api/gateway?gateway_id=default")
    assert r.status_code == 200 and r.json()["gateway_id"] == "default"


async def test_malformed_unknown_and_serial(client):
    r = await client.get("/api/points?gateway=nope")
    assert r.status_code == 404 and r.json()["detail"] == "No gateway has the ID or serial 'nope'"
    r = await client.get("/api/points", params={"gateway": "a\\b"})
    assert r.status_code == 422 and r.json()["detail"].startswith("Malformed gateway reference")
    r = await client.post("/api/command", json={"slug": "battery_command", "value": "Stop",
                                                "gateway_id": "nope"})
    assert r.status_code == 404

    from franklinwh_bridge.store.db import update_gateway
    await update_gateway(app.state.db, "default", serial="<serial8>")
    r = await client.get("/api/points?gateway=<serial8>")
    assert r.status_code == 200 and r.json()["gateway_id"] == "default"
