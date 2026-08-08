"""Phase 3 slice 2 — creating/editing v2 schedule entries (triggers + conditions)
through the existing /api/schedules surface."""

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
        yield ac


async def test_create_v2_trigger_entry_roundtrips(client):
    body = {
        "name": "Evening discharge",
        "action": "force_discharge",
        "params": {"power_w": 3000},
        "trigger_kind": "daily",
        "trigger_spec": {"time_of_day": "18:00"},
        "duration_s": 7200,
        "exit_conditions": {
            "match": "ANY",
            "conditions": [{"sensor": "battery.soc_pct", "op": "<=", "value": 20}],
        },
        "release_policy": "restore_prior_mode",
    }
    resp = await client.post("/api/schedules", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    sid = created["id"]
    assert created["trigger_kind"] == "daily"
    assert created["trigger_spec"] == {"time_of_day": "18:00"}
    assert created["duration_s"] == 7200
    assert created["exit_conditions"]["conditions"][0]["sensor"] == "battery.soc_pct"
    assert created["entry_conditions"] is None
    assert created["release_policy"] == "restore_prior_mode"
    assert created["next_fire"] is not None  # daily trigger has a next fire

    # round-trips on GET
    got = (await client.get(f"/api/schedules/{sid}")).json()
    assert got["trigger_kind"] == "daily"
    assert got["exit_conditions"]["match"] == "ANY"


async def test_create_entry_with_entry_gate(client):
    body = {
        "name": "Charge when cheap",
        "action": "force_charge",
        "params": {"power_w": 2000},
        "trigger_kind": "always",
        "entry_conditions": {
            "match": "ALL",
            "conditions": [{"sensor": "battery.soc_pct", "op": "<", "value": 30}],
        },
    }
    resp = await client.post("/api/schedules", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["trigger_kind"] == "always"
    assert created["entry_conditions"]["conditions"][0]["op"] == "<"


async def test_patch_v2_fields(client):
    created = (await client.post("/api/schedules", json={
        "name": "x", "action": "force_standby",
        "trigger_kind": "interval", "trigger_spec": {"every_seconds": 3600},
        "duration_s": 600,
    })).json()
    sid = created["id"]

    patched = (await client.patch(f"/api/schedules/{sid}", json={"duration_s": 1800})).json()
    assert patched["duration_s"] == 1800
    assert patched["trigger_kind"] == "interval"  # unchanged


async def test_legacy_window_entry_still_works(client):
    body = {
        "name": "legacy",
        "action": "force_charge",
        "params": {"power_w": 1000},
        "when_spec": {"days": [0, 1], "windows": [{"start": "14:00", "end": "16:00"}]},
    }
    resp = await client.post("/api/schedules", json=body)
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["trigger_kind"] is None
    assert created["when_spec"]["windows"][0]["start"] == "14:00"


async def test_execute_not_found_returns_404(client):
    resp = await client.post("/api/schedules/sch_missing/execute")
    assert resp.status_code == 404


async def test_execute_gated_by_entry_conditions(client):
    # Impossible entry condition → execute is gated, so no real Modbus dispatch
    # happens against the (hardware-less) test gateway. Exercises the endpoint +
    # gate path without hanging on a connection timeout.
    created = (await client.post("/api/schedules", json={
        "name": "exec", "action": "force_standby", "trigger_kind": "always",
        "entry_conditions": {"conditions": [{"sensor": "battery.soc_pct", "op": ">", "value": 200}]},
    })).json()
    resp = await client.post(f"/api/schedules/{created['id']}/execute")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("gated", "no_target")


async def test_log_filter_by_status(client):
    resp = await client.get("/api/schedules/log", params={"status": "executed", "limit": 10})
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert all(e["result"] == "executed" for e in events)
