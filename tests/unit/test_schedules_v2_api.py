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
        "entry_conditions": {
            "conditions": [{"sensor": "battery.soc_pct", "op": ">", "value": 200}]
        },
    })).json()
    resp = await client.post(f"/api/schedules/{created['id']}/execute")
    assert resp.status_code == 200
    assert resp.json()["status"] in ("gated", "no_target")


async def test_log_filter_by_status(client):
    resp = await client.get("/api/schedules/log", params={"status": "executed", "limit": 10})
    assert resp.status_code == 200
    events = resp.json()["events"]
    assert all(e["result"] == "executed" for e in events)


async def test_patch_can_clear_exit_conditions(client):
    """Regression: deleting all exit_conditions (client sends null) must persist.

    The old PATCH filtered `if v is not None`, silently dropping the null so a
    cleared exit condition never saved. exclude_unset now honours explicit nulls.
    """
    created = (await client.post("/api/schedules", json={
        "name": "clearable", "action": "force_discharge", "trigger_kind": "daily",
        "trigger_spec": {"time_of_day": "18:00"}, "duration_s": 3600,
        "exit_conditions": {
            "match": "ALL",
            "conditions": [{"sensor": "battery.soc_pct", "op": "<=", "value": 20}],
        },
    })).json()
    sid = created["id"]
    assert created["exit_conditions"]["conditions"], "precondition: has an exit cond"

    # Clear it — the frontend sends exit_conditions: null when the last row is removed.
    patched = await client.patch(f"/api/schedules/{sid}", json={"exit_conditions": None})
    assert patched.status_code == 200, patched.text
    assert patched.json()["exit_conditions"] is None, "exit_conditions should be cleared"

    # And it stays cleared on a fresh GET.
    got = (await client.get(f"/api/schedules/{sid}")).json()
    assert got["exit_conditions"] is None


async def test_patch_partial_leaves_other_fields_untouched(client):
    """exclude_unset: a partial PATCH (e.g. just `enabled`) must not wipe other
    fields — only what the client sent is applied."""
    created = (await client.post("/api/schedules", json={
        "name": "keep", "action": "force_discharge", "trigger_kind": "daily",
        "trigger_spec": {"time_of_day": "18:00"}, "duration_s": 3600,
        "exit_conditions": {
            "match": "ALL",
            "conditions": [{"sensor": "battery.soc_pct", "op": "<=", "value": 20}],
        },
    })).json()
    sid = created["id"]
    patched = (await client.patch(f"/api/schedules/{sid}", json={"enabled": False})).json()
    assert patched["enabled"] is False
    assert patched["exit_conditions"]["conditions"][0]["value"] == 20  # untouched
    assert patched["duration_s"] == 3600


async def test_switch_trigger_type_clears_stale_when_spec(client):
    """Switching a one-time windowed entry to a daily trigger must drop the stale
    when_spec.date/windows — else the entry renders as a broken mixed state
    (daily trigger + leftover one-time date). The editor now sends both fields."""
    created = (await client.post("/api/schedules", json={
        "name": "was one-time", "action": "force_discharge",
        "when_spec": {"date": "2026-08-07", "windows": [{"start": "05:00", "end": "06:00"}]},
    })).json()
    sid = created["id"]
    assert created["when_spec"]["date"] == "2026-08-07"

    # Switch to a daily trigger + clear the window spec (what saveEntry now sends).
    patched = (await client.patch(f"/api/schedules/{sid}", json={
        "action": "force_charge",
        "trigger_kind": "daily", "trigger_spec": {"time_of_day": "06:00"},
        "when_spec": {"days": [], "windows": []},
    })).json()
    assert patched["action"] == "force_charge"
    assert patched["trigger_kind"] == "daily"
    assert patched["when_spec"].get("date") in (None, "")
    assert patched["when_spec"]["windows"] == []
    assert patched["next_fire"] is not None  # daily trigger previews a next fire


async def test_switch_to_window_clears_trigger_kind(client):
    """The reverse: window/once entries send trigger_kind: null to drop a stale
    trigger so the entry doesn't keep firing on the old daily/interval cadence."""
    created = (await client.post("/api/schedules", json={
        "name": "was daily", "action": "force_charge",
        "trigger_kind": "daily", "trigger_spec": {"time_of_day": "06:00"},
    })).json()
    sid = created["id"]
    assert created["trigger_kind"] == "daily"
    patched = (await client.patch(f"/api/schedules/{sid}", json={
        "trigger_kind": None, "trigger_spec": {},
        "when_spec": {"days": [1], "windows": [{"start": "14:00", "end": "19:00"}]},
    })).json()
    assert patched["trigger_kind"] is None
    assert patched["when_spec"]["windows"][0]["start"] == "14:00"
