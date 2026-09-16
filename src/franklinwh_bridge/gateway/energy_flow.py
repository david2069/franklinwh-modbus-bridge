"""Directional energy flows — who supplied what, and where it went.

The lifetime counters in :mod:`energy_totals` answer "how much solar" and "how
much export", but not "how much of the solar went to the battery rather than
the grid". A Sankey needs the edges, and no counter reports them: the aGate
publishes four *signed scalars* per sample (solar, grid, battery, home), which
describe the nodes, not the arcs between them.

So the arcs are reconstructed. Per sample the site must balance —

    solar + grid_import + battery_discharge
        == home + grid_export + battery_charge

— and the split is resolved by the physically-motivated merit order every
hybrid inverter follows: **solar serves the house first, charges the battery
with what's left, and exports only the remainder**; the house then draws on
the battery before the grid. That ordering is what makes the answer unique,
because the balance equation alone is underdetermined (three sources feeding
three sinks has more unknowns than constraints).

The result is therefore a *well-founded reconstruction, not a measurement*.
Sub-interval behaviour is invisible: a sample averaging one minute of export
and one minute of import reports only the net, and the reconstruction prices
the net. Shorter polling intervals narrow that error; they don't remove it.
`residual_kwh` reports what failed to balance, so a systematically wrong
assumption shows up as a number rather than as quietly plausible arcs.

Pure — no I/O, no clock. The caller supplies the samples.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# The arcs, in the order a Sankey should stack them.
FLOWS: tuple[str, ...] = (
    "solar_to_home",
    "solar_to_battery",
    "solar_to_grid",
    "battery_to_home",
    "battery_to_grid",
    "grid_to_home",
    "grid_to_battery",
)


def _pos(value: Any) -> float:
    """A missing reading is zero flow, not a crash. The poller returns None for
    any point it failed to read, and one bad sample must not void the day."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0.0 else 0.0


def split_sample(sample: dict) -> dict[str, float]:
    """Resolve one sample's four scalars into the seven arcs, in watts.

    Sign conventions follow the rest of the bridge: ``grid_w`` positive is
    import, ``battery_w`` positive is discharge.
    """
    solar = _pos(sample.get("solar_w"))
    home = _pos(sample.get("home_w"))

    grid = sample.get("grid_w")
    grid_in = _pos(grid)
    grid_out = _pos(-grid) if grid is not None else 0.0

    batt = sample.get("battery_w")
    batt_out = _pos(batt)                       # discharging
    batt_in = _pos(-batt) if batt is not None else 0.0   # charging

    # Solar first: house, then battery, then whatever is left is exported.
    solar_to_home = min(solar, home)
    rem = solar - solar_to_home
    solar_to_battery = min(rem, batt_in)
    rem -= solar_to_battery
    solar_to_grid = min(rem, grid_out)

    # The house takes the balance from the battery before the grid.
    need = home - solar_to_home
    battery_to_home = min(batt_out, need)
    need -= battery_to_home
    grid_to_home = min(grid_in, need)

    # Any discharge not consumed at home is being pushed to the grid; any
    # charge not covered by solar is being pulled from it.
    battery_to_grid = min(batt_out - battery_to_home, max(grid_out - solar_to_grid, 0.0))
    grid_to_battery = min(grid_in - grid_to_home, max(batt_in - solar_to_battery, 0.0))

    return {
        "solar_to_home": solar_to_home,
        "solar_to_battery": solar_to_battery,
        "solar_to_grid": solar_to_grid,
        "battery_to_home": battery_to_home,
        "battery_to_grid": battery_to_grid,
        "grid_to_home": grid_to_home,
        "grid_to_battery": grid_to_battery,
    }


def _gap_cap(deltas: list[float]) -> float:
    """How long a single sample may be held for, derived from the series.

    A fixed cap cannot work here, because the caller's sample spacing is not
    fixed: ``query_metrics_daterange`` auto-buckets, so today's series arrives
    seconds apart while a 90-day series arrives in multi-hour buckets. Any
    constant tight enough to catch an outage in the former would silently
    truncate every legitimate bucket in the latter.

    So the cap is relative — a few multiples of the series' own median step.
    An outage is by definition far longer than the typical step; a bucket
    never is.
    """
    if not deltas:
        return 0.0
    ordered = sorted(deltas)
    median = ordered[len(ordered) // 2]
    return max(median * 3.0, 60.0)


def integrate(
    samples: Iterable[dict],
    *,
    max_gap_s: float | None = None,
) -> dict[str, float]:
    """Integrate the arcs over a series of samples, returning kWh per arc.

    Each sample's power is held until the next one (left Riemann sum) — the
    samples are bucket *averages*, so the value already represents its
    interval rather than an instant.

    ``max_gap_s`` bounds what one sample may be held across. The bridge stops
    polling whenever the container is down, and without a cap a sample either
    side of an eight-hour outage would carry its power across the whole gap and
    invent kWh that never flowed. Capped, an outage under-reports — visibly,
    via ``covered_s`` — instead of fabricating. Defaults to a multiple of the
    series' own median step; pass a value only to override that.
    """
    rows = [s for s in samples if s.get("ts") is not None]
    rows.sort(key=lambda s: s["ts"])

    if max_gap_s is None:
        steps = [rows[i + 1]["ts"] - rows[i]["ts"] for i in range(len(rows) - 1)]
        max_gap_s = _gap_cap([d for d in steps if d > 0])

    totals = dict.fromkeys(FLOWS, 0.0)
    balance_err_wh = 0.0
    covered_s = 0.0

    for i, row in enumerate(rows[:-1]):
        dt = rows[i + 1]["ts"] - row["ts"]
        if dt <= 0:
            continue
        dt = min(dt, max_gap_s)
        covered_s += dt

        hours = dt / 3600.0
        arcs = split_sample(row)
        for key, watts in arcs.items():
            totals[key] += watts * hours

        # What the merit order could not place. Sums as an absolute value: a
        # positive and a negative imbalance are two errors, not cancellation.
        served = arcs["solar_to_home"] + arcs["battery_to_home"] + arcs["grid_to_home"]
        balance_err_wh += abs(_pos(row.get("home_w")) - served) * hours

    out = {k: round(v / 1000.0, 3) for k, v in totals.items()}
    out["residual_kwh"] = round(balance_err_wh / 1000.0, 3)
    out["covered_s"] = round(covered_s, 1)
    out["samples"] = len(rows)
    return out


def node_totals(flows: dict[str, float]) -> dict[str, float]:
    """Roll the arcs up to the per-node figures a summary card shows."""
    g = lambda k: float(flows.get(k) or 0.0)  # noqa: E731
    return {
        "solar": round(g("solar_to_home") + g("solar_to_battery") + g("solar_to_grid"), 3),
        "home": round(g("solar_to_home") + g("battery_to_home") + g("grid_to_home"), 3),
        "grid_import": round(g("grid_to_home") + g("grid_to_battery"), 3),
        "grid_export": round(g("solar_to_grid") + g("battery_to_grid"), 3),
        "battery_charge": round(g("solar_to_battery") + g("grid_to_battery"), 3),
        "battery_discharge": round(g("battery_to_home") + g("battery_to_grid"), 3),
    }
