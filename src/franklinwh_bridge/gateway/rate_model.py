"""Seasonal time-of-use rate model — resolve "what does a kWh cost right now?".

The bridge priced exports, demand and standing charges but had no import-rate
model at all, so a TOU plan's peak/off-peak rates had nowhere to live and the
base feed-in tariff had nowhere at all. Two of six rates on a real AGL bill
were unrepresentable.

Shape (stored in a service's ``pricing.seasons``)::

    seasons: [
      { id, name,
        months: [11,12,1,2,3,6,7,8],      # [] = all year
        time_periods: { off_peak: {buy, sell}, on_peak: {...}, ... },
        blocks: [ {start:"15:00", end:"17:00", time_period:"on_peak", days:[0..6]} ] }
    ]

Three concepts, deliberately separate:

* the **season** owns which months it covers,
* a **block** owns hours (and optionally days-of-week), and names a time period,
* a **time period** owns the price pair ``{buy, sell}``.

**Why blocks name a time period rather than carrying prices.** A real plan's import
and export boundaries do not line up. AGL's peak import runs 15:00-21:00 but
its evening feed-in runs 17:00-21:00, so that span is two blocks sharing a buy
price and differing on sell. Treating import windows and export windows as
separate structures — what this repo did before — cannot express that without
contradicting itself.

**Time periods are named, not price-derived.** AGL's 07:00-08:00 morning-FiT
band prices identically to off-peak; it is still its own period because the
plan names it. Never dedupe periods by price.

Pure functions: no DB, no clock of its own. Everything takes ``now``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

#: The fixed vocabulary of TIME PERIODS — FranklinWH's own term for a tariff
#: band, so the bridge and the vendor app say the same thing. Internally it is
#: ``time_period`` rather than ``period`` because this codebase already uses
#: "period" 180-odd times to mean the BILLING period (period_start,
#: demand.period_charge, energy.period_import_kwh); a bare "period" would be
#: genuinely ambiguous.
#:
#: Fixed rather than user-definable: four covers the plans seen so far, and a
#: fixed set keeps the resolver, the UI grid and the sensor enum in agreement.
TIME_PERIODS: tuple[str, ...] = ("super_off_peak", "off_peak", "mid_peak", "on_peak")

TIME_PERIOD_LABELS: dict[str, str] = {
    "super_off_peak": "Super Off-Peak",
    "off_peak": "Off-Peak",
    "mid_peak": "Mid-Peak",
    "on_peak": "On-Peak",
}


def periods_of(season: Any) -> dict:
    """A season's time-period rates.

    Accepts the pre-rename ``waves`` key as a fallback: profiles exported
    before the rename, and any database not yet migrated, must keep working —
    an import that silently priced nothing would be worse than the old name.
    """
    if not isinstance(season, dict):
        return {}
    return season.get("time_periods") or season.get("waves") or {}


def period_of(block: Any) -> str | None:
    """The time period a block names, tolerating the pre-rename ``wave`` key."""
    if not isinstance(block, dict):
        return None
    return block.get("time_period") or block.get("wave")


def tier_rate(value: Any, used_kwh: float | None) -> float | None:
    """Price from a scalar rate OR a tier ladder.

    A wave's price may be a plain number, or tiers priced by CUMULATIVE
    consumption this billing period::

        buy: [ {up_to_kwh: 1000, rate: 0.22}, {rate: 0.28} ]

    The last tier omits ``up_to_kwh`` and is unbounded. Tiers live on the wave
    rather than beside the season, so "tiered within a time-of-use band" — a
    hybrid plan — needs no extra concept: a time period with a tiered price IS
    hybrid.

    With consumption unknown the FIRST tier is used, because a period starts at
    zero and that is the honest answer before any energy has flowed.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, list) or not value:
        return None

    tiers = [t for t in value if isinstance(t, dict)]
    if not tiers:
        return None
    used = float(used_kwh or 0.0)

    # Bounded tiers first, in ascending order, then whatever is unbounded.
    bounded = sorted(
        (t for t in tiers if isinstance(t.get("up_to_kwh"), (int, float))),
        key=lambda t: float(t["up_to_kwh"]),
    )
    for t in bounded:
        if used < float(t["up_to_kwh"]):
            r = t.get("rate")
            return float(r) if isinstance(r, (int, float)) else None

    unbounded = [t for t in tiers if not isinstance(t.get("up_to_kwh"), (int, float))]
    last = unbounded[0] if unbounded else (bounded[-1] if bounded else None)
    if last is None:
        return None
    r = last.get("rate")
    return float(r) if isinstance(r, (int, float)) else None


def tier_index(value: Any, used_kwh: float | None) -> int | None:
    """Which tier applies (1-based), or None when the price isn't tiered."""
    if not isinstance(value, list) or not value:
        return None
    tiers = [t for t in value if isinstance(t, dict)]
    bounded = sorted(
        (t for t in tiers if isinstance(t.get("up_to_kwh"), (int, float))),
        key=lambda t: float(t["up_to_kwh"]),
    )
    used = float(used_kwh or 0.0)
    for i, t in enumerate(bounded, start=1):
        if used < float(t["up_to_kwh"]):
            return i
    return len(bounded) + 1


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


def resolve(seasons: Any, now: datetime, used_kwh: float | None = None) -> dict:
    """What a kWh costs right now.

    Returns ``{season, wave, wave_label, buy, sell, billable, reason}``. Every
    value is None when it cannot be determined, and ``reason`` says why — a
    gap in the blocks is a configuration error worth surfacing, not a silent
    zero that would quietly under-bill.
    """
    blank = {
        "season": None, "time_period": None, "time_period_label": None,
        "buy": None, "sell": None, "billable": None, "tier": None,
        "reason": "not_configured",
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

    period = period_of(block)
    rates = periods_of(season).get(period) or {}
    raw_buy, raw_sell = rates.get("buy"), rates.get("sell")
    buy = tier_rate(raw_buy, used_kwh)
    sell = tier_rate(raw_sell, used_kwh)

    return {
        "season": season.get("name") or season.get("id"),
        "time_period": period,
        "time_period_label": TIME_PERIOD_LABELS.get(period, period),
        "buy": buy,
        "sell": sell,
        "tier": tier_index(raw_buy, used_kwh),
        # A free-import window is simply a wave priced at zero — the same
        # construct as any other band, so nothing special-cases it.
        "billable": (None if buy is None else bool(buy > 0.0)),
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

        periods = periods_of(s)

        # Tier ladders: an unbounded last tier is what makes the ladder total.
        # Without one, consumption past the final threshold has no price and
        # would be recorded as unpriced rather than billed.
        for wname, wrates in periods.items():
            if not isinstance(wrates, dict):
                continue
            for side in ("buy", "sell"):
                ladder = wrates.get(side)
                if not isinstance(ladder, list):
                    continue
                label = TIME_PERIOD_LABELS.get(wname, wname)
                if not ladder:
                    problems.append(f"'{name}' {label} {side} has an empty tier list.")
                    continue
                bounds = [
                    t.get("up_to_kwh") for t in ladder
                    if isinstance(t, dict) and isinstance(t.get("up_to_kwh"), (int, float))
                ]
                if len(bounds) == len(ladder):
                    problems.append(
                        f"'{name}' {label} {side} tiers stop at "
                        f"{max(bounds):g} kWh — add a final tier with no limit."
                    )
                if any(
                    not isinstance(t, dict) or not isinstance(t.get("rate"), (int, float))
                    for t in ladder
                ):
                    problems.append(f"'{name}' {label} {side} has a tier with no rate.")
                if len(set(bounds)) != len(bounds):
                    problems.append(f"'{name}' {label} {side} has duplicate tier limits.")

        for b in blocks:
            w = period_of(b)
            if w not in TIME_PERIODS:
                problems.append(f"'{name}' has a block with an unknown time period: {w!r}.")
            elif w not in periods:
                problems.append(
                    f"'{name}' uses {TIME_PERIOD_LABELS.get(w, w)} but prices no rate for it."
                )

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
