# Scheduling & Multi-aGate Orchestration — Design (Backlog)

**Status:** Backlog / design-for-review — **not scheduled.** Captured so the
concept (long noted only in the library roadmap) lives where the foundations
now are: the bridge.

**One-line goal:** time/TOU-driven battery dispatch that can target a single
gateway, a utility-service group, or the whole site — built on the command
handler and the services/phase model already in place.

---

## 1. Scope — deliberately simple

This is **not** a HEMS. Keep it a thin, declarative time→action scheduler.

**In scope**
- Time-based triggers: time-of-day windows, weekday masks, and named **TOU
  periods** (peak/off-peak/shoulder) with start–end times.
- Actions drawn from the **existing command vocabulary**: set operating mode,
  Force Charge/Discharge/Standby, set Self/TOU reserve, Release.
- **Targets:** one gateway, a **service** (→ fan out to its member gateways),
  or the **site**.
- A scheduler loop that evaluates active entries each tick and dispatches via
  the existing `CommandHandler` (and the MP5 group executor for multi-aGate).
- **Native-mode coexistence (§4a):** VPP dispatch overrides native **TOU**
  directly (owner-confirmed) and works without a SPAN unlock — but **releasing
  hands back to native TOU**, so the **release/gap behaviour** (release vs hold)
  is the key lever, not a mode-switch.

**Out of scope (this is FWHAI's domain — see §5)**
- Condition rule-engine (`IF live_data … THEN …`), live-data templating.
- Solar/load **forecasting**, rolling 24h timeline, reserve-charging detection.
- Dynamic-tariff arbitrage / price optimization, HEMS strategy selection.

---

## 2. What FWHAI already has (reference, do not replicate)

`franklinwh-ha-integrator` runs a sophisticated stack we should learn from but
not copy:
- **`AutomationEngine`** (`scheduler_core.py`, ~1500 lines) — a condition
  rule-engine: `evaluate_conditions(live_data, logic, conditions)` + safe
  placeholder templating (`{battery.soc}` → live values).
- **`SchedulePresets`** — named, saved/seeded TOU-style presets (CRUD + builtins).
- **Smart dispatch** — HEMS reserve-charging detection + a forecast-driven
  rolling 24h timeline (`smart_dispatch_rolling_timeline_design.md`).

The bridge wants ~10% of that surface: the *time → dispatch* spine, minus the
rule-engine, forecasting, and tariff optimization.

---

## 3. The bridge's model

A schedule **entry** is a small declarative record:

```
schedule entry
  when    : time spec  — { days: [mon..sun], windows: [{start:"14:00", end:"19:00"}] }
            (a window can carry a TOU label, e.g. "peak")
  action  : one of the command-handler verbs
            mode:<m> | force_charge | force_discharge | force_standby
            | reserve_self:<pct> | reserve_tou:<pct> | release
  params  : optional { power_w | power_pct, duration_s, target_soc }
  target  : { type: gateway|service|site, id?: <gateway_id|service_id> }
  enabled : bool
```

Semantics: while "now" is **inside** a window, the entry's action is the
desired state for its target; on a window **boundary** (enter/exit) the
scheduler issues the dispatch (or a Release on exit if nothing else claims the
target). Idempotent — re-issuing the same active state is a no-op.

### Data model
```
schedules
  id          TEXT PK
  name        TEXT
  enabled     INTEGER
  when_spec   TEXT (JSON)   -- days + windows
  action      TEXT
  params      TEXT (JSON)
  target_type TEXT          -- gateway | service | site
  target_id   TEXT          -- nullable for site
  created_at  REAL
schedule_log (audit: fired_at, entry_id, action, target, result)
```

---

## 4. Execution

- A **scheduler loop** (tick on the minute boundary; cheap) computes, per
  target, the *winning* active entry (last-writer / explicit priority), and
  dispatches only on **state change**.
- Dispatch goes through the existing **`CommandHandler`**, so schedules inherit
  the software watchdog, the standby-handshake release, SPAN-lock write
  verification, and the command audit log — no new control path.
- **Multi-aGate / per-service coordination** layers on **MP5** (the coordinated
  control executor): a `service`/`site` target fans the action out to every
  member gateway's handler, respecting each service's rated amperage, and
  releases the group together. *(MP5 is the prerequisite for multi-aGate
  scheduling; single-gateway scheduling works without it.)*
- **Manual override:** a user-issued Force command **supersedes** the schedule
  for that target and pauses scheduled control until the next window boundary
  (or an explicit "resume schedule"). Surfaces via the existing `command_
  superseded` audit + the FORCED status badge.

### Safety / conflicts
- Schedules never bypass the watchdog or the orphan-release paths.
- One target, one active dispatch: the scheduler must not fight the manual
  controls or another entry — resolve to a single winner per tick.
- SPAN-locked writes fail gracefully (logged, ✗), same as manual.

### 4a. Native-mode (TOU) interaction — RESOLVED (owner-confirmed on hardware)

Writing **`WSetEna=1` + `WSetPct` (M704)** forces the aGate into **VPP Mode,
which overrides whatever it is doing — including native TOU's internal
schedule.** Two consequences:

- ✅ **Taking control needs no mode-switch.** We do *not* have to leave TOU
  first — the VPP setpoint simply wins. And M704 power control is **unrestricted
  by the SPAN lock** (only the `15xxx` mode/reserve writes need SPAN), so the
  scheduler's core dispatch works for **all** users, with or without a SPAN
  unlock. The earlier "switch-out-of-TOU-or-ban" gate is **dropped** — it isn't
  needed.
- ⚠️ **Release hands back to the native schedule.** Setting `WSetEna=0`
  (Release) returns the aGate to whatever it was doing — i.e. **native TOU
  resumes its own schedule.** A schedule therefore can't "release to neutral";
  on a window boundary it must choose (**configurable**):
  - **`release`** → native TOU/mode runs the gap (co-existence: our windows take
    priority, native fills the rest); or
  - **`hold`** → keep VPP active at a neutral setpoint (e.g. `WSetPct=0`
    standby) to keep native suppressed across the gap.

**Control-state detection — do NOT trust `715.LocRemCtl`.** It's a
non-functional vendor point: it stays **"Local"** (`loc_rem_ctl_name='Local'`)
and **never shows "Remote"** even while VPP dispatch is active. So we can't read
a clean "we have control" signal — infer it from **`704.WSetEna`** (1 = our
setpoint is live) plus behavioural confirmation (does battery power track the
commanded setpoint). Logged in the vendor-issue catalog alongside
`ControllerHb`/`DERHb`.

**This is documented from the cloud side in FWHAI** —
`FHAI_AGENT_HANDOFF_operating_mode_run_status.md` separates **`run_status`**
(physical battery action) from **`workMode`** (operating mode), with
**`run_status=9 = "VPP mode"`** = VPP dispatch active and overriding the mode.
That `run_status=9` is the cloud mirror of our Modbus `704.WSetEna=1`. The bridge
should expose an equivalent **"VPP Mode" run-status** (derived from `WSetEna`)
distinct from the operating mode, matching FWHAI / the mobile app's two-line
status (Line 1 = physical action incl. "VPP mode"; Line 2 = controlling
programme / TOU).

**Net:** the pre-flight gate is no longer a TOU-ban; it's just "can we write the
M704 setpoint" (which always works). The real design lever is the **release/gap
behaviour** above.

### 4b. Conflict policy when a dispatch is already active
When an entry fires and the target already has an active dispatch (a manual
Force, or a prior entry) — configurable per-entry, global default:
- **`defer` (default)** — don't disrupt; skip this window, log "deferred —
  control already active." Manual Force always wins under `defer`.
- **`override`** — preempt and take control (logs `command_superseded`).
- **`wait`** — retry within the window until the conflicting control clears.

### 4c. Context preconditions (FWH extensions) — kept minimal
A *small, fixed*, opt-in guard set — **not** a rule-engine:
- Skip charge-from-grid when **solar** (`15502` total / `15503` proximal /
  `15504`–`15505` remote) already covers **home load** (`15506` / `16000`).
- Skip discharge below reserve / below a floor SoC.
- (Operating-mode no longer gates control — §4a — but the schedule may still
  *report* the live mode for context.)
- **Boundary:** anything richer (load/solar/forecast-aware optimization) is
  where this becomes FWHAI's `AutomationEngine` — don't build it here (§5).

---

## 5. Roadmap & future commonality with FWHAI

Keep the door open without paying for it now:
- **Schema as a subset.** Model the `schedules` entry as a *subset* of FWHAI's
  automation row (a time-trigger with no `conditions[]`), so a future
  import/merge — or a shared `franklinwh-scheduler` core — is feasible.
- **Preset format.** If we add named presets, mirror `SchedulePresets`' JSON
  shape so presets could move between FWHAI and the bridge.
- **The convergence endpoint** is the library roadmap's **"Local VPP"** vision
  (multi-site/multi-aGate orchestration + TOU/dynamic-tariff arbitrage). The
  bridge already has the topology (services/phase, MP1–MP4); this scheduler +
  MP5 is the control spine; FWHAI brings the forecasting/HEMS brain. A future
  step could let FWHAI *drive* the bridge's schedule API instead of each
  re-implementing dispatch.
- **Library `TOUSchedule`.** `franklinwh-modbus` has a rudimentary
  `TOUSchedule` (period/price/strategy resolution, unit-tested, but no active
  schedule-following). Optionally reuse it for TOU-window resolution; otherwise
  the bridge owns its own (simpler) time logic. Recommend: bridge owns it;
  treat the library scheduler as optional.
- **Status vocabulary parity.** Align the bridge's status model with FWHAI's
  documented one (`FHAI_AGENT_HANDOFF_operating_mode_run_status.md`): a
  **`run_status`** line (physical action — Standby/Charging/Discharging/**VPP
  mode**, derived from DC power + `WSetEna`) distinct from the **operating
  mode**/programme line. Same vocabulary across Modbus (bridge) and Cloud
  (FWHAI) keeps the scheduler's "scheduled / forced / VPP" states consistent and
  eases a future merge.

---

## 6. UI — Schedule tab (modelled on FWHAI's TOU editor)

A dedicated **Schedule** sidebar item + tab, visually in the family of FWHAI's
TOU editor (sidebar nav → header → visual timeline → block table), but with the
bridge's **own, simpler vocabulary**: it dispatches *command-handler actions*,
not tariff/price strategies. No tariff plan, no pricing, no forecast preview
(those are FWHAI's domain — §1/§5).

### 6.1 Sidebar
- New **Schedule** nav item (calendar-clock icon) under PRIMARY, after Controls.
- A small badge when a schedule is **actively controlling** a target (e.g. a
  green dot / "1").

### 6.2 Header strip (reuses the existing tab header pattern)
- **Target selector** — Gateway ▸ Service ▸ Site (same selector the Controls/
  gateways UI already uses). The timeline + table below are scoped to it.
- Live status pills: target's run-status (**VPP Mode** when a schedule is live,
  distinct from the operating-mode pill), **Active now** when inside a window,
  and **N entries**.
- Buttons: **Add Entry**, **Validate**, **Enable/Disable all**.

### 6.3 Visual timeline (the centrepiece — like FWHAI's "Time-of-Use Visual Timeline")
- A 24 h horizontal bar for the selected day, segmented by the active entries'
  windows and **coloured by action** (not by tariff):
  - Force Charge → green · Force Discharge → orange · Force Standby → grey ·
    Self-Consumption → teal · Reserve change → blue.
  - **Gaps** render as a hatched **"native (TOU/mode)"** band so the user sees
    exactly when the aGate runs itself vs. when the schedule holds control —
    this is the visual expression of the §4a `release`-vs-`hold` decision.
- Hour ticks `00:00 … 24:00`, a live **"now" marker** line, and a legend keyed
  to the action colours.
- A **day-of-week** chip row (Mon–Sun) to preview a different day's windows
  (entries carry weekday masks).
- *(Stretch, SCH4)* a per-window hover card: action, target, power, release
  behaviour, next fire.

### 6.4 Entry table (like FWHAI's block table, bridge columns)
Columns: **START · END · DAYS · ACTION · TARGET · POWER · RELEASE · CONFLICT ·
ENABLED · NEXT FIRE · ⌫**.
- ACTION is a dropdown of the command vocabulary (Force Charge/Discharge/Standby,
  Self/TOU reserve, mode, Release) — the same list the Controls tab uses, so
  there is one source of truth for "what can we command".
- TARGET reuses the gateway/service/site selector; POWER reuses the W/% control.
- RELEASE = `release` | `hold` (per-entry override of the global default, §4a).
- CONFLICT = `defer` | `override` | `wait` (§4b).
- Inline enable toggle; **NEXT FIRE** computed server-side from the when-spec.
- A **Quick-Add row** at the bottom (Start / End / Action / Target / Add),
  mirroring FWHAI's "Quick Add Block".

### 6.5 Dashboard hook
- A small **"Scheduled: <action> until <time>"** indicator on the dashboard when
  a schedule is actively controlling a target — visually distinct from the
  FORCED (manual) badge so the user can tell *scheduled* from *forced* from
  *VPP-from-elsewhere*.

### 6.6 What we deliberately DON'T build (vs the FWHAI screenshot)
- No **Tariff Plan / Change Tariff / Pricing / $ prices** column — we dispatch
  actions, not price strategies.
- No **Energy Forecast Preview / Live Simulation** — no forecasting (§1).
- No **seasons / Manage seasons** in SCH2 (weekday masks only; seasons are a
  possible SCH4+ add if asked).
- **Presets** (Save/Load Preset) deferred to SCH4, and when added will mirror
  FWHAI's `SchedulePresets` JSON shape for future portability (§5).

---

## 7. Phasing
| Phase | What |
|---|---|
| SCH0 | ~~Hardware verification (VPP vs TOU)~~ **✅ resolved (owner): VPP overrides native TOU; release resumes it** |
| SCH1 | `schedules` table + REST CRUD + scheduler loop driving **one gateway** via the command handler, **including the configurable release/gap behaviour (§4a) + conflict policy (§4b)** |
| SCH2 | Schedule tab UI (list/add/edit, next-fire preview, override surfacing) |
| SCH3 | **Service/site targets** — fan-out via MP5 (depends on MP5) |
| SCH4 | TOU-window labels + simple per-window reserve/mode presets; optional context guards (§4c) |
| SCH5 | *(future)* commonality with FWHAI — shared schedule/preset schema; let FWHAI's HEMS drive the bridge schedule API |

**Live verification:** SCH1's logic ships behind unit tests (mocked controller),
but the *hardware* behaviour (VPP-overrides-TOU, release-resumes-TOU,
control-state-from-`WSetEna`) is verified against a real aGate per
[`scheduling-live-test-plan.md`](scheduling-live-test-plan.md). Ship gate for
SCH1 = LT-1, LT-2, LT-5, LT-7, LT-10, LT-11 pass on hardware.

## 8. Open questions
- **Default release/gap behaviour** (§4a): `release` (let native TOU fill the
  gaps) or `hold` (keep a 0 W VPP standby to suppress native)? Per-entry,
  per-schedule, or global?
- If `hold`, is a continuous `WSetPct=0` standby safe to leave indefinitely, and
  does it count against any (cosmetic) revert timer / keepalive?
- Entry conflict resolution: last-writer, explicit priority, or first-match?
- Without a trustworthy `LocRemCtl`, is `704.WSetEna` + setpoint-tracking
  enough to confirm control, or do we need an explicit verify-by-behaviour step?
- Does a `service` target need an all-or-nothing semantic (fail if any member
  rejects), or best-effort with per-gateway result reporting?
- Reuse the library `TOUSchedule` for TOU resolution, or keep time logic local?
- Where do schedules live relative to the bridge vs FWHAI long-term (avoid two
  schedulers fighting one aGate)?
