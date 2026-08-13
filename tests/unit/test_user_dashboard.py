"""Phase 2 — simplified /user dashboard route + role-based landing redirects.

Builds a tiny app with just the UI router so the conftest auth bypass (which
targets main.app) doesn't apply, and overrides ``get_current_user`` per role.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from franklinwh_bridge.api.auth import get_current_user
from franklinwh_bridge.api.ui import _home_for
from franklinwh_bridge.api.ui import router as ui_router


def _client(user):
    app = FastAPI()
    app.include_router(ui_router)
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize(
    "role,expected",
    [("admin", "/"), ("user", "/user"), ("viewer", "/user")],
)
def test_home_for_role(role, expected):
    assert _home_for("", {"role": role}) == expected
    assert _home_for("/ingress", {"role": role}) == f"/ingress{expected}"


def test_index_redirects_by_role():
    # Unauthenticated → /login
    r = _client(None).get("/")
    assert r.status_code == 302 and r.headers["location"] == "/login"
    # Non-admin → /user
    r = _client({"role": "viewer"}).get("/")
    assert r.status_code == 302 and r.headers["location"] == "/user"
    # Admin → full SPA
    r = _client({"role": "admin"}).get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]


def test_user_route_serves_board_for_any_role():
    for role in ("user", "viewer", "admin"):
        r = _client({"role": role}).get("/user")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "user_app.js" in r.text
    # Unauthenticated → /login
    r = _client(None).get("/user")
    assert r.status_code == 302 and r.headers["location"] == "/login"


def test_login_page_redirects_authed_by_role():
    assert _client({"role": "viewer"}).get("/login").headers["location"] == "/user"
    assert _client({"role": "admin"}).get("/login").headers["location"] == "/"
