"""Who may command the battery.

ROLE_CAPABILITIES has always reserved ``control`` to roles allowed to operate
the hardware, but until the mobile dispatch card shipped nothing enforced it:
the control routes asked only for a session, so any signed-in viewer could POST
a forced charge. These tests pin the grant and the enforcement together.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.gateway.modules import ROLE_CAPABILITIES
from franklinwh_bridge.main import app

# ── The grant ─────────────────────────────────────────────────


def test_viewer_cannot_control():
    assert "control" not in ROLE_CAPABILITIES["viewer"]


def test_user_can_control():
    """The mobile dashboard's charge/discharge card is for this role."""
    assert "control" in ROLE_CAPABILITIES["user"]


def test_admin_can_control():
    assert "control" in ROLE_CAPABILITIES["admin"]


def test_no_role_gains_control_by_accident():
    """A new role added without thinking about hardware defaults to safe."""
    allowed = {r for r, caps in ROLE_CAPABILITIES.items() if "control" in caps}

    assert allowed == {"admin", "user"}


# ── The enforcement ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _real_auth():
    """Opt out of conftest's autouse auth bypass.

    That bypass overrides require_auth with a synthetic *admin*, which this
    module's whole subject — refusing a viewer — would silently pass under. It
    runs first (conftest scope), so popping here leaves the real dependency in
    place for these tests only.
    """
    from franklinwh_bridge.api.auth import get_current_user, require_auth

    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(get_current_user, None)
    yield


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    # The test client speaks plain HTTP, and login refuses that by default.
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
    """Create a user with ``role`` and start a session as them."""
    from franklinwh_bridge.security import hash_password
    from franklinwh_bridge.store.db import create_user

    await create_user(app.state.db, username=f"t_{role}",
                      password_hash=hash_password("pw12345678"), role=role)
    r = await client.post("/api/auth/login",
                          json={"username": f"t_{role}", "password": "pw12345678"})
    assert r.status_code == 200, r.text


async def test_a_viewer_is_refused_the_command_route(client):
    await _login_as(client, "viewer")

    resp = await client.post("/api/command",
                             json={"slug": "battery_command", "value": "Force Charge"})

    assert resp.status_code == 403
    assert "control" in resp.json()["detail"]


async def test_a_viewer_is_refused_the_per_gateway_route(client):
    """Both control surfaces, or the gate is decorative."""
    await _login_as(client, "viewer")

    resp = await client.post("/api/gateways/default/command",
                             json={"slug": "battery_command", "value": "Force Charge"})

    assert resp.status_code == 403


async def test_a_user_reaches_the_command_route(client):
    """403 would mean the mobile dispatch card cannot work. Anything else —
    including a handler-unavailable answer — means the gate let them past."""
    await _login_as(client, "user")

    resp = await client.post("/api/command",
                             json={"slug": "battery_command", "value": "Stop"})

    assert resp.status_code != 403


async def test_an_anonymous_caller_is_still_401_not_403(client):
    """Unauthenticated must stay distinguishable from unauthorised."""
    resp = await client.post("/api/command",
                             json={"slug": "battery_command", "value": "Stop"})

    assert resp.status_code == 401
