"""Scheduler v2 trigger evaluation — ``next_fire_at`` per FWHAI trigger kind.

Pure, controller-free. Given a trigger spec (the ``trigger_spec`` JSON on a
``schedules`` row) and a reference ``now``, return the next instant the trigger
should fire, or ``None`` if it never will again (a past one-off, an invalid
spec, or ``always`` which has no discrete fire time).

FWHAI-parity kinds (see the planning brief §5) — no cron/RRULE:

    OneOff    { kind: "oneoff",   fire_at: iso8601 }
    Daily     { kind: "daily",    time_of_day: "HH:MM" }
    Weekly    { kind: "weekly",   time_of_day: "HH:MM", days_of_week: [0..6] }  # Mon=0
    Interval  { kind: "interval", every_seconds: int, anchor_time: "HH:MM" | null }
    Always    { kind: "always" }                                               # → None

Timezone handling: computation follows the awareness of ``now``. If ``now`` is
naive (the current engine uses ``datetime.now()``), fires are naive local wall
times — matching the existing SCH1 engine. If ``now`` is timezone-aware
(``ZoneInfo``), day/weekly fires are built with that tzinfo so each candidate
carries the correct per-date UTC offset across a DST transition. ``fire_at`` on a
one-off is reconciled to ``now``'s awareness so the two are always comparable.

Known limitation: ``interval`` uses wall-clock ``timedelta`` stepping, so an
interval spanning a DST boundary can drift by the offset delta for that step.
Sub-day intervals (the common case) are unaffected; absolute-time-exact interval
math is deferred to the Phase-2 catchup work, which resolves fires against
``last_ok_ts``.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

VALID_KINDS = frozenset({"oneoff", "daily", "weekly", "interval", "always"})


def _parse_hhmm(raw: object) -> tuple[int, int] | None:
    """'HH:MM' → (hour, minute), or None if malformed/out of range."""
    try:
        h_str, m_str = str(raw).strip().split(":")
        h, m = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def _coerce_awareness(dt: datetime, ref: datetime) -> datetime:
    """Make ``dt`` comparable to ``ref``: match aware/naive-ness (best effort)."""
    if (dt.tzinfo is None) == (ref.tzinfo is None):
        return dt
    if ref.tzinfo is None:
        return dt.replace(tzinfo=None)
    return dt.replace(tzinfo=ref.tzinfo)


def _at(day_dt: datetime, h: int, m: int, ref: datetime) -> datetime:
    """A datetime on ``day_dt``'s date at HH:MM, carrying ref's tzinfo."""
    return datetime.combine(day_dt.date(), time(h, m), tzinfo=ref.tzinfo)


def _oneoff(trigger: dict, now: datetime) -> datetime | None:
    raw = trigger.get("fire_at")
    if not raw:
        return None
    try:
        fire = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    fire = _coerce_awareness(fire, now)
    return fire if fire >= now else None


def _daily(trigger: dict, now: datetime) -> datetime | None:
    hm = _parse_hhmm(trigger.get("time_of_day"))
    if hm is None:
        return None
    h, m = hm
    for offset in (0, 1):  # today, else tomorrow
        cand = _at(now + timedelta(days=offset), h, m, now)
        if cand >= now:
            return cand
    return None  # defensive; the tomorrow candidate always satisfies


def _weekly(trigger: dict, now: datetime) -> datetime | None:
    hm = _parse_hhmm(trigger.get("time_of_day"))
    if hm is None:
        return None
    days = {int(d) for d in (trigger.get("days_of_week") or []) if 0 <= int(d) <= 6}
    if not days:
        return None
    h, m = hm
    for offset in range(8):  # scan up to a full week ahead (incl. today)
        day = now + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        cand = _at(day, h, m, now)
        if cand >= now:
            return cand
    return None


def _interval(trigger: dict, now: datetime) -> datetime | None:
    try:
        every = int(trigger.get("every_seconds"))
    except (ValueError, TypeError):
        return None
    if every <= 0:
        return None
    anchor_raw = trigger.get("anchor_time")
    hm = _parse_hhmm(anchor_raw) if anchor_raw else (0, 0)  # default anchor = midnight
    if hm is None:
        return None
    anchor = _at(now, hm[0], hm[1], now)
    if anchor > now:  # anchor later today → step back to yesterday's anchor
        anchor -= timedelta(days=1)
    elapsed = (now - anchor).total_seconds()
    k = int(elapsed // every)
    cand = anchor + timedelta(seconds=every * k)
    if cand < now:
        cand = anchor + timedelta(seconds=every * (k + 1))
    return cand


def next_fire_at(trigger: dict | None, now: datetime) -> datetime | None:
    """Next fire instant at/after ``now`` for a trigger spec, or None.

    ``always`` and any invalid/unknown spec return None (``always`` has no
    discrete fire — its job is evaluated purely on the entry-conditions tree
    every tick).
    """
    if not isinstance(trigger, dict):
        return None
    # Zero sub-minute noise so a fire landing on the current minute still counts
    # as "now" — matches the existing engine's minute-resolution semantics.
    now = now.replace(second=0, microsecond=0)
    kind = trigger.get("kind")
    if kind == "oneoff":
        return _oneoff(trigger, now)
    if kind == "daily":
        return _daily(trigger, now)
    if kind == "weekly":
        return _weekly(trigger, now)
    if kind == "interval":
        return _interval(trigger, now)
    # "always" and anything unrecognised
    return None
