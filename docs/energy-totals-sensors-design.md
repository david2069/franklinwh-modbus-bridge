# Energy-Total Sensors (Today / Week / Month / YTD) — Design (Backlog)

**Status:** Backlog / design-for-review — **not scheduled.** Scoped 2026-08-08
against the current codebase + a live aGate read. Decisions/requirements below
are from the owner; nothing is built yet.

**One-line goal:** per-source cumulative-energy sensors over **today, this week,
this month, and year-to-date** — usable as Automation condition sensors and
surfaced as HA entities — with period totals that **align with the FranklinWH
Cloud API reporting/analytics endpoint** so the Bridge's numbers match the
FranklinWH app.

---

## 1. Requirements (owner, 2026-08-08)

- **Sources:** grid **import** + **export**, **home load**, **solar**, **battery
  inverter** (charge + discharge), and — **future**, once added via Cloud/Local
  API — **smart circuits**.
- **Periods:** **today**, **this week**, **this month**, **year-to-date** (plus
  **lifetime total**, which is free — see §3).
- **Alignment:** the today/week/month/YTD figures MUST agree with the FranklinWH
  Cloud API reporting/analytics endpoint — same period boundaries (week-start
  day, month/year rollover), same timezone, same rounding — so a user sees
  consistent numbers across the Bridge and the FranklinWH app.
- **Consumption:** exposed as Automation **condition sensors** (`/api/sensors` +
  the Automation Builder dropdown) so users can gate/exit on e.g. "if this
  month's grid export > 300 kWh", and as curated HA entities.

## 2. Current state (verified)

- The scheduler already has a pure sensor registry —
  `gateway/scheduler_sensors.py` `snapshot(points, now)` + `sensor_catalog()` —
  feeding `/api/sensors` and the Automation Builder. New sensors slot in here.
- **The aGate already streams LIFETIME cumulative Wh over Modbus** (confirmed live
  in `sample.points`):
  - `grid_import_wh` (SunSpec 701.TotWhAbs) — e.g. 1,879,145
  - `grid_export_wh` (701.TotWhInj) — e.g. 4,874,971
  - `pv_energy_total_wh` (502.OutWh) — e.g. 14,179,911
  - `dc_energy_charged_wh` (714.DCWhAbs) — battery charge
  - `dc_energy_discharged_wh` (714.DCWhInj) — battery discharge
- **No direct cumulative Wh for home load** (`home_load_ext` is instantaneous W)
  and none for smart circuits (not a Modbus concept).
- The Bridge also stores power samples (`metrics` / `metrics_archive`) — a
  fallback/cross-check, but the Modbus counters are the better primary source.

## 3. Approach

**Lifetime totals — near-free.** Map the existing Modbus counters to condition
sensors directly (Wh → kWh): `energy.grid_import.total_kwh`,
`energy.grid_export.total_kwh`, `energy.solar.total_kwh`,
`energy.battery_charge.total_kwh`, `energy.battery_discharge.total_kwh`. Pure
addition to `scheduler_sensors.py` — no state, no new storage.

**Period totals — a counter DELTA across a boundary.** For each source and each
period `P ∈ {today, week, month, ytd}`:

    energy.<source>.<P>_kwh = (current_lifetime_wh − baseline_wh[source, P]) / 1000

where `baseline_wh[source, P]` is the counter value captured at the **start of
period P**. This is far cheaper and more accurate than summing power samples.
Needs:
- A persisted **baseline store** — a small `energy_baselines` table (or
  `app_config` JSON) keyed `(source, period)` → `{baseline_wh, period_start_ts}`.
- A **roll-over** step (cheap, runs on the engine tick or a daily job): when
  `now` crosses a period boundary, re-capture `baseline_wh[source, P] =
  current_lifetime_wh` and advance `period_start_ts`. Boundaries for
  week/month/year computed to **match FranklinWH** (see §4).
- **Reset/rollover handling:** if a counter DECREASES vs the stored baseline
  (firmware reset / register wrap), re-base and flag — never emit a negative
  total.
- On first run, seed baselines from the current counter (period totals start at 0
  and accrue).

**Home-load energy — DERIVED** (no counter). Track a synthetic cumulative
`home_load_wh` from the energy balance
`home = grid_import + solar + battery_discharge − grid_export − battery_charge`
(integrated from the source counters, so it inherits their accuracy), then treat
it exactly like the others for period deltas.

**Smart circuits — FUTURE.** Not in Modbus; comes via the FranklinWH Cloud/Local
API (see project-multiuser-pwa-wizard + pricing backlogs). Same period-delta
machinery once a per-circuit cumulative energy is available.

## 4. FranklinWH Cloud alignment — the pivotal decision

The period boundaries must match FranklinWH's reporting. Two ways, decide when
picked up:

- **(a) Compute locally, boundaries defined to match.** Use the Modbus counters
  (resilient, works offline) but reverse-engineer FranklinWH's exact
  week-start/month/year/timezone semantics so the totals agree.
- **(b) Pull period totals from the FranklinWH Cloud reporting/analytics
  endpoint.** Guaranteed match, and it also covers smart circuits — but adds a
  Cloud API dependency + auth, and won't work offline.

**Recommended: HYBRID** — local Modbus counters as the real-time/offline source,
reconciled to (and boundary-aligned with) the Cloud reporting endpoint;
smart-circuit + any Cloud-only figures come from (b).

**FIRST STEP when picked up:** inspect the FranklinWH Cloud reporting/analytics
endpoint to capture its exact period definitions (FWHAI —
`~/dev/franklinwh-ha-integrator` — likely already calls it;
READ-ONLY per CLAUDE.md) before choosing (a)/(b).

## 5. Data model

`energy_baselines` (migration; next schema version at build time):

```sql
CREATE TABLE energy_baselines (
    source        TEXT NOT NULL,   -- grid_import|grid_export|solar|
                                   -- battery_charge|battery_discharge|home_load
    period        TEXT NOT NULL,   -- today|this_week|this_month|ytd
    gateway_id    TEXT NOT NULL DEFAULT 'default',
    baseline_wh   REAL NOT NULL,   -- lifetime counter at period start
    period_start  REAL NOT NULL,   -- ts of the current period's start
    PRIMARY KEY (gateway_id, source, period)
);
```

Per-gateway (multi-aGate already exists). Home-load's synthetic cumulative can
live in the same store or a companion `energy_derived` row.

## 6. Sensor namespace

`energy.<source>.<period>_kwh` (kWh, FranklinWH-style) plus `..._total_kwh`
lifetime. ~6 sources × (4 periods + total) ≈ 30 sensors — group them in the
Automation Builder dropdown (a "source → period" two-step, or an `<optgroup>`
per source) so the list stays usable. All flow through the existing
`scheduler_sensors.snapshot()` / `/api/sensors` path; `snapshot()` gains a small
lookup into the baseline store (it already takes `points` + `now`).

## 7. Phasing

1. **Lifetime `energy.*` sensors** — map the 5 Modbus counters (kWh). Trivial,
   immediately useful in Automations. (Zero storage.)
2. **Period deltas** — `energy_baselines` table + roll-over + reset handling +
   the 4 periods, boundaries per §4. The main work.
3. **Home-load derivation** — synthetic cumulative + its period deltas.
4. **FranklinWH Cloud reconciliation** — align/verify against the reporting
   endpoint (and pull smart-circuit + Cloud-only figures).
5. **HA entities** — curated EntityDefs for the totals ([[feedback-entity-patterns]]).

## 8. Verification

- Unit: delta math, boundary roll-over (today→tomorrow, week/month/year edges,
  DST), counter-reset re-base, home-load derivation.
- Integration: `/api/sensors` shows the `energy.*` sensors with live values;
  `/api/scheduler/evaluate` gates on them.
- Live: cross-check a period total against the FranklinWH app for the same
  window (the alignment acceptance test).
- Browser: the grouped sensor dropdown in the Automation Builder.

## 9. Open questions

- FranklinWH's exact boundaries — **week-start day**, month/year rollover,
  **timezone**, rounding (resolve via §4 first step).
- "This week" start (Mon vs Sun) must follow FranklinWH, not the engine's Mon=0.
- Whether to also expose `season`/`quarter` later (owner narrowed to
  today/week/month/YTD for now — see [[project-automations-extensions-backlog]]).
- Dropdown ergonomics for ~30 sensors (grouping vs a dedicated energy picker).
