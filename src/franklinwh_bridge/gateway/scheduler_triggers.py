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


# ── previous fire (mirror of next_fire_at, looking backwards) ──


def _prev_oneoff(trigger: dict, now: datetime) -> datetime | None:
    raw = trigger.get("fire_at")
    if not raw:
        return None
    try:
        fire = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return None
    fire = _coerce_awareness(fire, now)
    return fire if fire <= now else None


def _prev_daily(trigger: dict, now: datetime) -> datetime | None:
    hm = _parse_hhmm(trigger.get("time_of_day"))
    if hm is None:
        return None
    h, m = hm
    today = _at(now, h, m, now)
    if today <= now:
        return today
    return _at(now - timedelta(days=1), h, m, now)


def _prev_weekly(trigger: dict, now: datetime) -> datetime | None:
    hm = _parse_hhmm(trigger.get("time_of_day"))
    if hm is None:
        return None
    days = {int(d) for d in (trigger.get("days_of_week") or []) if 0 <= int(d) <= 6}
    if not days:
        return None
    h, m = hm
    for offset in range(8):  # scan back up to a full week (incl. today)
        day = now - timedelta(days=offset)
        if day.weekday() not in days:
            continue
        cand = _at(day, h, m, now)
        if cand <= now:
            return cand
    return None


def _prev_interval(trigger: dict, now: datetime) -> datetime | None:
    try:
        every = int(trigger.get("every_seconds"))
    except (ValueError, TypeError):
        return None
    if every <= 0:
        return None
    anchor_raw = trigger.get("anchor_time")
    hm = _parse_hhmm(anchor_raw) if anchor_raw else (0, 0)
    if hm is None:
        return None
    anchor = _at(now, hm[0], hm[1], now)
    if anchor > now:
        anchor -= timedelta(days=1)
    elapsed = (now - anchor).total_seconds()
    k = int(elapsed // every)
    return anchor + timedelta(seconds=every * k)  # <= now by construction


#: Minimum segment width (minutes) so a brief/zero-duration fire is still visible
#: on the 24h timeline bar.
MIN_SEGMENT_MIN = 8


def day_segments(
    trigger_kind: str,
    spec: dict,
    duration_s: int | None,
    weekday: int,
) -> list[tuple[int, int]]:
    """Timeline segments (start_min, end_min) a trigger produces on ``weekday``
    (0=Mon..6=Sun), for the 24h preview bar. Pure — no clock.

    ``always`` has no discrete fire → no segments. A fire+duration crossing
    midnight is clamped to end-of-day (the bar is per-day). ``interval`` is
    capped to keep a dense schedule from flooding the bar.
    """
    dur_min = max(int((duration_s or 0) // 60), MIN_SEGMENT_MIN)
    spec = spec or {}
    out: list[tuple[int, int]] = []

    def add(start_min: int) -> None:
        if 0 <= start_min < 1440:
            out.append((start_min, min(start_min + dur_min, 1440)))

    if trigger_kind == "daily":
        hm = _parse_hhmm(spec.get("time_of_day"))
        if hm:
            add(hm[0] * 60 + hm[1])
    elif trigger_kind == "weekly":
        hm = _parse_hhmm(spec.get("time_of_day"))
        days = {int(d) for d in (spec.get("days_of_week") or []) if 0 <= int(d) <= 6}
        if hm and weekday in days:
            add(hm[0] * 60 + hm[1])
    elif trigger_kind == "interval":
        try:
            every_min = max(1, int(spec.get("every_seconds")) // 60)
        except (ValueError, TypeError):
            every_min = 0
        if every_min:
            anchor_hm = _parse_hhmm(spec.get("anchor_time")) if spec.get("anchor_time") else (0, 0)
            anchor = (anchor_hm[0] * 60 + anchor_hm[1]) if anchor_hm else 0
            first = anchor % every_min
            t = first
            count = 0
            while t < 1440 and count < 96:  # cap dense schedules
                add(t)
                t += every_min
                count += 1
    elif trigger_kind == "oneoff":
        raw = spec.get("fire_at")
        if raw:
            try:
                fire = datetime.fromisoformat(str(raw))
                if fire.weekday() == weekday:  # preview on the matching weekday
                    add(fire.hour * 60 + fire.minute)
            except (ValueError, TypeError):
                pass
    # "always" and anything else → no segments
    return out


def prev_fire_at(trigger: dict | None, now: datetime) -> datetime | None:
    """Most recent fire instant at/before ``now``, or None.

    The backward counterpart of ``next_fire_at``. The engine uses it to derive a
    trigger entry's synthetic active window ``[prev_fire, prev_fire + duration]``
    without holding fire state. ``always`` and invalid specs return None (the
    engine treats ``always`` as continuously active, condition-gated).
    """
    if not isinstance(trigger, dict):
        return None
    now = now.replace(second=0, microsecond=0)
    kind = trigger.get("kind")
    if kind == "oneoff":
        return _prev_oneoff(trigger, now)
    if kind == "daily":
        return _prev_daily(trigger, now)
    if kind == "weekly":
        return _prev_weekly(trigger, now)
    if kind == "interval":
        return _prev_interval(trigger, now)
    return None
