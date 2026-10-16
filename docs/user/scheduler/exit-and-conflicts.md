# Exit, release and conflicts

This page covers how a run ends, what the gateway does afterwards, what happens when two entries (or you and an entry) want the same gateway, and how the bridge checks that a command actually took.

## How a run ends

A force command started by an entry ends at the **first** of these:

| End | How it is logged | Fires again… |
|---|---|---|
| **Window ends** (recurring/one-off window end, or fire time + Duration (min)) | *ok* — "window exit — released to native" | at the next window |
| **Exit conditions** become true | *exit_condition_met*, with the failing/passing values | at the next window, not in this one |
| **Target SoC** reached (API `target_soc`) | *expired* — "dispatch ended in-window" | at the next window |
| **Time limit** (API `duration_s`, or a carried-over staged limit) | *expired* | at the next window |
| **Released elsewhere**: the Release button, the Controls tab, MQTT, the overshoot guard | *expired* | at the next window |
| **Stop** (■) on the entry | *stopped* | at the next window |
| Entry **disabled** or **deleted** | *ok* — released | when enabled again |
| **Bridge shuts down** | control log: release, reason *shutdown* | after restart, if the window is still open |

"At the next window" means the entry is marked done for the current window and will not re-fire until its window closes and opens again. For an **Always** entry the window never closes, so it does not fire again until the bridge restarts (see [Triggers](triggers.md#always-evaluate)).

![Exit conditions block](../../images/scheduler/exit-conditions.png)

Exit conditions:

- are checked every tick, only while this entry owns a running **force** command;
- use the same sensors, operators, ALL/ANY and groups as [entry conditions](conditions.md);
- end the run as soon as they are true, then apply the release policy;
- are not checked for Release, Reserve, Mode or HA-only entries, nor for **Run now** or a **late fire** (those are not owned by the scheduler).

Typical exits: `battery.soc_pct >= const.max_charge_soc` for a charge, `battery.soc_pct <= const.min_discharge_soc` for a discharge, `tariff.spot_price >= 8` for a price rule.

## Release policy

![On exit / release](../../images/scheduler/release-policy.png)

| Option (UI) | API `release_policy` | When the run ends |
|---|---|---|
| release → native | `release` (default) | The setpoint is cleared; the gateway goes back to whatever mode it is in. |
| release, log any mode change | `restore_prior_mode` | Same release. The bridge also compares the mode now with the mode when the run started and writes the result to the Activity Log ("mode unchanged (TOU)" or "mode not restored: changed during dispatch (TOU → Self-Consumption), left as-is"). It never writes the old mode back. |
| *(API only)* | `set_operating_mode:<Mode>` | Release, then set that mode (for example `set_operating_mode:TOU`). Skipped if the mode is already in place or the gateway is known to ignore mode writes; the reason is logged. |

The release policy applies to entries with a trigger type or with conditions. A plain window entry with no conditions always uses a plain release.

### Hold instead of release (API only)

The API field `release` takes `release` (default) or `hold`. With `hold`, when the window of a force command ends the bridge does **not** hand back: it switches to Force Standby (0 W) and keeps the gateway in remote-control mode, so its own TOU or Self-Consumption logic stays suppressed. The Activity Log says *"window exit — holding native suppressed"*. The hold lasts until another entry takes the gateway or you release it. The form has no control for this; it keeps whatever value the entry has.

## Priority and competing entries

A gateway runs one battery command at a time. When several **battery** entries are active for the same gateway at once:

1. **The running entry keeps the gateway** for the rest of its run, even if its gate turns false.
2. Otherwise, the **highest priority** entry whose **entry conditions pass** wins. Ties go to the **oldest** entry (created first).
3. If no entry's gate passes, the top-ranked entry logs its own *gated* row.

The losers are logged once per contest as *skipped* — "gateway held by '<name>' this tick". Priority is 0–1000, default 0.

HA-only entries (action *No battery action*) are never part of this contest.

## Conflict policy: manual control

![If already controlled](../../images/scheduler/conflict-policy.png)

"Already controlled" means a force command is active on the gateway that this scheduler did not start: one sent from the Controls tab, the top-bar buttons, MQTT/Home Assistant entities or the REST API, or a **Run now**.

| Option (UI) | API `conflict` | Behaviour |
|---|---|---|
| defer (skip) | `defer` (default) | Leave the manual command alone. Logged once as *deferred* — "target under manual control (Force Charge)". The entry takes over when the manual command ends, if its window is still open. |
| override | `override` | Replace the manual command with this entry's action. The control log records the manual command as *superseded*. |
| wait | `wait` | Currently the same as defer. |

Only force actions honour the conflict policy. A **Release** entry releases a manual command regardless. A setpoint written by another tool directly to the aGate (not through this bridge) is not seen as manual control here; it is logged in the control log as an *external dispatch*.

## Targets

![Target](../../images/scheduler/target.png)

| Target (UI) | API `target_type` / `target_id` | Sends to |
|---|---|---|
| Gateway | `gateway` / the gateway id | that gateway |
| Site (all gateways) | `site` / none | every **running** gateway, mock gateways included |
| Service | `service` / the service id | every running gateway linked to that utility service |

For Site and Service, each member gateway is handled separately: its own conditions snapshot, its own running state, its own release, its own Activity Log rows (the Target column names the gateway). A gateway that joins or leaves the group is picked up on the next tick; one that leaves while running is released.

A mock gateway target shows a warning in the form: its commands change simulated data only.

## Run now and Stop

| Button | What it does |
|---|---|
| ▶ **Run now** | Sends the entry's action immediately, whatever the time, after checking the entry conditions (the REST API can skip the check with `?force=true`). It uses the entry's Duration (min) as a time limit. The scheduler does **not** own this command: exit conditions, the window end and the release policy do not apply. A window entry has no duration, so a Run now force command runs until you release it. The Activity Log says *executed*, or *gated* if the conditions failed. |
| ■ **Stop** | Shown while the entry is active. Releases the run it owns (with its release policy), runs exit-time HA actions, logs *stopped*, and blocks the entry for the rest of this window. The entry stays enabled. Disable it to stop it permanently. |

## Bridge restart

| Step | What happens |
|---|---|
| Graceful shutdown | Every active force command is released (fast release, no ramp-down). |
| Crash or power loss | The setpoint stays on the aGate. The aGate does not revert on its own. |
| Start-up | Each gateway checks its saved command state; if a command was active it is **released** "for safety". |
| First ticks | The scheduler starts with no memory of past runs. Entries whose window is still open fire again (gate and hold-for permitting; the hold-for timer starts again). Always entries are eligible again. |
| Catch-up | If the bridge was down for more than 60 s, fires that started and ended while it was down are handled by each entry's [missed policy](triggers.md#after-an-outage-or-restart-catch-up). |

## Dispatch verification and re-send

After every force command the scheduler checks that the aGate is doing what it was asked, using fresh polled values only (not older than 45 s):

| Check | When | If it fails |
|---|---|---|
| **Send accepted** | immediately | Logged *failed* — "dispatch failed: …, retrying in 15s". The entry retries after 15 s, 30 s, then every 60 s while its window is open. The first failure and a later recovery are sent to the entry's notify targets. If the window closes without one successful send: *failed* — "window ended without a successful dispatch". |
| **Setpoint still enabled** (WSetEna = 1) | every tick | The setpoint is re-sent and logged *reasserted* (up to 3 times per window). After that: *failed* — "setpoint lost 3 times — not re-sending again this window", plus a notification. |
| **Battery is moving** | 90 s after the send | The battery must run in the commanded direction at at least max(100 W, 20 % of the request). Standby must be within 200 W of zero. Pass: *verified* — "battery at 2950 W (requested 3000 W)". Fail: *not_effective* and one re-send; if it still fails, *not_effective* "even after a re-send" plus a notification. |
| **At a SoC limit** | at the 90 s check | Charging at ≥ 98 % SoC, or discharging within 3 % of the reserve, is not judged: *unverifiable*. |

## Overshoot guard

The command handler watches each force command. If, 20 s or more after the start, the battery runs at more than **1.25 × the request + 500 W**, the rating it was sized against must be wrong. The bridge then:

1. releases the command (reason *overshoot*),
2. marks that direction's battery rating as contradicted, and
3. refuses further force commands in that direction until you calibrate the rating again.

For the scheduler this shows as *expired* — "dispatch ended in-window" — and the control log shows the overshoot.

## Activity Log results

| Result | Meaning |
|---|---|
| created, updated, enabled, disabled, deleted | a config change (the detail lists changed fields) |
| ok | a dispatch ("dispatched <name> — Force Discharge @ 3000W") or a release/hold at window exit |
| gated | entry conditions failed (the detail lists the failing rows with live values); also an HA action whose guard failed, or Run now blocked by conditions |
| waiting | conditions met, hold-for timer running |
| skipped | another entry holds the gateway; or a failure alert with no notify target |
| deferred | the gateway is under manual control and the conflict policy is defer/wait |
| exit_condition_met | exit conditions ended the run |
| expired | the command ended inside the window (target SoC, time limit, external release, overshoot) |
| stopped | you pressed Stop |
| executed | Run now |
| missed, late_fired | catch-up after an outage or restart |
| verified, unverifiable, not_effective, reasserted | dispatch verification |
| failed | a send failed, an HA action failed, or verification gave up |
| ha_action | an HA action or notification succeeded |

The filter buttons above the log cover *fired*, *executed*, *ha action*, *gated*, *waiting*, *missed* and *exit condition met*. Dispatches are recorded as *ok*, so the *fired* filter shows nothing; clear the filter to see them.
