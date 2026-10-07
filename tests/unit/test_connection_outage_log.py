"""The browser reports a "can't reach the bridge" outage once it's back.

The bridge never sees requests that didn't arrive, so without this the outage
the user saw is nowhere in the bridge's log.
"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_outage_is_written_to_the_log(client, caplog):
    body = {
        "started_at": 1_800_000_000.0, "ended_at": 1_800_000_066.0, "failures": 3,
        "message": "no response within 8s", "url": "api/sequence/execute", "page": "sequencer",
    }
    with caplog.at_level(logging.WARNING, logger="franklinwh_bridge.api.ui"):
        resp = await client.post("/api/ui/connection-outage", json=body)
    assert resp.status_code == 204
    line = next(r.getMessage() for r in caplog.records if "lost contact" in r.getMessage())
    assert "66s" in line and "3 failed requests" in line
    assert "no response within 8s on api/sequence/execute" in line
    assert "(sequencer page)" in line


async def test_outage_report_is_validated(client):
    resp = await client.post("/api/ui/connection-outage", json={
        "started_at": 1.0, "ended_at": 2.0, "bogus": True})
    assert resp.status_code == 422
    resp = await client.post("/api/ui/connection-outage", json={
        "started_at": 1.0, "ended_at": 2.0, "message": "x" * 301})
    assert resp.status_code == 422


async def test_outage_report_needs_a_session(client):
    from franklinwh_bridge.api.auth import get_current_user, require_auth

    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(get_current_user, None)
    resp = await client.post("/api/ui/connection-outage", json={"started_at": 1.0, "ended_at": 2.0})
    assert resp.status_code == 401
