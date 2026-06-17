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
- A **pre-flight eligibility gate** that refuses to run on a target it can't
  cleanly control — e.g. stuck in native **TOU** (which schedules itself) when
  the mode can't be switched (SPAN lock). See §4a — this is the safety crux.

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

### 4a. Pre-flight eligibility & native-mode (TOU) interaction — critical

**The aGate's native modes are themselves schedulers.** Native **TOU** runs its
own internal time-of-use charge/discharge schedule; **Manual** is user-direct.
Our scheduler issuing VPP dispatch *on top of* one of these means two
schedulers fighting one battery. **Rule: our scheduler must own the operating
mode** before it controls a target — establish a cooperative baseline
(Self-Consumption or Emergency Backup) where our VPP dispatch is the sole
controller.

**Pre-flight eligibility gate (the "TOU ban").** When the scheduler is enabled,
and before each control action, *per target*:
1. Read the current operating mode (`15507`).
2. If it's a **self-scheduling native mode (TOU)** and an entry needs control,
   attempt to switch to the baseline mode (write `15507`) and **verify the
   read-back**.
3. If the switch **isn't honoured** (SPAN-locked / value unchanged), mark the
   gateway **scheduler-ineligible**, skip control, and surface a clear reason:
   *"Scheduler off for <gw>: cannot leave TOU (15507 write not honoured — SPAN
   lock)."* Re-check on the next tick / when SPAN unlocks.

> **⚠️ Hardware unknown that sets how strict this must be:** does an active VPP
> dispatch (`WSetEna=1` + `WSetPct`) **fully override** native TOU, or does TOU
> **still act**? If VPP wins outright, the mode-switch is belt-and-suspenders;
> if TOU still fights, the switch is **mandatory** and the ban is essential.
> **Unverified** — design for the strict case until proven (good candidate for
> the SPAN tester, issue #5).

### 4b. Conflict policy when a dispatch is already active
When an entry fires and the target already has an active dispatch (a manual
Force, or a prior entry) — configurable per-entry, global default:
- **`defer` (default)** — don't disrupt; skip this window, log "deferred —
  control already active." Manual Force always wins under `defer`.
- **`override`** — preempt and take control (logs `command_superseded`).
- **`wait`** — retry within the window until the conflicting control clears.

### 4c. Context preconditions (FWH extensions) — kept minimal
A *small, fixed* guard set — **not** a rule-engine:
- **Operating mode (`15507`)** — the mandatory eligibility gate (§4a).
- *Optional, opt-in:* skip charge-from-grid when **solar** (`15502` total /
  `15503` proximal / `15504`–`15505` remote) already covers **home load**
  (`15506` / `16000`); skip discharge below reserve.
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

---

## 6. UI
- A **Schedule** tab/card: list of entries (name, when, action, target,
  enabled), add/edit form reusing the command + service/phase selectors, an
  enable toggle, and a **"next fire"** preview per entry.
- Dashboard: a small "scheduled: <action> until <time>" indicator when a
  schedule is actively controlling a target (distinct from FORCED/manual).

---

## 7. Phasing
| Phase | What |
|---|---|
| SCH0 | **Hardware verification:** does an active VPP dispatch override native TOU, or does TOU still fight it? (§4a) — decides how strict the gate must be |
| SCH1 | `schedules` table + REST CRUD + scheduler loop driving **one gateway** via the command handler, **including the pre-flight eligibility gate (§4a) + conflict policy (§4b)** |
| SCH2 | Schedule tab UI (list/add/edit, next-fire preview, override + ineligible-reason surfacing) |
| SCH3 | **Service/site targets** — fan-out via MP5 (depends on MP5) |
| SCH4 | TOU-window labels + simple per-window reserve/mode presets; optional context guards (§4c) |
| SCH5 | *(future)* commonality with FWHAI — shared schedule/preset schema; let FWHAI's HEMS drive the bridge schedule API |

> Note: the pre-flight gate (§4a) ships **with SCH1**, not later — without it a
> schedule could silently fight native TOU.

## 8. Open questions
- **Does VPP dispatch override native TOU?** (the §4a unknown) — until verified,
  assume it doesn't and require the mode-switch + ban.
- Which **baseline mode** do we switch *to* when leaving TOU —
  Self-Consumption or Emergency Backup? (Affects what the battery does between
  scheduled dispatches.)
- Pre-flight: switch mode **per action**, or **once** on scheduler-enable and
  hold the baseline for the whole active window?
- Entry conflict resolution: last-writer, explicit priority, or first-match?
- On window **exit** with no successor: auto-Release to native (restoring the
  user's prior mode?), or hold?
- Does a `service` target need an all-or-nothing semantic (fail if any member
  rejects), or best-effort with per-gateway result reporting?
- Reuse the library `TOUSchedule` for TOU resolution, or keep time logic local?
- Where do schedules live relative to the bridge vs FWHAI long-term (avoid two
  schedulers fighting one aGate)?
