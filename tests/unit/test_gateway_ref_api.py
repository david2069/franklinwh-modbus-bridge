"""/api/gateways/{ref}/… takes a gateway's ID or serial; anything else is
rejected with the reason and logged (#296 item 42).

Before, a serial was "not found", and an unknown ID got the same "not running"
as a stopped gateway, with nothing in the bridge's log.
"""

from __future__ import annotations

import logging

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app
from franklinwh_bridge.store.db import update_gateway

SERIAL = "AG0000000001"


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


async def test_gateway_id_or_serial_in_the_path(client, caplog):
    await update_gateway(app.state.db, "default", serial=SERIAL)

    # The ID and the serial reach the same gateway.
    by_id = await client.get("/api/gateways/default")
    by_serial = await client.get(f"/api/gateways/{SERIAL}")
    assert by_id.status_code == by_serial.status_code == 200
    assert by_serial.json()["id"] == "default"
    sub_id = await client.get("/api/gateways/default/battery-rating")
    sub_serial = await client.get(f"/api/gateways/{SERIAL}/battery-rating")
    assert sub_serial.status_code == sub_id.status_code
    assert sub_serial.json() == sub_id.json()

    with caplog.at_level(logging.WARNING, logger="franklinwh_bridge.api.gateway_ref"):
        # Unknown: 404 says it is neither an ID nor a serial, and is logged.
        r = await client.get("/api/gateways/nope/points")
        assert r.status_code == 404
        assert r.json()["detail"] == "No gateway has the ID or serial 'nope'"
        assert "API GET /api/gateways/nope/points rejected (404)" in caplog.text

        # Malformed: 422 with what is wrong.
        r = await client.post("/api/gateways/" + "x" * 64 + "/stop")
        assert r.status_code == 422
        assert r.json()["detail"] == (
            "Malformed gateway reference: it is 64 characters long; "
            "an ID or serial has at most 63")
        r = await client.get("/api/gateways/a%01b")
        assert r.status_code == 422
        assert "control character" in r.json()["detail"]
        assert caplog.text.count("rejected (422)") == 2

    # The list route has no reference and is untouched.
    assert (await client.get("/api/gateways")).status_code == 200
