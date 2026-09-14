"""Timezone identity — record it at install, verify it every start.

Every schedule trigger, TOU window and ``time.*`` sensor is evaluated against
the container's LOCAL clock. Nothing in the app chooses that clock: it comes
from the environment (``TZ``, ``/etc/localtime``). When the environment changes
underneath a running install, every automation silently moves.

That is not hypothetical. On 2026-09-12 the ``/etc/localtime`` bind mount
reverted inside a long-running container, the bridge dropped from AEST to UTC,
and a daily 18:00 export simply never became active — no dispatch, and no audit
row either, because the entry was never evaluated. The only visible symptom was
a missing line in a log.

So: remember the timezone the install was set up with, and compare on every
startup. A mismatch is reported loudly rather than inferred later from a battery
that didn't discharge.

We store the IANA **name** (``Australia/Sydney``), never the UTC offset — the
offset legitimately changes at every DST transition, so an offset check would
false-alarm twice a year and be trained away.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# app_config key holding the timezone this install was set up with.
TZ_CONFIG_KEY = "timezone.expected"

# Set once the USER has confirmed the timezone, as opposed to it merely having
# been auto-detected. The distinction matters: seeding from the environment
# records whatever the container resolved to, and a container with no TZ set
# resolves to UTC. Auto-detection cannot tell "correct" from "default", so an
# install that was wrong from the first boot would be verified as correct
# forever. Only a person can settle that.
TZ_CONFIRMED_KEY = "timezone.confirmed"

# Set once at startup so the time.tz_ok sensor can compare without re-reading
# the database on every evaluation. None = not yet checked this process.
_expected: str | None = None


def resolve_tz_name() -> str:
    """The IANA timezone name this process is actually running in.

    Ordered by trustworthiness: ``TZ`` wins because glibc honours it over
    ``/etc/localtime``, so it is what the clock is really using. ``tzname`` is
    the last resort and returns an abbreviation (``AEST``), not an IANA name —
    good enough to detect a change, which is all this is for.
    """
    env = (os.environ.get("TZ") or "").strip()
    if env:
        return env

    try:
        text = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass

    try:
        target = os.readlink("/etc/localtime")
        marker = "zoneinfo/"
        if marker in target:
            return target.split(marker, 1)[1]
    except OSError:
        pass

    return time.tzname[0] if time.tzname else "UTC"


def utc_offset_hours() -> float:
    """Current offset from UTC in hours (DST-aware)."""
    return round(time.localtime().tm_gmtoff / 3600, 2)


def expected_tz() -> str | None:
    """The recorded timezone, or None if this process hasn't checked yet."""
    return _expected


def tz_ok() -> bool | None:
    """True/False against the recorded timezone; None when nothing is recorded."""
    if _expected is None:
        return None
    return resolve_tz_name() == _expected


async def check_and_record(db: Any) -> dict:
    """Compare the running timezone to the recorded one; seed it on first run.

    Returns ``{expected, actual, offset_h, status}`` where status is:
      ``seeded``   — first run, nothing to compare against, now recorded
      ``ok``       — matches
      ``mismatch`` — the clock moved since install; schedules WILL fire at the
                     wrong wall-clock time until it is put back

    Never raises and never self-heals: rewriting the stored value on mismatch
    would turn the alarm into a rubber stamp. Accepting a new timezone is a
    deliberate act (``record_timezone``), because a real relocation and a broken
    mount look identical from in here.
    """
    global _expected

    # Imported lazily: db.py imports heavily and this module is also used by
    # the sensor layer, which must stay cheap.
    from franklinwh_bridge.store.db import get_app_config, set_app_config

    actual = resolve_tz_name()
    offset = utc_offset_hours()

    try:
        stored = await get_app_config(db, TZ_CONFIG_KEY, None)
    except Exception as exc:  # a config read must never block startup
        logger.warning("Timezone check skipped (config read failed): %s", exc)
        _expected = actual
        return {"expected": None, "actual": actual, "offset_h": offset, "status": "seeded"}

    if not stored:
        try:
            await set_app_config(db, TZ_CONFIG_KEY, actual)
        except Exception as exc:
            logger.warning("Could not record timezone %s: %s", actual, exc)
        _expected = actual
        logger.info(
            "Timezone recorded for this install: %s (UTC%+g). "
            "Schedules and TOU windows use this clock.", actual, offset,
        )
        return {"expected": actual, "actual": actual, "offset_h": offset, "status": "seeded"}

    _expected = stored
    if stored == actual:
        return {"expected": stored, "actual": actual, "offset_h": offset, "status": "ok"}

    logger.error(
        "TIMEZONE CHANGED: this install was set up as %s but is now running as "
        "%s (UTC%+g). Every schedule trigger and TOU window will fire at the "
        "wrong wall-clock time until this is corrected — check the TZ setting "
        "on the container. If the move is intentional, re-record it in Settings.",
        stored, actual, offset,
    )
    return {"expected": stored, "actual": actual, "offset_h": offset, "status": "mismatch"}


async def record_timezone(db: Any, name: str | None = None) -> str:
    """Accept the current (or given) timezone as this install's expected one."""
    global _expected

    from franklinwh_bridge.store.db import set_app_config

    value = (name or resolve_tz_name()).strip()
    await set_app_config(db, TZ_CONFIG_KEY, value)
    _expected = value
    logger.info("Timezone for this install re-recorded as %s", value)
    return value


def _looks_like_a_container_default(name: str) -> bool:
    """Is this timezone more likely an unset default than a choice?

    A container with no TZ and no mounted zoneinfo reports UTC. Someone in
    London or Reykjavik legitimately runs UTC, so this only raises the prompt's
    prominence — it never refuses the value.
    """
    return name.strip().upper() in {"UTC", "ETC/UTC", "GMT", "ETC/GMT", "UCT"}


async def status(db: Any) -> dict:
    """Everything the setup prompt and the Settings panel need.

    ``needs_confirmation`` is the question "has a human ever agreed to this?",
    deliberately separate from whether the clock has since drifted.
    """
    from franklinwh_bridge.store.db import get_app_config

    actual = resolve_tz_name()
    try:
        expected = await get_app_config(db, TZ_CONFIG_KEY, None)
        confirmed = await get_app_config(db, TZ_CONFIRMED_KEY, None)
    except Exception:
        expected, confirmed = None, None

    lt = time.localtime()
    return {
        "timezone": actual,
        "expected": expected,
        "confirmed": bool(confirmed),
        "needs_confirmation": not confirmed,
        "matches_expected": (expected is None or expected == actual),
        "utc_offset_h": utc_offset_hours(),
        "abbreviation": lt.tm_zone,
        "local_time": time.strftime("%Y-%m-%d %H:%M:%S", lt),
        # True when the value looks like a container default rather than a
        # choice — the prompt says so rather than quietly accepting it.
        "looks_like_default": _looks_like_a_container_default(actual),
    }


async def confirm_timezone(db: Any, name: str | None = None) -> dict:
    """Record the user's decision. ``name`` None = "the detected one is right".

    Confirming also (re)records the expected timezone, so a deliberate move is
    not then reported as drift on the next start.
    """
    from franklinwh_bridge.store.db import set_app_config

    value = await record_timezone(db, name)
    await set_app_config(db, TZ_CONFIRMED_KEY, "1")
    logger.info("Timezone confirmed by user: %s", value)
    return await status(db)
