"""Writes need the admin role unless a route declares its own gate (#102).

Most feature routers asked only for a session, so a signed-in viewer could
reconfigure gateways and MQTT, toggle modules, stop gateways and run schedules.
"""

from __future__ import annotations

import re

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.main import app

#: Write routes that any caller (login) or any signed-in user may use.
OPEN_WRITES = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("POST", "/api/auth/password"),
    ("POST", "/api/disclaimer"),
    ("POST", "/api/ui/connection-outage"),
}


def _write_routes() -> list[tuple[str, str]]:
    """Every write route, from the OpenAPI schema (public API).

    Not ``app.routes``: since FastAPI 0.137 it holds included routers, not
    their routes, so a loop over it saw no write route at all and this check
    passed while proving nothing (#248). No route is hidden from the schema
    (checked when this was written), so the schema is the whole list.
    """
    paths = app.openapi()["paths"]
    return [(m.upper(), path) for path, ops in paths.items() for m in ops
            if m.upper() not in {"GET", "HEAD", "OPTIONS"}]


def test_the_write_route_list_is_real():
    """Guards the check below against passing on an empty list again."""
    routes = _write_routes()
    assert len(routes) > 50
    assert ("PATCH", "/api/gateways/{gw_id}") in routes


@pytest.fixture(autouse=True)
def _real_auth():
    """Opt out of conftest's admin bypass: this module is about non-admins."""
    from franklinwh_bridge.api.auth import get_current_user, require_auth

    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(get_current_user, None)
    yield


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("ALLOW_INSECURE_AUTH", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def _login_as(client, role: str) -> None:
    from franklinwh_bridge.security import hash_password
    from franklinwh_bridge.store.db import create_user

    await create_user(app.state.db, username=f"g_{role}",
                      password_hash=hash_password("pw12345678"), role=role)
    r = await client.post("/api/auth/login",
                          json={"username": f"g_{role}", "password": "pw12345678"})
    assert r.status_code == 200, r.text


#: The writes the #102 probe showed a viewer could make, plus schedule execution.
_ADMIN_WRITES = [
    ("patch", "/api/gateways/default", {"description": "x"}),
    ("patch", "/api/mqtt/config", {"host": "elsewhere.example"}),
    ("post", "/api/mqtt/namespace", {"topic_prefix": "fwh_zz", "confirm": True}),
    ("patch", "/api/modules/energy_costs", {"enabled": True}),
    ("post", "/api/gateways/default/stop", None),
    ("post", "/api/schedules/nope/execute", None),
    ("put", "/api/automation/constants", {}),
    ("put", "/api/tariff/spot/settings", {"nem_region": "NSW1"}),
]


@pytest.mark.parametrize("role", ["viewer", "user", "api"])
async def test_non_admins_are_refused_admin_writes(client, role):
    await _login_as(client, role)
    for method, path, body in _ADMIN_WRITES:
        kwargs = {"json": body} if body is not None else {}
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 403, (role, path, resp.status_code)
    cfg = (await client.get("/api/mqtt/config")).json()
    assert cfg["host"] != "elsewhere.example"


@pytest.mark.parametrize("role", ["viewer", "user", "api"])
async def test_non_admins_can_still_read(client, role):
    await _login_as(client, role)
    for path in ("/api/gateways", "/api/mqtt/config", "/api/modules", "/api/tariff/spot"):
        assert (await client.get(path)).status_code == 200, (role, path)


@pytest.mark.parametrize("role", ["user", "api"])
async def test_control_roles_keep_the_gateway_command(client, role):
    """The per-gateway command has its own `control` gate; the admin gate must not shadow it."""
    await _login_as(client, role)
    resp = await client.post("/api/gateways/default/command",
                             json={"slug": "__probe__", "value": "0"})
    assert resp.status_code != 403, resp.text


async def test_every_write_route_refuses_a_viewer(client):
    """A new write route must say who may call it, or be listed as open here.

    Asked with requests rather than by reading each route's dependencies, so
    it holds however FastAPI stores them. Every module is switched on first,
    so a module gate can't answer for a missing role gate."""
    from franklinwh_bridge.gateway.modules import MODULES, set_module_enabled

    for m in MODULES:
        if not m.get("core"):
            await set_module_enabled(app.state.db, m["id"], True)
    await _login_as(client, "viewer")
    allowed = []
    for method, path in _write_routes():
        if (method, path) in OPEN_WRITES:
            continue
        resp = await client.request(method, re.sub(r"\{[^}]+\}", "x", path), json={})
        if resp.status_code not in (401, 403):
            allowed.append(f"{method} {path} -> {resp.status_code}")
    assert allowed == []


async def test_admin_still_writes(client):
    await _login_as(client, "admin")
    resp = await client.patch("/api/gateways/default", json={"description": "ok"})
    assert resp.status_code == 200, resp.text
