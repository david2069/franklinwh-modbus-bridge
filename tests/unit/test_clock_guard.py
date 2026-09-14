"""Timezone identity guard — record at install, verify every start.

Regression cover for 2026-09-12: the container's timezone silently moved from
AEST to UTC and a daily 18:00 export stopped firing with no audit row at all.
"""

from __future__ import annotations

import pytest

from franklinwh_bridge.config import clock
from franklinwh_bridge.store.db import get_app_config, set_app_config


@pytest.fixture(autouse=True)
def _reset_module_state():
    """The expected-tz cache is module state; don't leak it between tests."""
    clock._expected = None
    yield
    clock._expected = None


@pytest.fixture
async def db(tmp_path):
    from franklinwh_bridge.store.db import init_db

    conn = await init_db(tmp_path / "clock.db")
    yield conn
    await conn.close()


def test_resolve_prefers_tz_env(monkeypatch):
    """glibc honours TZ over /etc/localtime, so the check must too."""
    monkeypatch.setenv("TZ", "Australia/Sydney")
    assert clock.resolve_tz_name() == "Australia/Sydney"


def test_resolve_ignores_blank_tz(monkeypatch):
    """An empty TZ is not a timezone — fall through, don't report ''."""
    monkeypatch.setenv("TZ", "   ")
    assert clock.resolve_tz_name().strip() != ""


@pytest.mark.asyncio
async def test_first_run_seeds_the_timezone(db, monkeypatch):
    monkeypatch.setenv("TZ", "Australia/Sydney")

    result = await clock.check_and_record(db)

    assert result["status"] == "seeded"
    assert result["actual"] == "Australia/Sydney"
    assert await get_app_config(db, clock.TZ_CONFIG_KEY) == "Australia/Sydney"


@pytest.mark.asyncio
async def test_matching_timezone_is_ok(db, monkeypatch):
    monkeypatch.setenv("TZ", "Australia/Sydney")
    await clock.check_and_record(db)

    result = await clock.check_and_record(db)

    assert result["status"] == "ok"
    assert clock.tz_ok() is True


@pytest.mark.asyncio
async def test_drift_to_utc_is_reported(db, monkeypatch):
    """The exact 2026-09-12 failure: install was AEST, container came up UTC."""
    monkeypatch.setenv("TZ", "Australia/Sydney")
    await clock.check_and_record(db)

    monkeypatch.setenv("TZ", "UTC")
    result = await clock.check_and_record(db)

    assert result["status"] == "mismatch"
    assert result["expected"] == "Australia/Sydney"
    assert result["actual"] == "UTC"
    assert clock.tz_ok() is False


@pytest.mark.asyncio
async def test_mismatch_does_not_overwrite_the_recorded_value(db, monkeypatch):
    """Self-healing would turn the alarm into a rubber stamp: the next start
    would compare UTC against UTC and report ok while schedules stay mistimed."""
    monkeypatch.setenv("TZ", "Australia/Sydney")
    await clock.check_and_record(db)

    monkeypatch.setenv("TZ", "UTC")
    await clock.check_and_record(db)

    assert await get_app_config(db, clock.TZ_CONFIG_KEY) == "Australia/Sydney"
    assert (await clock.check_and_record(db))["status"] == "mismatch"


@pytest.mark.asyncio
async def test_recording_a_real_move_clears_the_alarm(db, monkeypatch):
    """A genuine relocation is a deliberate act, and then stays quiet."""
    monkeypatch.setenv("TZ", "Australia/Sydney")
    await clock.check_and_record(db)

    monkeypatch.setenv("TZ", "Pacific/Auckland")
    assert (await clock.check_and_record(db))["status"] == "mismatch"

    await clock.record_timezone(db)

    assert (await clock.check_and_record(db))["status"] == "ok"
    assert await get_app_config(db, clock.TZ_CONFIG_KEY) == "Pacific/Auckland"


@pytest.mark.asyncio
async def test_tz_ok_is_none_before_any_check(db, monkeypatch):
    """Unknown must not read as False — that would alarm on a fresh process."""
    monkeypatch.setenv("TZ", "Australia/Sydney")
    await set_app_config(db, clock.TZ_CONFIG_KEY, "Australia/Sydney")

    assert clock.tz_ok() is None


@pytest.mark.asyncio
async def test_check_survives_a_broken_config_read(monkeypatch):
    """The guard must never be the reason the bridge fails to start."""
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db gone")

    monkeypatch.setenv("TZ", "Australia/Sydney")
    result = await clock.check_and_record(Boom())

    assert result["status"] == "seeded"
    assert result["actual"] == "Australia/Sydney"


# ── First-run confirmation ────────────────────────────────────
# Auto-seeding records whatever the container resolved to. A container with no
# TZ resolves to UTC, so an install that was wrong from day one would be
# verified as correct forever. Only a person can settle that.


@pytest.mark.asyncio
async def test_first_run_needs_confirmation(db, monkeypatch):
    monkeypatch.setenv("TZ", "Australia/Sydney")
    await clock.check_and_record(db)

    st = await clock.status(db)

    assert st["needs_confirmation"] is True
    assert st["confirmed"] is False
    assert st["timezone"] == "Australia/Sydney"


@pytest.mark.asyncio
async def test_confirming_clears_the_prompt(db, monkeypatch):
    monkeypatch.setenv("TZ", "Australia/Sydney")
    await clock.check_and_record(db)

    st = await clock.confirm_timezone(db)

    assert st["confirmed"] is True
    assert st["needs_confirmation"] is False


@pytest.mark.asyncio
async def test_confirming_a_correction_also_records_it(db, monkeypatch):
    """Correcting at the prompt must not then be reported as drift next boot."""
    monkeypatch.setenv("TZ", "UTC")
    await clock.check_and_record(db)

    await clock.confirm_timezone(db, "Pacific/Auckland")

    assert await get_app_config(db, clock.TZ_CONFIG_KEY) == "Pacific/Auckland"


@pytest.mark.asyncio
async def test_utc_is_flagged_as_a_likely_default(db, monkeypatch):
    """The case that motivated this: a container with no TZ reports UTC."""
    monkeypatch.setenv("TZ", "UTC")
    await clock.check_and_record(db)

    assert (await clock.status(db))["looks_like_default"] is True


@pytest.mark.asyncio
async def test_a_real_timezone_is_not_flagged(db, monkeypatch):
    monkeypatch.setenv("TZ", "Australia/Sydney")
    await clock.check_and_record(db)

    assert (await clock.status(db))["looks_like_default"] is False
