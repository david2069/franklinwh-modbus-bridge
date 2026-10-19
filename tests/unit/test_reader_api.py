"""SunSpec Explorer reader (#288): the franklinwh-modbus reader, run in-process.

The vendored reader is stubbed: these tests check what the bridge asks of it
(base address 0, the CLI's option rules, one Modbus client) and what it refuses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.api import reader_api
from franklinwh_bridge.api.auth import require_auth, require_role
from franklinwh_bridge.vendor import modbus_sunspec2_reader as rdr


class _Controller:
    def __init__(self):
        self.disconnects = 0
        self.connects = 0

    def disconnect(self):
        self.disconnects += 1

    def connect(self):
        self.connects += 1
        return True


def _inst(mock=False, active=False):
    return SimpleNamespace(
        config=SimpleNamespace(mock=mock, host="192.0.2.50", port=502, unit_id=1, timeout=5),
        command_handler=SimpleNamespace(state=SimpleNamespace(active=active)),
        controller=_Controller(),
        poller=SimpleNamespace(closed=[], _close_vreg_client=lambda: None),
        modbus_lock=asyncio.Lock(),
    )


@pytest.fixture
def calls(monkeypatch):
    seen = {}

    def read_sunspec_device(ip, **kw):
        seen["read"] = {"ip": ip, **kw}
        return {"models": {}}, None

    def print_device_info(info, detail, **kw):
        seen["print"] = {"detail": detail, **kw}
        print("15508 SelfReserve 8")

    monkeypatch.setattr(rdr, "read_sunspec_device", read_sunspec_device)
    monkeypatch.setattr(rdr, "print_device_info", print_device_info)
    return seen


def _app(inst, role="admin"):
    app = FastAPI()
    app.state.registry = SimpleNamespace(get=lambda gid: inst if gid == "default" else None)
    app.include_router(reader_api.router)
    user = {"id": "u", "username": "u", "role": role, "enabled": True}
    app.dependency_overrides[require_auth] = lambda: user
    return app


async def _post(app, body):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/api/reader/run", json=body)


async def test_raw_read_uses_base_0_skips_the_model_scan_and_hands_over_the_client(calls):
    inst = _inst()
    r = await _post(_app(inst), {"raw": "15000:40", "nz": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "15508 SelfReserve 8" in body["output"] and body["error"] is None
    assert calls["read"]["base_address"] == 0
    assert calls["read"]["raw_read_spec"] == "15000:40"
    assert calls["read"]["scan_models"] is False
    assert calls["print"]["show_nonzero"] is True
    assert inst.controller.disconnects == 1   # one client: the bridge let go first
    assert inst.controller.connects == 1      # ...and took it back before unlocking
    assert not inst.modbus_lock.locked()


async def test_match_scans_models_and_raises_detail_like_the_cli(calls):
    r = await _post(_app(_inst()), {"raw": "15000:40", "match": True})
    assert r.status_code == 200, r.text
    assert calls["read"]["scan_models"] is True
    assert calls["read"]["detail_level"] == "values"
    assert calls["print"]["do_match"] is True


async def test_models_are_parsed_with_the_readers_own_parser(calls):
    await _post(_app(_inst()), {"models": "1,701-703"})
    assert calls["read"]["models_to_scan"] == {1, 701, 702, 703}


@pytest.mark.parametrize("body, status", [
    ({"raw": "15000:126"}, 400),
    ({"raw": "65530:10"}, 400),
    ({"raw": "15000;40"}, 422),
    ({"models": "1; rm -rf /"}, 422),
    ({"point": "714.DCW extra"}, 422),
])
async def test_bad_requests_are_refused(calls, body, status):
    assert (await _post(_app(_inst()), body)).status_code == status
    assert "read" not in calls


async def test_refused_for_a_demo_gateway_an_active_command_or_an_unknown_gateway(calls):
    assert (await _post(_app(_inst(mock=True)), {"raw": "15500:14"})).status_code == 409
    assert (await _post(_app(_inst(active=True)), {"raw": "15500:14"})).status_code == 409
    assert (await _post(_app(_inst()), {"gateway_id": "x", "raw": "15500:14"})).status_code == 404
    assert "read" not in calls


async def test_admin_only():
    app = _app(_inst(), role="viewer")
    assert (await _post(app, {"raw": "15500:14"})).status_code == 403


def test_the_route_requires_the_admin_role():
    route = next(r for r in reader_api.router.routes if r.path == "/api/reader/run")
    calls = [d.call for d in route.dependant.dependencies]
    assert any(getattr(c, "__qualname__", "").startswith("require_role") for c in calls)
    assert require_role  # imported for the qualname above


def test_the_vendored_reader_has_no_write_path():
    """It must stay read-only: no Modbus write function codes or write calls."""
    import inspect

    src = inspect.getsource(rdr)
    for needle in ("write_hregs", ".write(", "0x06", "0x10"):
        assert needle not in src, needle
