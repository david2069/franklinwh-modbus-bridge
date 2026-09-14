"""Seasonal time-of-use rate model — resolve "what does a kWh cost right now?".

The bridge priced exports, demand and standing charges but had no import-rate
model at all, so a TOU plan's peak/off-peak rates had nowhere to live and the
base feed-in tariff had nowhere at all. Two of six rates on a real AGL bill
were unrepresentable.

Shape (stored in a service's ``pricing.seasons``)::

    seasons: [
      { id, name,
        months: [11,12,1,2,3,6,7,8],      # [] = all year
        waves:  { off_peak: {buy, sell}, on_peak: {...}, ... },
        blocks: [ {start:"15:00", end:"17:00", wave:"on_peak", days:[0..6]} ] }
    ]

Three concepts, deliberately separate:

* the **season** owns which months it covers,
* a **block** owns hours (and optionally days-of-week), and names a wave,
* a **wave** owns the price pair ``{buy, sell}``.

**Why blocks name a wave rather than carrying prices.** A real plan's import
and export boundaries do not line up. AGL's peak import runs 15:00-21:00 but
its evening feed-in runs 17:00-21:00, so that span is two blocks sharing a buy
price and differing on sell. Treating import windows and export windows as
separate structures — what this repo did before — cannot express that without
contradicting itself.

**Waves are named, not price-derived.** AGL's 07:00-08:00 morning-FiT band
prices identically to off-peak; it is still its own wave because the plan names
it. Never dedupe waves by price.

Pure functions: no DB, no clock of its own. Everything takes ``now``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: The fixed wave vocabulary (user-definable waves were considered and
#: deferred — four covers the plans we have, and a fixed set keeps the
#: resolver, the UI grid and the sensor enum in agreement).
WAVES: tuple[str, ...] = ("super_off_peak", "off_peak", "mid_peak", "on_peak")

WAVE_LABELS: dict[str, str] = {
    "super_off_peak": "Super Off-Peak",
    "off_peak": "Off-Peak",
    "mid_peak": "Mid-Peak",
    "on_peak": "On-Peak",
}


def _hhmm(raw: object) -> int | None:
    """"HH:MM" → minutes since midnight."""
    if not isinstance(raw, str) or ":" not in raw:
        return None
    try:
        h, m = raw.split(":", 1)
        h, m = int(h), int(m)
    except ValueError:
        return None
    if not (0 <= h <= 24 and 0 <= m < 60):
        return None
    return h * 60 + m


def _covers(block: dict, now: datetime) -> bool:
    """Does this block cover ``now``?

    ``days`` empty means every day. An end at or before the start wraps past
    midnight (21:00→06:00), which is how overnight off-peak is usually written.
    ``24:00`` is accepted as end-of-day, since plans are written that way.
    """
    days = block.get("days") or []
    if days and now.weekday() not in days:
        return False

    start = _hhmm(block.get("start"))
    end = _hhmm(block.get("end"))
    if start is None or end is None:
        return False

    minute = now.hour * 60 + now.minute
    if end <= start:                      # wraps midnight
        return minute >= start or minute < end
    return start <= minute < end


def find_season(seasons: Any, now: datetime) -> dict | None:
    """The season covering ``now``'s month.

    A season with no months acts as the catch-all, and is only used when no
    month-specific season matches — otherwise adding an all-year season would
    shadow every seasonal one.
    """
    if not isinstance(seasons, list):
        return None
    fallback = None
    for s in seasons:
        if not isinstance(s, dict):
            continue
        months = s.get("months") or []
        if not months:
            fallback = fallback or s
        elif now.month in months:
            return s
    return fallback


def resolve(seasons: Any, now: datetime) -> dict:
    """What a kWh costs right now.

    Returns ``{season, wave, wave_label, buy, sell, billable, reason}``. Every
    value is None when it cannot be determined, and ``reason`` says why — a
    gap in the blocks is a configuration error worth surfacing, not a silent
    zero that would quietly under-bill.
    """
    blank = {
        "season": None, "wave": None, "wave_label": None,
        "buy": None, "sell": None, "billable": None, "reason": "not_configured",
    }
    if not seasons:
        return blank

    season = find_season(seasons, now)
    if season is None:
        return {**blank, "reason": "no_season_for_month"}

    block = next(
        (b for b in (season.get("blocks") or []) if isinstance(b, dict) and _covers(b, now)),
        None,
    )
    if block is None:
        # The plan covers this month but not this hour. Reported rather than
        # defaulted, because guessing a rate produces a confident wrong bill.
        return {
            **blank,
            "season": season.get("name") or season.get("id"),
            "reason": "no_block_for_time",
        }

    wave = block.get("wave")
    rates = (season.get("waves") or {}).get(wave) or {}
    buy = rates.get("buy")
    sell = rates.get("sell")

    return {
        "season": season.get("name") or season.get("id"),
        "wave": wave,
        "wave_label": WAVE_LABELS.get(wave, wave),
        "buy": float(buy) if isinstance(buy, (int, float)) else None,
        "sell": float(sell) if isinstance(sell, (int, float)) else None,
        # A free-import window is simply a wave priced at zero — the same
        # construct as any other band, so nothing special-cases it.
        "billable": (
            None if not isinstance(buy, (int, float)) else bool(float(buy) > 0.0)
        ),
        "reason": "ok",
    }


def validate(seasons: Any) -> list[str]:
    """Problems a user should fix, in plain words. Empty list = usable.

    Coverage is checked at hourly resolution per season: a plan that prices
    23 hours a day is a real misconfiguration, and finding it at setup beats
    finding it in a month-end total.
    """
    problems: list[str] = []
    if not isinstance(seasons, list) or not seasons:
        return ["No seasons defined."]

    claimed: dict[int, str] = {}
    for s in seasons:
        if not isinstance(s, dict):
            problems.append("A season is not an object.")
            continue
        name = s.get("name") or s.get("id") or "unnamed season"

        for m in s.get("months") or []:
            if m in claimed:
                problems.append(
                    f"Month {m} is claimed by both '{claimed[m]}' and '{name}'."
                )
            else:
                claimed[m] = name

        blocks = s.get("blocks") or []
        if not blocks:
            problems.append(f"'{name}' has no time blocks.")
            continue

        waves = s.get("waves") or {}
        for b in blocks:
            w = b.get("wave") if isinstance(b, dict) else None
            if w not in WAVES:
                problems.append(f"'{name}' has a block with an unknown wave: {w!r}.")
            elif w not in waves:
                problems.append(f"'{name}' uses {WAVE_LABELS.get(w, w)} but prices no rate for it.")

        # Hourly coverage, on a weekday and a weekend day, so a days-restricted
        # block can't hide a gap.
        for probe_day, label in ((datetime(2026, 1, 5), "weekdays"),
                                 (datetime(2026, 1, 10), "weekends")):
            missing = [
                h for h in range(24)
                if not any(
                    isinstance(b, dict) and _covers(b, probe_day.replace(hour=h))
                    for b in blocks
                )
            ]
            if missing:
                hours = ", ".join(f"{h:02d}:00" for h in missing[:4])
                more = "…" if len(missing) > 4 else ""
                problems.append(f"'{name}' prices no rate on {label} at {hours}{more}.")

    return problems
