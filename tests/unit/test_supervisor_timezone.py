"""Supervisor timezone auto-config.

`addon/run.sh` asks bashio for the host timezone. FWHAI shipped that and found
the call returns "Unable to access the API, forbidden" on a real install, while
the identical token works from Python. The guard there — and ours —
(`if bashio::info.timezone > /dev/null 2>&1`) cannot distinguish "forbidden"
from "not configured", so it falls through and the container stays on UTC.

That is not cosmetic: schedule triggers, TOU blocks, demand and export windows
are all local wall-clock, so a Sydney site runs ten hours out. These tests pin
the Python path that replaces it, and the precedence rules that stop it from
overriding a deliberate choice.
"""

from __future__ import annotations

import os

import pytest

from franklinwh_bridge.config import supervisor


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Client:
    """Stands in for httpx.AsyncClient; records the URL it was asked for."""

    def __init__(self, resp, *, raises=None):
        self._resp = resp
        self._raises = raises
        self.url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        self.url = url
        if self._raises:
            raise self._raises
        return self._resp


@pytest.fixture
def no_tz(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    yield


def _patch_httpx(monkeypatch, client):
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)


# ── Discovery ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timezone_comes_from_the_supervisor(monkeypatch, no_tz):
    client = _Client(_Resp({"data": {"timezone": "Australia/Sydney"}}))
    _patch_httpx(monkeypatch, client)

    assert await supervisor.discover_timezone() == "Australia/Sydney"
    assert client.url.endswith("/info")


@pytest.mark.asyncio
async def test_no_supervisor_token_means_no_lookup(monkeypatch):
    """Outside an add-on there is no Supervisor. That is normal, not a failure."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("HASSIO_TOKEN", raising=False)

    assert await supervisor.discover_timezone() is None


@pytest.mark.asyncio
async def test_a_forbidden_response_returns_none_and_does_not_raise(monkeypatch, no_tz):
    """The exact failure FWHAI hit. It must not take startup down, and must not
    be mistaken for a valid answer."""
    _patch_httpx(monkeypatch, _Client(_Resp({}, status=403)))

    assert await supervisor.discover_timezone() is None


@pytest.mark.asyncio
async def test_a_network_error_returns_none(monkeypatch, no_tz):
    _patch_httpx(monkeypatch, _Client(None, raises=OSError("no route")))

    assert await supervisor.discover_timezone() is None


@pytest.mark.asyncio
async def test_a_null_timezone_is_not_adopted(monkeypatch, no_tz):
    """bashio renders an unset value as the string "null"; the API can send a
    real null. Neither is a timezone."""
    for payload in ({"data": {"timezone": "null"}}, {"data": {"timezone": None}}, {}):
        _patch_httpx(monkeypatch, _Client(_Resp(payload)))
        assert await supervisor.discover_timezone() is None, payload


# ── Precedence ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_it_applies_when_tz_is_unset(monkeypatch, no_tz):
    _patch_httpx(monkeypatch, _Client(_Resp({"data": {"timezone": "Australia/Sydney"}})))

    assert await supervisor.apply_timezone() == "Australia/Sydney"
    assert os.environ["TZ"] == "Australia/Sydney"


@pytest.mark.asyncio
async def test_it_overrides_a_bare_utc(monkeypatch):
    """UTC is what the container falls back to when run.sh's bashio call was
    refused — it is the symptom, not a choice, so it must not win."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setenv("TZ", "UTC")
    _patch_httpx(monkeypatch, _Client(_Resp({"data": {"timezone": "Australia/Sydney"}})))

    assert await supervisor.apply_timezone() == "Australia/Sydney"


@pytest.mark.asyncio
async def test_an_explicit_timezone_is_never_overridden(monkeypatch):
    """An operator who set TZ meant it. Discovery is a default, not an override."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    _patch_httpx(monkeypatch, _Client(_Resp({"data": {"timezone": "Australia/Sydney"}})))

    assert await supervisor.apply_timezone() is None
    assert os.environ["TZ"] == "Pacific/Auckland"


@pytest.mark.asyncio
async def test_applying_the_same_zone_is_a_no_op(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setenv("TZ", "UTC")
    _patch_httpx(monkeypatch, _Client(_Resp({"data": {"timezone": "UTC"}})))

    assert await supervisor.apply_timezone() is None


@pytest.mark.asyncio
async def test_a_failed_lookup_leaves_tz_alone(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "tok")
    monkeypatch.setenv("TZ", "UTC")
    _patch_httpx(monkeypatch, _Client(None, raises=OSError("forbidden")))

    assert await supervisor.apply_timezone() is None
    assert os.environ["TZ"] == "UTC"


@pytest.mark.asyncio
async def test_the_applied_zone_takes_effect_immediately(monkeypatch, no_tz):
    """tzset() matters: without it datetime.now() keeps the old zone for the
    life of the process, which is exactly the window startup runs in."""
    import time as _time

    _patch_httpx(monkeypatch, _Client(_Resp({"data": {"timezone": "Australia/Sydney"}})))
    await supervisor.apply_timezone()

    assert _time.localtime().tm_zone in ("AEST", "AEDT")
