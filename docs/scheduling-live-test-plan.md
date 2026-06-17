# Scheduler — Live (On-Hardware) Test Plan

**Companion to** `scheduling-and-orchestration-design.md`. Unit tests (mocked
controller) cover the *logic* — window evaluation, winner selection, conflict
policy, release/gap behaviour, idempotency. They **cannot** confirm the one
thing that actually matters on an aGate: **that a scheduled VPP dispatch
overrides native TOU and that releasing hands control back cleanly.** That is
what this plan verifies.

## Run log

**2026-06-17 — aGate X (`10060006A02F00000001`, V10R01B04D00), no SPAN unlock.**
Core ship gate **PASSED**: LT-1 ✓ (battery flipped +1500 W discharge → −1000 W
charge while `mode` stayed TOU), LT-2 ✓ (release resumed TOU in ~8 s), LT-5 ✓
(`LocRemCtl` read "Local" throughout dispatch), LT-7 ✓ (manual Force Standby
superseded the schedule; engine logged `deferred`), LT-10 ✓ (watchdog released
at 60 s, no re-fire), LT-11 ✓ (graceful restart released the dispatch + schedule
persisted). Three bugs found & fixed: tick race (double-dispatch),
watchdog-expiry re-fire within window, and a flaky-gateway-start aborting the
engine/health-checker on restart. Battery stayed on TOU safely throughout.

## Status & prerequisites

- **Gated.** Runs only against a real aGate. CI never runs these
  (`pytest -m hardware` only; default `-m "not hardware"` skips them).
- **Operator present.** Every case writes a live battery setpoint. An operator
  must watch the battery and be ready to hit **Release** / pull the schedule.
- **SPAN unlock NOT required for the core path** (§4a: M704 `WSetEna`/`WSetPct`
  is unrestricted). SPAN *is* required only for the reserve/operating-mode
  context cases (CTX-*) — those are skipped if `15xxx` writes return SPAN-locked.
- **Community tester** engaged via [issue #5](https://github.com/david2069/franklinwh-modbus/issues/5)
  has the SPAN-unlocked rig; the core cases run on any aGate.
- Record firmware + serial at the top of each run (template: aGate X,
  `V10R01B04D00`, `10060006A02F00000001`).

## How to read a case

Each case is **Pre → Do → Expect → Pass/Fail**. "Observe" rows say *where* to
look: bridge dashboard run-status, `control_log` audit rows, the FranklinWH
mobile app, and a clamp meter on a battery leg if available.

---

## A. Core dispatch — VPP overrides TOU (no SPAN needed)

### LT-1 — A scheduled window takes control from native TOU
- **Pre:** aGate in **TOU** mode (set in the mobile app), idle or following its
  own TOU plan. Bridge running, gateway online. No active manual Force.
- **Do:** Create a schedule entry: window = *now → now+10 min*, action =
  **Force Charge** @ 1000 W, target = this gateway, enabled. Wait for the
  minute-boundary tick.
- **Expect:**
  - Battery begins charging ~1000 W within one tick (≤60 s).
  - Bridge run-status shows **VPP Mode** (derived from `704.WSetEna=1`), distinct
    from the operating-mode line which still reads **TOU**.
  - `control_log` has a `command_sent` row, `action="Force Charge"`, with a
    `schedule_id` / `source=schedule` marker.
  - Mobile app reflects VPP / dispatch (cloud `run_status=9` per FWHAI docs).
- **Pass:** battery follows the commanded setpoint despite TOU being the mode.

### LT-2 — Window exit releases, native TOU resumes (`release` gap default)
- **Pre:** LT-1 active, gap behaviour = **`release`**.
- **Do:** Let the window expire (or shorten it to end in ~1 min).
- **Expect:**
  - On the boundary the scheduler issues a Release: `WSetEna→0`, revert timer
    zeroed; run-status returns to **TOU** within a tick.
  - Battery resumes whatever native TOU dictates (idle / self-consumption per
    its plan) — **not** stuck at the commanded setpoint.
  - `control_log` has `command_released`, `reason=window_exit`.
- **Pass:** no residual VPP setpoint; native schedule visibly back in control.

### LT-3 — `hold` gap keeps native suppressed across a gap
- **Pre:** Two windows with a deliberate gap between them; gap behaviour =
  **`hold`**.
- **Do:** Observe the gap period.
- **Expect:** during the gap the bridge holds `WSetEna=1, WSetPct=0` (standby) —
  run-status stays **VPP Mode**, battery sits at ~0 W, native TOU does **not**
  resume until the schedule is fully released/disabled.
- **Pass:** native mode stays suppressed for the whole gap; battery near 0 W.
- **Watch (open question §8):** confirm a continuous `WSetPct=0` hold is safe to
  leave for hours and the (cosmetic) revert timer doesn't trip anything.

### LT-4 — Force Discharge & Force Standby windows
- Repeat LT-1 with **Force Discharge** @ 1000 W (battery exports / supplies
  home load) and **Force Standby** (0 W). Confirm sign and run-status for each.

---

## B. Control-state detection (LocRemCtl is unusable)

### LT-5 — `WSetEna` is the only trustworthy "we have control" signal
- **Pre:** LT-1 active (VPP dispatch live).
- **Do:** Read `715.LocRemCtl` and `704.WSetEna` (Explorer or `/api/points`).
- **Expect:** `LocRemCtl` reads **"Local"** *even though* we are actively
  controlling (confirmed vendor defect); `WSetEna=1`. The bridge's "control
  confirmed" indicator must derive from `WSetEna` **+** setpoint-tracking, never
  from `LocRemCtl`.
- **Pass:** dashboard shows "control confirmed" while `LocRemCtl=Local`.

### LT-6 — Behavioural confirmation (setpoint tracking)
- **Do:** Command Force Charge @ 1500 W; compare commanded vs measured
  `battery_power_w` after settle (~10 s).
- **Expect:** measured tracks commanded within the battery's ramp tolerance.
- **Pass:** bridge marks the dispatch "tracking ✓"; a non-tracking dispatch
  (e.g. SoC at 100 % so charge can't proceed) is flagged, not silently "ok".

---

## C. Conflict & override (no hardware-risk if A passed)

### LT-7 — Manual Force supersedes the schedule (`defer` default)
- **Pre:** A schedule window active (Force Charge). Global conflict policy =
  **`defer`**.
- **Do:** From Controls, issue a manual **Force Discharge**.
- **Expect:** manual wins; `control_log` gets `command_superseded`
  (action="Force Charge", superseded by manual Force Discharge); the schedule
  shows **deferred** and does **not** fight back until the next boundary.
- **Pass:** battery follows the manual command; schedule paused, not thrashing.

### LT-8 — `override` policy preempts an active dispatch
- Repeat LT-7 with the entry's conflict policy = **`override`**: the entry
  preempts the manual Force at its boundary and logs the supersede the other way.

### LT-9 — Two entries, one target → single winner
- **Pre:** Two enabled entries whose windows overlap on the same gateway.
- **Expect:** exactly one dispatch at a time (last-writer / declared priority —
  whichever §8 resolves to); no oscillation between the two each tick.
- **Pass:** one steady setpoint through the overlap; the audit log shows one
  winner, not alternating sends.

---

## D. Safety nets

### LT-10 — Software watchdog still releases a scheduled dispatch
- **Pre:** A scheduled Force Charge with a short watchdog (e.g. 2 min) but a
  longer window.
- **Expect:** watchdog fires and releases at 2 min even though the window is
  still open; `control_log` `command_released`, `reason=watchdog_expired`. (The
  hardware revert timer is cosmetic — PICS Issue 4 — so the SW watchdog is the
  real net, same as manual dispatch.)
- **Pass:** dispatch ends at the watchdog, not the window.

### LT-11 — Bridge restart mid-window
- **Pre:** A scheduled dispatch active.
- **Do:** Restart the bridge (container stop/start).
- **Expect:** startup `_release_stale_commands` clears the orphaned VPP
  setpoint; the scheduler then re-evaluates and, if the window is still open,
  re-issues the dispatch cleanly. No double-control, no stuck VPP.
- **Pass:** at most one tick of gap; state consistent after restart.

### LT-12 — SPAN-locked write degrades gracefully
- **Pre:** aGate **without** SPAN unlock; an entry whose action needs a `15xxx`
  write (e.g. `reserve_self`).
- **Expect:** the write fails with a SPAN-locked result, logged ✗; the scheduler
  does not crash or retry-storm; M704 power actions in the same plan still work.
- **Pass:** clear ✗ in the log + UI; no loop wedged.

---

## E. Context guards (SPAN required — skip if locked) — §4c

### CTX-1 — Skip grid-charge when solar already covers home load
- **Pre:** Daytime, solar (`15502`) > home load (`16000`); an entry =
  Force Charge-from-grid with the "skip-if-solar-covers-load" guard on.
- **Expect:** the entry is **skipped** for that tick with a logged reason; it
  fires normally once solar drops below load.

### CTX-2 — Skip discharge below reserve / floor SoC
- **Pre:** SoC at/below the configured floor; a Force Discharge entry with the
  floor guard on.
- **Expect:** discharge skipped, logged; resumes when SoC recovers.

---

## F. Multi-aGate (SCH3 / MP5 — only once that lands)

### LT-13 — Service/site target fans out
- **Pre:** ≥2 gateways linked to one service (MP1–MP4 done), MP5 executor live.
- **Do:** A schedule entry targeting the **service**.
- **Expect:** every member gateway takes the dispatch; release happens together;
  per-gateway results reported; a partial failure surfaces per §8's all-or-
  nothing-vs-best-effort decision.

---

## Sign-off matrix

| Case | What it proves | Gate |
|------|----------------|------|
| LT-1/2 | VPP overrides TOU; release resumes TOU | **core — must pass before ship** |
| LT-3 | `hold` suppresses native across a gap | core |
| LT-4 | Discharge/Standby sign + status | core |
| LT-5/6 | Control-state from `WSetEna`, not `LocRemCtl` | core |
| LT-7/8/9 | Conflict policy + single winner | core |
| LT-10/11/12 | Watchdog, restart, SPAN-lock safety | core |
| CTX-1/2 | Context guards | SPAN-only |
| LT-13 | Multi-aGate fan-out | SCH3/MP5 |

**Ship gate for SCH1:** LT-1, LT-2, LT-5, LT-7, LT-10, LT-11 all pass on a real
aGate. The rest are required before enabling `hold`, context guards, or
multi-aGate targets respectively.
