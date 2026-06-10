# Reporting & Power-Flow Sankey — Design (Backlog)

**Status:** Backlog / design-for-review — **not scheduled.** Captured so the
idea isn't lost; do not start until current in-flight work is closed.
**Inspiration:** FranklinWH HA Integrator "Reporting" page (energy-flow Sankey +
source→destination breakdown). That repo is read-only reference — concept only.

---

## 1. Goal

A **Reporting** side-tab with:
- Range selector: **Day (5-min) / Day (hourly) / Week / Month / Year**.
- Summary cards: **Solar Production, Battery Flows, Grid Flows, Home Load** (kWh,
  with source→destination sub-breakdowns).
- **Energy-flow Sankey** (live + fixed day/range).
- The existing **power timeline** chart, with a **multi-gateway filter**.

---

## 2. Data — what we have vs need

- **Recorded time-series** (`metrics`): instantaneous **power** only —
  `battery_w, grid_w, solar_w, home_w, soc`. (See
  [multi-gateway-and-mock-lifecycle.md](multi-gateway-and-mock-lifecycle.md).)
- **Live energy counters** (not stored as series): `grid_export/import_kwh`
  (701.TotWhInj/Abs), `pv_energy_total`, `battery_charged/discharged_kwh`
  (714.DCWhInj/Abs).

**Conclusion:** the Sankey/cards are **derivable from the recorded power series**
by integrating each channel over the period and applying a flow-allocation model
(§3). **No new collection needed** for day/range; the **live** Sankey uses the
current instantaneous power directly. (A future option: also store energy-counter
deltas for exact device-reported totals — not required for v1.)

---

## 3. Flow-allocation model ⚠️ (needs sign-off — this decides the numbers)

At each sample we have `solar (≥0)`, `home (≥0)`, `battery` (+ = discharge,
− = charge), `grid` (sign per existing convention). Allocate per sample (then
integrate to kWh):

1. **Solar serves:** Home first → then Battery charge → then Grid export.
2. **Battery discharge serves:** remaining Home → then Grid export.
3. **Grid import serves:** remaining Home → then Battery charge.

Yields the Sankey edges: `Solar→{Home,Battery,GridExport}`,
`Battery→{Home,GridExport}`, `Grid→{Home,Battery}`. Integrate per 5-min bucket
for accuracy. **Assumptions to confirm:** priority order above, sign
conventions, and handling of simultaneous charge+export edge cases.

---

## 4. API

- `GET /api/reporting/energy?range=day|week|month|year&date=…&gateway_id=…`
  → integrated kWh per channel **and** per Sankey edge (runs §3 over `metrics`).
- `GET /api/reporting/flow/live?gateway_id=…` → instantaneous flow edges (W).
- Reuse `query_metrics` for the timeline; add optional `gateway_id` filter.

---

## 5. UI

- New **Reporting** tab (sidebar) — own Alpine component.
- Range selector + date stepper; four summary cards; Sankey panel; timeline panel.
- **Sankey renderer:** a small dependency-light JS sankey (e.g. d3-sankey or a
  minimal SVG layout) served locally (no CDN, matching current vendoring).
- **Multi-gateway filter** (timeline + Sankey): All / per-gateway toggles / Site,
  driven by the existing gateway list. (This is the "toggle gateways on/off"
  request — it belongs here, where multiple recorded gateways are compared.)

---

## 6. Phasing

| Phase | What |
|---|---|
| P1 | Energy aggregation + flow-allocation backend + tests |
| P2 | Reporting tab: cards + power timeline + range selector |
| P3 | Sankey (live + day/range) |
| P4 | Multi-gateway filter on timeline + Sankey |

Prereq this also satisfies: **gateway-scoped Power History** (P4 makes the chart
per-gateway instead of all-merged).

---

## 7. Open questions

1. Flow-allocation priority order (§3) — confirm before P1.
2. Sankey library choice (d3-sankey vs hand-rolled SVG) — vendoring constraints.
3. Energy source: derive-from-power (v1) vs also store counter deltas (later)?
4. Does Reporting need per-gateway **and** Site aggregate, or Site only?
