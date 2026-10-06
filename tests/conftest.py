"""Shared test fixtures.

Auth bypass: the existing suite hits routes unauthenticated. Once auth gating is
on, that would 401 every API test. This autouse fixture overrides the real app's
``require_auth`` / ``get_current_user`` dependencies with a synthetic admin so the
suite keeps passing. The auth logic itself is tested directly (with the overrides
NOT applied) in ``test_auth_api.py``.
"""

import asyncio
import time

import pytest

_ADMIN = {"id": "test-admin", "username": "test", "role": "admin", "enabled": True}


@pytest.fixture(autouse=True)
def _auth_bypass():
    try:
        from franklinwh_bridge.api.auth import get_current_user, require_auth
        from franklinwh_bridge.main import app
    except Exception:
        yield
        return
    app.dependency_overrides[require_auth] = lambda: _ADMIN
    app.dependency_overrides[get_current_user] = lambda: _ADMIN
    yield
    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _configured_default_gateway(monkeypatch):
    """Give the default gateway an address, as a configured install has.

    With no MODBUS_HOST the default gateway is created unconfigured and never
    started (no phantom 192.168.1.100). Most of the suite exercises a running
    default gateway, so configure one — at a TEST-NET-1 address that can never
    be a real device. Tests of the unconfigured path delete this variable.
    """
    monkeypatch.setenv("MODBUS_HOST", "192.0.2.10")


@pytest.fixture
def default_gateway_bus():
    """Wait for the default gateway and return its own sample bus.

    ``/api/points`` reads the default instance's per-gateway bus, never the
    global one, and gateways start in a background task (``main.py``
    ``_start_gateways``) — so the registry is still empty when the lifespan
    context returns. A test that publishes before that lands writes to a bus
    nothing reads.
    """

    async def _bus(app, timeout: float = 10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            inst = app.state.registry.get("default")
            if inst is not None:
                return inst.sample_bus
            await asyncio.sleep(0.05)
        raise AssertionError("default gateway never registered")

    return _bus
