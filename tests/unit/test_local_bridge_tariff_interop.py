"""Tariff profiles interoperate with the FranklinWH Local Bridge.

The fixture is a real export from the Local Bridge (Settings → Sites & Billing
→ Export, 2026-10-08): AGL on Ausgrid, two-season TOU with demand, export
bonus, export charge and a daily supply charge. Both bridges use the
``franklinwh-bridge/tariff-profile`` v1 format; this pins that a Local Bridge
export imports here with nothing dropped and exports back unchanged
(bridge#17).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "local_bridge_tariff_agl_ausgrid_tou.json"


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


async def test_local_bridge_export_round_trips(client):
    bundle = json.loads(FIXTURE.read_text())
    assert bundle["type"] == "franklinwh-bridge/tariff-profile" and bundle["version"] == 1

    dry = (await client.post("/api/services/import", json=bundle)).json()
    assert dry["ok"] and dry["rate_problems"] == [] and dry["ignored_fields"] == []
    assert dry["seasons"] == 2

    resp = await client.post("/api/services/import?dry_run=false", json=bundle)
    assert resp.status_code == 200, resp.text
    exported = (await client.get(f"/api/services/{resp.json()['applied_to']}/export")).json()

    assert exported["type"] == bundle["type"] and exported["version"] == bundle["version"]
    assert exported["profile"] == bundle["profile"]
