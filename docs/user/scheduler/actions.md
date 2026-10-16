# Actions

The action is what an entry sends to the gateway when it fires. Each entry has exactly one battery action (or *none*) plus any number of Home Assistant actions.

![The Action list](../../images/scheduler/action-dropdown.png)

## Summary

| Action (UI) | API `action` | Parameters | Kind | What the bridge writes |
|---|---|---|---|---|
| Force Charge | `force_charge` | Power (W) | sustained | a WSet charge setpoint (Model 704), plus the revert timer |
| Force Discharge | `force_discharge` | Power (W) | sustained | a WSet discharge setpoint, plus the revert timer |
| Force Standby | `force_standby` | — | sustained | a 0 W setpoint: the battery neither charges nor discharges |
| Release | `release` | — | one-shot | clears the setpoint and the revert timer: the gateway returns to its own mode |
| Self Reserve % | `reserve_self` | Reserve % | one-shot | Self-Consumption reserve (register 15508) |
| TOU Reserve % | `reserve_tou` | Reserve % | one-shot | TOU reserve (register 15509) |
| Operating Mode | `mode` | Mode | one-shot | operating mode (register 15507) |
| No battery action (HA only) | `none` | — | none | nothing; only the entry's HA actions run |

- **Sustained** actions hold the battery for the length of the window. The bridge supervises them and releases them when the run ends (see [Exit, release and conflicts](exit-and-conflicts.md)).
- **One-shot** actions are sent once each time the entry becomes active, and not again until the next window. Nothing is undone when the window ends.

## Force Charge and Force Discharge

![Power field](../../images/scheduler/action-force.png)

### Power

| Power field | Result |
|---|---|
| a number, in **watts** | that power, capped at the gateway's trusted battery rating for that direction |
| empty | the gateway's full **trusted** rating for that direction |

- Power is in **watts only**. Percentage power was removed in 0.2.8. The API rejects `params.power_pct` with *"params.power_pct has been removed: set params.power_w in watts"*.
- An entry saved before 0.2.8 with a percentage still runs at that share of the trusted rating. The form shows *"This entry was saved as N % of max …"* until you enter watts and save. If you save it with Power empty, it runs at full power from then on.

!!! danger "Force commands are refused until the battery rating is trusted"
    The bridge sizes every force command against a **battery power rating**. A rating is *trusted* only when it is backed by evidence:

    - **calibrated** — you ran a full-rate dispatch from the FranklinWH app and the bridge recorded the peak, or
    - **m702_confirmed** — the aGate's Model 702 rating, and the battery has been seen running at 80–115 % of it.

    Until then Force Charge and Force Discharge are **refused**: nothing is written to the aGate and the control log records *"Refused Force Charge: … Calibrate it: Settings → Site & Gateways → this gateway → Battery power rating"*. Each direction is judged on its own. A rating also stops being trusted when the battery is seen running well above it, until you calibrate again. Mock gateways are always trusted.

    Force Standby is never refused: it asks for 0 W.

### What is written

1. Optional staging: duration and target SoC if the entry's params carry them, then the power.
2. The setpoint (WSetEna = 1, WSetPct) in Model 704. The aGate enters remote-control (VPP) mode.
3. The revert timer (WSetRvrtTms): the duration in seconds, or 7200 s for an open-ended command. It keeps the FranklinWH app from overriding the dispatch. The aGate does **not** release by itself when it counts down; the bridge does the releasing.
4. A read-back. If WSetEna is not 1 afterwards, the send is treated as failed.

### API-only parameters

The form sets Power only. Through the REST API, `params` may also carry:

| Param | Effect |
|---|---|
| `power_w` | watts, as above |
| `duration_s` | a time limit (0–7200 s) after which the bridge releases the command itself |
| `target_soc` | 0–100. Charge stops at or above it, discharge at or below it (checked every 5 s). A target already reached when the command starts is cleared. |

These are staged on the gateway's command handler before the force command. Fire-based entries already end at *fire time + duration*, so you rarely need `duration_s` in params.

!!! warning "Staged values can carry over"
    The command handler keeps the last staged power and time limit until something else is staged. A schedule entry with **empty Power** stages no power, so it runs at whatever was staged last (for example by the Controls tab or the HA *Command Power* entity), not necessarily the full rating. Likewise a time limit staged by a manual command can end a scheduled run early (the Activity Log then shows *expired*). Setting Power explicitly avoids the first case.

## Force Standby

Holds the battery at 0 W. The house runs from solar and grid; the battery neither charges nor discharges. Useful to keep charge for later without changing the gateway's mode.

## Release

Sends a release: the setpoint and the revert timer are cleared and the gateway returns to its own mode. It is a one-shot. It is **not** subject to the conflict policy: it releases a manual command too.

## Reserve and Operating Mode

=== "Reserve %"

    ![Reserve % field](../../images/scheduler/action-reserve.png)

    | Param | Values |
    |---|---|
    | Reserve % (`pct`) | 0–100 |

=== "Operating Mode"

    ![Mode field](../../images/scheduler/action-mode.png)

    | Param | Values |
    |---|---|
    | Mode (`mode`) | Self-Consumption, TOU, Emergency Backup (`Time of Use` is accepted as an alias for TOU) |

!!! warning "Many aGates ignore mode and reserve writes"
    Reserve and mode live in FranklinWH extension registers (15507–15509). On every real aGate tested, reserve writes are acknowledged but the value does not change, and some gateways also ignore mode writes. TOU reserve (15509) mirrors Self-Consumption reserve (15508). The scheduler logs these actions as *ok* when they were sent; check **Logs** for the real result (*"The aGate acknowledged the write but the register did not change"*). Prefer force commands where you can.

## No battery action (HA only)

The entry sends nothing to the battery and only runs its HA actions. Such entries never compete with battery entries for a gateway, and the conflict policy and release policy do not apply.

## Home Assistant actions

![HA Actions with no Home Assistant connected](../../images/scheduler/ha-actions-empty.png)

HA actions are one-shot calls that run alongside the battery action. **+ add action** appears once at least one controllable entity is exposed on the **Home Assistant** tab; otherwise the block says *"No controllable HA entities exposed."*

| Field | Values | Notes |
|---|---|---|
| Kind | **Entity** or **Notify** | |
| Entity | an exposed entity in a controllable domain | switch, input_boolean, light, select, input_select, number, input_number, button, scene, script |
| Service (Entity) | depends on the domain | switch / input_boolean / light: Turn On, Turn Off, Toggle. select / input_select: `select_option` + an option. number / input_number: `set_value` + a value. button: `press`. scene / script: `turn_on`. |
| Notify target | a `notify.*` service of an HA instance | listed separately: it is a service, not an entity |
| Title, Message (Notify) | text, up to 200 / 1000 characters | Message is required |
| When | **on fire**, **on exit**, **entry & exit** | |
| Guard (**+ guard**) | sensor, operator, value | the action runs only if this condition is true at that moment |

### When they run

| When | Runs |
|---|---|
| on fire | once when the entry fires, **after** the battery action is accepted. For an HA-only entry, when it becomes active (and its gate passes). |
| on exit | once when the run ends: the window closes, an exit condition is met, the command ends in-window (time limit, target SoC, external release) or you press Stop. |
| entry & exit | both. |

Each action runs once per activation, not every tick. If the battery action fails, fire-time HA actions wait for the successful retry.

### Placeholders in notifications

Title and message can quote live values with `%sensor.id%`, using the same ids as conditions:

```text
SoC is %battery.soc_pct%%, mode %mode.name%, reserve %battery.reserve_pct%%
```

- Numbers print without a trailing `.0` (63.0 → 63).
- An unknown sensor or a sensor without a value prints `?`; the notification is still sent.
- Only two-part ids work (`group.name`). Ids with two dots, such as `energy.grid_import.today_kwh`, and `ha:` ids are not substituted.

### Failure alerts

If a force command fails, is not effective, or recovers after failing, the bridge sends a message titled *FranklinWH bridge* to **every notify target on that entry**. An entry with no notify action gets no alert; the Activity Log records *skipped — no notify target on this entry*.

### What is logged

Every HA action writes an Activity Log row: *ha_action* (`[fire] switch.pool_pump → turn_off`, `[exit] notify.mobile_app: …`) on success, *failed* with the error, or *gated* when its guard was false. With no Home Assistant configured at all, a single *failed* row says the actions were skipped.

## Audit trail

| Where | What |
|---|---|
| **Schedule → Activity Log** | the scheduler's decisions: dispatched, gated, skipped, waiting, deferred, released, verified, failed, HA actions, config changes. See the [result list](exit-and-conflicts.md#activity-log-results). |
| **Logs → control log** | every command the gateway's command handler sent or refused, with the requester `schedule:<entry id>`, the hardware state before and after, and the release reason. |
| Dispatch outcomes | each force command is tracked as sent → verified / not effective / superseded / released / failed. |

## Dependencies by action

| Action | Needs |
|---|---|
| Force Charge / Discharge | a running aGate with a **trusted** rating in that direction. A MAC-1 collar has no battery to command; `gateway.battery_capable` reads false for one, so you can gate on it. |
| Force Standby, Release | a running gateway |
| Reserve %, Operating Mode | a running gateway; the aGate must accept extension-register writes (many do not) |
| HA actions | a Home Assistant instance, the entity exposed on the Home Assistant tab; for notifications a `notify.*` service |
| None | nothing |
