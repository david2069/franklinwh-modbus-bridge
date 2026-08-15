"""Energy-Costs / Tariff tab (Phase B) — GET /api/tariff/overview."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app


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
        # The module is off by default — enable it so the router is reachable.
        r = await ac.patch("/api/modules/energy_costs", json={"enabled": True})
        assert r.status_code == 200, r.text
        yield ac


async def test_module_gated_when_disabled(client):
    # Disabling the module blocks the endpoint (require_module gate).
    await client.patch("/api/modules/energy_costs", json={"enabled": False})
    resp = await client.get("/api/tariff/overview")
    assert resp.status_code == 403
    await client.patch("/api/modules/energy_costs", json={"enabled": True})


async def test_overview_shape_and_live_sensors(client):
    resp = await client.get("/api/tariff/overview")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # top-level shape
    for key in ("config", "services", "period", "live", "linked"):
        assert key in data
    assert set(data["config"]) == {"demand", "bonus", "charge"}
    # live carries only tariff/demand/bonus sensors, each with an id + value key
    ids = {s["id"] for s in data["live"]}
    assert "tariff.export_charge_cost" in ids
    assert "demand.peak_kw" in ids
    assert "bonus.period_credit" in ids
    assert all(s["id"].split(".")[0] in ("tariff", "demand", "bonus") for s in data["live"])
    assert all("value" in s for s in data["live"])


async def test_linked_automations_scanner(client):
    # A schedule that conditions on a tariff sensor shows up under linked;
    # one that doesn't, doesn't.
    tariff_entry = {
        "name": "Solar sponge guard",
        "action": "force_charge",
        "params": {"power_w": 3000},
        "trigger_kind": "daily",
        "trigger_spec": {"time_of_day": "11:00"},
        "entry_conditions": {
            "match": "ALL",
            "conditions": [
                {"sensor": "tariff.export_charge_window_active", "op": "==", "value": True}
            ],
        },
        "enabled": True,
    }
    plain_entry = {
        "name": "SOC only",
        "action": "force_discharge",
        "params": {"power_w": 2000},
        "trigger_kind": "daily",
        "trigger_spec": {"time_of_day": "18:00"},
        "entry_conditions": {
            "match": "ALL",
            "conditions": [{"sensor": "battery.soc_pct", "op": ">", "value": 50}],
        },
    }
    a = (await client.post("/api/schedules", json=tariff_entry)).json()
    await client.post("/api/schedules", json=plain_entry)

    data = (await client.get("/api/tariff/overview")).json()
    linked = {x["name"]: x for x in data["linked"]}
    assert "Solar sponge guard" in linked
    assert "SOC only" not in linked
    row = linked["Solar sponge guard"]
    assert row["sensors"] == ["tariff.export_charge_window_active"]
    assert row["enabled"] is True
    assert row["action"] == "force_charge"
    assert a["id"]  # sanity
