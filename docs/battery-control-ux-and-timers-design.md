# Battery Control — UX reset, active/inactive surfacing, and timer consolidation

**Status:** Backlog / design. No code yet.
**Origin:** User review of the Battery Control card + Release modal (2026-06).
**Scope:** Three related but separable workstreams around the battery dispatch
control surface and its timers. WS-A and WS-B are low-risk UX. WS-C is a
safety-critical architecture change gated behind hardware verification.

---

## Background

A user-commanded dispatch flows through three independent "timer" mechanisms:

1. **Hardware revert** — `WSetRvrtTms` (configured) + `WSetRvrtRem` (countdown),
   M704. *Documented cosmetic* on the aGate: the countdown runs but `WSetEna`/
   `WSetPct` are unchanged at expiry (PICS Issue 4; library observed 186 s
   post-expiry with no reversion).
2. **Library software timer** — `send_command(cmd, duration_s=…)` schedules its
   own reset (`cancel_command_timer()` + a timer that calls
   `reset_control_state()` after `duration_s`).
3. **Bridge software watchdog** — `CommandHandler._watchdog_loop`: enforces the
   duration timeout **and** target-SoC (charge-to-X% / discharge-to-X%), which
   the other two cannot do.

The control parameters (`_command_power_w`, `_command_power_pct`,
`_command_duration_s`, `_target_soc`) are staged server-side and surfaced to the
UI via `CommandHandler.virtual_points`. The dashboard's `controls_tab.js`
syncs the sliders/inputs from those points.

---

## WS-A — Reset control inputs on Release / expiry

### Problem
After a Release (or watchdog/target-SoC expiry) the command dropdown returns to
"Not Active", but **POWER (W) / POWER (%) / DURATION** keep their last values
(e.g. DURATION still shows 4m 0s with nothing active). The controls misrepresent
"this is what's running" when nothing is.

### Current behaviour (`command_handler.py::_release_command`)
On release it resets `_state` (action/power) and `_target_soc = 0`, but **does
not** reset `_command_power_w`, `_command_power_pct`, or `_command_duration_s`.
So `virtual_points` keeps emitting the stale staged values and the UI inputs hold.

### Proposal
On every release path (`reason in {release, shutdown, watchdog_expired,
target_soc_reached, superseded-source}`), zero the staged command parameters:

```
self._command_power_w = 0
self._command_power_pct = 0
self._command_duration_s = DEFAULT_WATCHDOG_S   # 0
# _target_soc already reset
```

The frontend already syncs from `virtual_points`, so the inputs fall back to 0
automatically — no JS change strictly required (verify `controls_tab.js`
`_syncFromPoints` applies the zeros and doesn't gate on truthiness).

### Decision needed
- **Reset to zero (recommended)** — controls always reflect the *active*
  dispatch; clean slate after release. Matches the user's mental model.
- **Preserve as a "preset"** — keep last power/duration so the user can re-issue
  the same dispatch quickly. If chosen, the card must visually mark these as
  *staged, not active* (overlaps WS-B).
- **Hybrid** — reset power, keep duration as a sticky user preference.

### Risk: low. Self-contained to `_release_command` + a quick UI sync check.

---

## WS-B — Surface active vs inactive on the Battery Control card

### Problem
The Battery Control card always renders editable inputs regardless of whether a
dispatch is active. The only active-state cues are the small "Active:" line and
the command log. The Release modal already does this well (amber when active,
emerald "No active control" when not, with live Elapsed/Watchdog).

### Proposal
Bring the modal's active/inactive treatment onto the card:
- When **inactive**: muted styling; inputs presented as *staged* (e.g. a
  "Staged command" caption); no live counters.
- When **active**: highlight the in-effect parameters (amber accent), show live
  **Elapsed** and **remaining** (software watchdog and/or hardware RvrtRem — see
  WS-C), and the active power/direction. Reuse `forcedDispatch` /
  `hasActiveCommand` / `releaseControlState` getters already in `app.js`.
- Consider a single shared partial/component so the card and the modal can't
  drift.

### Decision needed
- How much of the modal to inline vs. link out ("Details" → modal).
- Whether the inputs stay editable while a dispatch is active (re-issue) or lock
  until Release.

### Risk: low–medium. Pure frontend; no register/library changes.

---

## WS-C — Timer consolidation (remove redundant software timers?)

> The user's premise: *"the hardware timers are working — should the software
> ones (Release dialog + library) be removed?"*

### The contradiction to resolve first
The **deployed library still asserts the opposite**: `WSetRvrtTms` is cosmetic,
no hardware reversion observed, and the software `duration_s` timeout is "the
ONLY safety mechanism." Before removing *any* software safety we must
empirically establish current hardware behaviour — the firmware/library may have
changed, or the observation may be a misread (RvrtRem counting ≠ auto-revert).

### Verification step (prerequisite, do this first)
Issue a short, attended `Force Charge` with a small `duration_s` and **watch**:
1. Does `WSetRvrtRem` count down to 0?
2. **At expiry, does `WSetEna` auto-clear and power actually stop** (the real
   test — countdown alone is not reversion)?
3. Does this hold across charge *and* discharge, and across reconnects?
Record the result (and firmware version) in `docs/agate-reference.md` and the
vendor-issue catalog. This decides everything below.

### Analysis (regardless of the verification outcome)
- **Target-SoC enforcement is software-only.** Neither the hardware revert nor
  the library duration timer can stop at an SoC threshold. The bridge watchdog
  **must stay** for target-SoC. So "remove the software timer entirely" is off
  the table.
- **Two software duration timers are redundant.** The library's own
  `duration_s` timer and the bridge `_watchdog_loop` duration check both call a
  reset. This redundancy is worth resolving *independently* of the hardware
  question — pick one owner of duration timeout (recommend the bridge watchdog,
  since it already owns target-SoC and the audit logging; pass `duration_s=None`
  to the library, or keep it purely as a belt-and-suspenders backstop).
- **Orphan safety depends on a software timer.** Per
  `command_handler.py`, the crash-never-restart window is only closed if
  *something* auto-reverts. If hardware revert is verified working, that window
  finally closes in hardware — a real benefit, and an argument to *keep* a
  short hardware `WSetRvrtTms` as the orphan backstop while the bridge watchdog
  handles attended duration + SoC.

### Proposal (conditional on verification)
| Hardware revert verified… | Duration timeout | Target SoC | Orphan safety |
|---|---|---|---|
| **Working** | Hardware `WSetRvrtTms` (primary) + bridge watchdog (backstop) | Bridge watchdog (unchanged) | Hardware revert (closes crash-never-restart) |
| **Still cosmetic** | Bridge watchdog only; pass `duration_s=None` to library to drop the redundant library timer | Bridge watchdog | Bridge `_release_stale_commands` on restart (status quo) |

In **both** cases: collapse the duplicate software duration timer to a single
owner, and update the Release modal to show the timer that is actually
authoritative (hide/relabel the redundant one to avoid implying two independent
safeties).

### Release-modal display follow-on
- If hardware is authoritative: feature `RvrtRem` as the live countdown; demote
  `Watchdog` (software) to a secondary/backstop line or hide when 0.
- If software remains authoritative: keep `Watchdog`, and label `RvrtTms`/
  `RvrtRem` explicitly as "cosmetic (no auto-revert)" so it isn't mistaken for a
  safety.

### Coordination / constraints
- The software duration timer lives in the **`franklinwh-modbus` library**
  (`send_command` / `cancel_command_timer`). This repo is **read-only** here —
  any library change (dropping/relabelling its timer, updating the PICS Issue 4
  docstring) must be filed upstream, not edited locally.
- Update the **vendor-issue catalog** (PICS Issue 4 status) and
  `docs/agate-reference.md` with the verification result.

### Risk: **high** — safety-critical. Gated behind the verification step. Do not
remove any software safety on the user's recollection alone.

---

## Suggested sequencing
1. **WS-A** — reset staged inputs on release. Small, immediate UX win.
2. **WS-B** — active/inactive surfacing on the card (shared with the modal).
3. **WS-C verification** — the attended hardware-revert test. *Findings-first;*
   only then schedule the consolidation, and split the library change upstream.

## Open questions
- Re-issue while active, or lock inputs until Release? (WS-B)
- Keep duration as a sticky preset, or always zero it? (WS-A)
- If hardware revert works, what's the right `WSetRvrtTms` orphan-backstop value
  (short enough to bound an orphan, long enough not to cut attended dispatches)?
