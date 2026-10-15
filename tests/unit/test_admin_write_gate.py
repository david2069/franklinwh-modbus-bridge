"""Writes need the admin role unless a route declares its own gate (#102).

Most feature routers asked only for a session, so a signed-in viewer could
reconfigure gateways and MQTT, toggle modules, stop gateways and run schedules.
"""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
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


def _dep_names(dependant) -> set[str]:
    names, stack = set(), list(dependant.dependencies)
    while stack:
        dep = stack.pop()
        names.add(getattr(dep.call, "__qualname__", ""))
        stack.extend(dep.dependencies)
    return names


def test_every_write_route_is_gated():
    """A new write route must say who may call it — or be listed as open here."""
    ungated = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"GET", "HEAD", "OPTIONS"}:
            if (method, route.path) in OPEN_WRITES:
                continue
            names = _dep_names(route.dependant)
            if "require_admin_for_writes" in names or any(
                n.startswith(("require_role.", "require_capability.")) for n in names
            ):
                continue
            ungated.append(f"{method} {route.path}")
    assert ungated == []


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


async def test_admin_still_writes(client):
    await _login_as(client, "admin")
    resp = await client.patch("/api/gateways/default", json={"description": "ok"})
    assert resp.status_code == 200, resp.text
