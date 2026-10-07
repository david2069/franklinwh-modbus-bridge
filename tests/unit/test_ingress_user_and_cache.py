"""Ingress shows the Home Assistant user's name; HTML pages aren't browser-cached."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from franklinwh_bridge.api.auth import get_current_user
from franklinwh_bridge.main import app


def _ingress_request(headers: dict) -> Request:
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "app": SimpleNamespace(state=SimpleNamespace(config=SimpleNamespace(environment="ha_addon"))),
    }
    return Request(scope)


async def test_ingress_user_is_named_after_the_ha_user():
    user = await get_current_user(_ingress_request({"X-Remote-User-Display-Name": "David"}))
    assert user["username"] == "David"
    assert user["id"] == "ingress" and user["role"] == "admin"   # id unchanged
    user = await get_current_user(_ingress_request({"X-Remote-User-Name": "david"}))
    assert user["username"] == "david"
    user = await get_current_user(_ingress_request({}))
    assert user["username"] == "Home Assistant"                  # never "(ingress)"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_html_pages_are_not_cached(client):
    served = 0
    for path in ("/", "/user", "/login"):
        resp = await client.get(path, follow_redirects=False)
        if resp.status_code == 200:
            served += 1
            assert resp.headers.get("cache-control") == "no-cache", path
    assert served >= 2  # the signed-in shell pages, at least
