"""Service country/timezone (migration 38) + the plan-vs-clock cross-check."""

from __future__ import annotations

import pytest

from franklinwh_bridge.api.gateways_api import ServiceUpdate
from franklinwh_bridge.gateway.scheduler_sensors import _tz_matches_clock
from franklinwh_bridge.store.db import create_service, init_db, update_service


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "svc.db")
    yield conn
    await conn.close()


# ── Persistence ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_country_and_timezone_round_trip(db):
    svc = await create_service(db, name="AGL")

    row = await update_service(db, svc["id"], country="AU", timezone="Australia/Sydney")

    assert row["country"] == "AU"
    assert row["timezone"] == "Australia/Sydney"


@pytest.mark.asyncio
async def test_existing_services_default_to_unstated(db):
    """Migration 38 must not invent a location for services created before it."""
    svc = await create_service(db, name="Legacy")

    assert svc.get("country", "") == ""
    assert svc.get("timezone", "") == ""


# ── Validation ────────────────────────────────────────────────


def test_country_is_upper_cased():
    assert ServiceUpdate(country="au").country == "AU"


def test_country_rejects_a_non_iso_value():
    with pytest.raises(ValueError):
        ServiceUpdate(country="Aus")


def test_timezone_accepts_an_iana_name():
    assert ServiceUpdate(timezone="Pacific/Auckland").timezone == "Pacific/Auckland"


def test_timezone_rejects_a_plausible_looking_typo():
    """'Australia/Sydne' would otherwise sit on file reading as confirmation."""
    with pytest.raises(ValueError):
        ServiceUpdate(timezone="Australia/Sydne")


def test_timezone_rejects_an_abbreviation():
    """AEST is not an IANA zone — it also can't express DST."""
    with pytest.raises(ValueError):
        ServiceUpdate(timezone="AEST")


def test_blank_timezone_clears_rather_than_failing():
    assert ServiceUpdate(timezone="").timezone == ""


# ── Cross-check against the clock the engine runs on ──────────


def test_unstated_timezone_is_unknown_not_a_failure(monkeypatch):
    """None, never False — an unconfigured service must not raise an alarm."""
    assert _tz_matches_clock({}) is None
    assert _tz_matches_clock({"service_timezone": "  "}) is None


def test_unresolvable_timezone_is_unknown():
    assert _tz_matches_clock({"service_timezone": "Mars/Olympus"}) is None


def test_matching_timezone_passes(monkeypatch):
    import time as time_mod

    monkeypatch.setattr(
        time_mod, "localtime",
        lambda *a: type("T", (), {"tm_gmtoff": 10 * 3600})(),
    )
    assert _tz_matches_clock({"service_timezone": "Australia/Brisbane"}) is True


def test_clock_on_utc_while_plan_is_sydney_fails(monkeypatch):
    """The 2026-09-12 failure, seen from the plan's side: TOU windows written
    for AEST evaluated against a UTC clock land 10 hours out."""
    import time as time_mod

    monkeypatch.setattr(
        time_mod, "localtime",
        lambda *a: type("T", (), {"tm_gmtoff": 0})(),
    )
    assert _tz_matches_clock({"service_timezone": "Australia/Brisbane"}) is False


def test_equivalent_zones_are_not_a_mismatch(monkeypatch):
    """Compare offsets, not names: UTC and Etc/UTC are the same clock."""
    import time as time_mod

    monkeypatch.setattr(
        time_mod, "localtime",
        lambda *a: type("T", (), {"tm_gmtoff": 0})(),
    )
    assert _tz_matches_clock({"service_timezone": "Etc/UTC"}) is True
