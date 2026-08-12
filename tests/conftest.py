"""Shared test fixtures.

Auth bypass: the existing suite hits routes unauthenticated. Once auth gating is
on, that would 401 every API test. This autouse fixture overrides the real app's
``require_auth`` / ``get_current_user`` dependencies with a synthetic admin so the
suite keeps passing. The auth logic itself is tested directly (with the overrides
NOT applied) in ``test_auth_api.py``.
"""

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
