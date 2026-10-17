"""In the add-on, the bridge's logged-out modal never shows (#247).

Home Assistant signs the user in and the bridge has no login of its own under
ingress. A 401 there came from HA's lapsed session for the panel, and the
modal's "Log in" led to a bridge login page ingress doesn't serve: the owner
saw the modal, then "page not found". Pages served through ingress are marked,
and the page script takes HA's path on a 401 instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.api import auth
from franklinwh_bridge.api.auth import SUPERVISOR_INGRESS_IP, get_current_user
from franklinwh_bridge.api.ui import router as ui_router
from franklinwh_bridge.store.db import init_db
from tests.unit.test_auth_api import _build_app

INGRESS = {"X-Ingress-Path": "/api/hassio_ingress/abc123"}
JS = Path(__file__).parents[2] / "src" / "franklinwh_bridge" / "static" / "js"
MARK = 'data-ha-ingress="1"'


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "ui.db")
    yield conn
    await conn.close()


async def _get(app, path, peer="192.0.2.10", headers=None):
    transport = ASGITransport(app=app, client=(peer, 40000), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get(path, headers=headers or {}, follow_redirects=False)


@pytest.mark.parametrize("path", ["/", "/user"])
async def test_pages_served_through_ingress_are_marked(db, path):
    app = _build_app(db, environment="ha_addon")
    app.include_router(ui_router)
    r = await _get(app, path, SUPERVISOR_INGRESS_IP, INGRESS)
    assert r.status_code == 200, r.text[:200]
    assert MARK in r.text


@pytest.mark.parametrize("path", ["/", "/user"])
async def test_pages_opened_directly_are_not_marked(db, path):
    """A direct login (Docker, or the add-on's own port) keeps the modal."""
    app = _build_app(db, environment="docker")
    app.include_router(ui_router)

    async def _admin():
        return {"id": "u1", "username": "alice", "role": "admin", "enabled": True}

    app.dependency_overrides[get_current_user] = _admin
    r = await _get(app, path)
    assert r.status_code == 200, r.text[:200]
    assert MARK not in r.text


def test_page_script_never_shows_the_modal_or_login_under_ingress():
    app_js = (JS / "app.js").read_text()
    assert "if (HA_INGRESS) _noteIngress401();\n        else _noteLoggedOut(" in app_js
    go = app_js.index("goToLogin() {")
    assert "if (HA_INGRESS) { _reloadHaPage(); return; }" in app_js[go:go + 200]
    user_js = (JS / "user_app.js").read_text()
    assert "if (status !== 401 || leaving || HA_INGRESS) return;" in user_js
    assert "if (!HA_INGRESS) window.location.href = `${base}/login`;" in user_js


async def test_a_refused_ingress_request_is_logged_once_a_minute(db, caplog, monkeypatch):
    """Tells the two 401 sources apart: HA's session (the bridge never sees
    the request) or the bridge refusing one that claims ingress."""
    monkeypatch.setattr(auth, "_last_refused_ingress_log", 0.0)
    app = _build_app(db, environment="ha_addon")
    with caplog.at_level(logging.WARNING, logger=auth.__name__):
        for _ in range(3):
            assert (await _get(app, "/protected", "172.30.33.5", INGRESS)).status_code == 401
    refused = [r for r in caplog.records if "ingress header" in r.getMessage()]
    assert len(refused) == 1
    assert "172.30.33.5" in refused[0].getMessage()


async def test_no_log_without_an_ingress_header(db, caplog, monkeypatch):
    monkeypatch.setattr(auth, "_last_refused_ingress_log", 0.0)
    app = _build_app(db, environment="ha_addon")
    with caplog.at_level(logging.WARNING, logger=auth.__name__):
        assert (await _get(app, "/protected")).status_code == 401
    assert not [r for r in caplog.records if "ingress header" in r.getMessage()]
