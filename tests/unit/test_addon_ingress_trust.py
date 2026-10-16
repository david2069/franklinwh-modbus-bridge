"""Add-on mode trusts only the Supervisor's ingress proxy (GHSA-hhm2-xvvc-f6ww).

Before the fix every request in add-on mode was treated as the ingress admin,
so any other add-on on the Supervisor network (and anyone, once port 8099 was
published) had the full admin API with no credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.api.auth import SUPERVISOR_INGRESS_IP
from franklinwh_bridge.store.db import init_db
from tests.unit.test_auth_api import _build_app

INGRESS = {"X-Ingress-Path": "/api/hassio_ingress/abc123"}


@pytest.fixture
async def addon_app(tmp_path):
    db = await init_db(tmp_path / "addon.db")
    try:
        yield _build_app(db, environment="ha_addon")
    finally:
        await db.close()


async def _get(app, path, peer, headers=None):
    transport = ASGITransport(app=app, client=(peer, 40000), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        return await ac.get(path, headers=headers or {})


async def test_ingress_proxy_with_ingress_headers_is_admin(addon_app):
    r = await _get(addon_app, "/admin-only", SUPERVISOR_INGRESS_IP, INGRESS)
    assert r.status_code == 200


@pytest.mark.parametrize(
    "peer, headers",
    [
        ("172.30.33.5", {}),  # another add-on on the Supervisor network
        ("172.30.33.5", INGRESS),  # ... forging the ingress header
        ("192.0.2.10", {}),  # LAN client on a published port 8099
        ("192.0.2.10", {**INGRESS, "X-Forwarded-For": SUPERVISOR_INGRESS_IP}),
        ("127.0.0.1", {}),  # inside the container (healthcheck)
        (SUPERVISOR_INGRESS_IP, {}),  # proxy address without ingress headers
    ],
)
async def test_anything_else_must_log_in(addon_app, peer, headers):
    assert (await _get(addon_app, "/protected", peer, headers)).status_code == 401
    assert (await _get(addon_app, "/admin-only", peer, headers)).status_code == 401


def test_addon_does_not_trust_forwarded_headers_from_any_peer():
    """``--forwarded-allow-ips '*'`` lets any client rewrite its own address."""
    run_sh = (Path(__file__).parents[2] / "addon" / "run.sh").read_text()
    assert "--forwarded-allow-ips '*'" not in run_sh
    assert '--forwarded-allow-ips "*"' not in run_sh
    assert "--no-proxy-headers" in run_sh
