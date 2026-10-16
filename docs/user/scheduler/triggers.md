# Triggers

The trigger decides when an entry is **active**. Conditions and actions only matter while it is active.

![The Trigger Type list](../../images/scheduler/trigger-type-dropdown.png)

There are two families:

| Family | Trigger types | Active while |
|---|---|---|
| **Windows** | Recurring windows, One-off date window | the clock is inside a start → end window you type |
| **Fire-based** (shown with an **auto** badge) | Daily at time, Weekly at time, Every N minutes, Monthly / calendar, Custom cron, Always evaluate | from each *fire time* until fire time + **Duration (min)** |

## Summary

| Trigger Type (UI) | API `trigger_kind` | Fields | Active window |
|---|---|---|---|
| Recurring windows | *(none)* — `when_spec` | Days, one or more time windows | each window, on the ticked days |
| One-off date window | *(none)* — `when_spec.date` | Date, one or more time windows | each window, on that date only |
| Daily at time | `daily` | Fire at, Duration (min) | fire time → + duration, every day |
| Weekly at time | `weekly` | Days, Fire at, Duration (min) | fire time → + duration, on the ticked days |
| Every N minutes | `interval` | Every (min), Anchor, Duration (min) | each fire → + duration |
| Monthly / calendar | `monthly` | Day of month, Fire at, Duration (min), Months | fire time → + duration |
| Custom cron | `cron` | Cron expression, Duration (min) | each cron match → + duration |
| Always evaluate (sensor-driven) | `always` | — | always |
| *(API only)* | `oneoff` | `fire_at` (ISO date-time) | fire time → + duration, once |

!!! note "Duration (min) = 0 means one minute"
    For every fire-based trigger except Always, a duration of 0 (or empty) gives a **one-minute** active window. That is enough for a one-shot action (a mode, a reserve, a notification). For a force command, set a duration: when the window ends the bridge releases the battery, and an exit condition can only end a run *earlier*, never later.

!!! note "Duration (min) is not the same as Duration (HH:MM:SS)"
    *Duration (min)* sits with the trigger and sets how long the entry stays active. *Duration (HH:MM:SS)* sits under the entry conditions and is the [hold-for (dwell) time](conditions.md#hold-for-dwell).

## Recurring windows

![Recurring windows fields](../../images/scheduler/trigger-window.png)

| Field | Values | Notes |
|---|---|---|
| Days | Mon … Sun, any combination | None ticked = every day. |
| Time windows | start → end, `HH:MM` | **+ add window** for more. ✕ removes a row (at least one must stay). |

- The entry is active from *start* up to, but not including, *end*.
- **End earlier than start wraps past midnight.** `22:00 → 06:00` runs overnight; the form says *wraps past midnight*. The day check uses the day the clock is on now, so for an overnight window on selected days, the part after midnight belongs to the next day's tick. Tick both days if you need both halves.
- **Start equal to end matches nothing.**
- Resolution is one minute.

## One-off date window

![One-off date window fields](../../images/scheduler/trigger-once.png)

| Field | Values | Notes |
|---|---|---|
| Date | a calendar date | Defaults to the bridge's today. |
| Time windows | start → end | Same rules as recurring windows. |

The entry fires only on that date and is dormant afterwards. The list shows *Once (passed)* once the date has gone. The day picker is hidden; the date replaces it.

## Daily at time

![Daily fields](../../images/scheduler/trigger-daily.png)

| Field | Values |
|---|---|
| Fire at | `HH:MM` |
| Duration (min) | 0 or more |

## Weekly at time

![Weekly fields](../../images/scheduler/trigger-weekly.png)

| Field | Values | Notes |
|---|---|---|
| Days | Mon … Sun | **At least one day is required**: a weekly trigger with no days never fires. |
| Fire at | `HH:MM` | |
| Duration (min) | 0 or more | |

## Every N minutes

![Interval fields](../../images/scheduler/trigger-interval.png)

| Field | Values | Notes |
|---|---|---|
| Every (min) | 1 or more | Stored as seconds (`every_seconds`). |
| Anchor | `HH:MM`, optional | Fires are counted from this time each day. Empty = midnight. |
| Duration (min) | 0 or more | |

Fires land on *anchor + k × N*. With N = 30 and no anchor: 00:00, 00:30, 01:00 … Across a daylight-saving change the step can drift by the change for that one step.

## Monthly / calendar

![Monthly fields](../../images/scheduler/trigger-monthly.png)

| Field | Values | Notes |
|---|---|---|
| Day of month | 1–31 | Clamped to the month's length: 31 fires on 30 April and 28/29 February. |
| Fire at | `HH:MM` | Empty = 00:00. |
| Duration (min) | 0 or more | |
| Months | Jan … Dec | None ticked = every month. |

The quick presets use this trigger: *Every month (1st day)*, *Quarterly* (Jan, Apr, Jul, Oct), *Six monthly* (Jan, Jul) and *Annually* (Jan 1st).

The bridge rejects a day outside 1–31 or a month outside 1–12 when you save.

## Custom cron

![Cron fields](../../images/scheduler/trigger-cron.png)

| Field | Values |
|---|---|
| Cron expression | five fields: minute, hour, day of month, month, day of week |
| Duration (min) | 0 or more |

Examples:

| Expression | Fires |
|---|---|
| `*/15 * * * *` | every 15 minutes |
| `0 17 * * 1-5` | 17:00 Monday–Friday (cron counts Sunday as 0) |
| `*/30 6-20 * * *` | every half hour from 06:00 to 20:30 |
| `0 0 1 * *` | midnight on the 1st of each month |

The bridge checks the expression when you save and refuses an invalid one. Cron entries are not drawn on the visual timeline.

## Always evaluate

![Always evaluate](../../images/scheduler/trigger-always.png)

The entry is active all the time; only its entry conditions decide whether it dispatches.

!!! warning "Always fires once per bridge run, and does not release when the gate turns false"
    The form says an Always entry "releases when [its conditions] stop". The engine does not do that:

    - Once dispatched, a force command keeps running when the entry conditions become false. Only an exit condition, a target SoC, a time limit or Stop ends it.
    - After a run ends (exit condition, Stop, a time limit), the entry is marked *done for this window*. An Always window never ends, so the entry **does not fire again until the bridge restarts**. Disabling and re-enabling it does not reset this.

    For "whenever the price is low" style rules, use **Custom cron** (for example `*/30 * * * *` with a 29-minute duration) so the entry re-arms every half hour. The [examples](examples.md) do this.

## Quick presets

![Quick presets](../../images/scheduler/trigger-preset-dropdown.png)

A preset fills the trigger fields; you can edit them afterwards.

| Preset | Sets |
|---|---|
| Every 5 / 10 / 15 / 30 minutes, Every minute, Every hour | Every N minutes, N = 5, 10, 15, 30, 1, 60 |
| Every day (midnight), Every day (morning 8am) | Daily at 00:00 / 08:00 |
| Every week (Sun midnight) | Weekly, Sunday, 00:00 |
| Every month (1st day), Quarterly, Six monthly, Annually | Monthly, day 1, months as named |
| Custom cron expression… | Custom cron |

## One clock

All triggers, tariff windows and `time.*` sensors use **the bridge's local clock** (its container timezone). The browser's timezone does not matter: the timeline, *Next fire* and the Activity Log are shown on the bridge's clock, and the zone is printed next to them (for example *(NZDT)*).

- On first start the bridge records its timezone. On later starts it compares. A mismatch (for example a container that fell back to UTC) means every schedule fires at the wrong wall-clock time. The top bar asks you to **confirm** the detected timezone or **change** it.
- `time.tz_ok` reads 0 when the clock no longer matches the recorded timezone, and `time.utc_offset_h` shows the current offset. Both can be used in conditions, for example to send a notification.
- `service.tz_matches_clock` reads 0 when your utility service's timezone differs from the bridge clock, which means TOU windows are applied at the wrong time.

## After an outage or restart (catch-up)

When the bridge loses a gateway for more than 60 seconds it records an *outage*. When the gateway is back, or when the bridge starts after being down for more than 60 seconds, it checks each entry targeting that gateway:

| Situation | What happens |
|---|---|
| The entry's window is **still open** | Nothing special: the next tick resumes it normally, gate permitting. |
| A window or fire **started and finished during the outage** | Handled by the entry's **If missed during an outage** policy, below. |
| Always entries | Never "missed": they have no fire time. |

![If missed during an outage](../../images/scheduler/missed-policy.png)

| Option (UI) | API `missed_policy` | Behaviour |
|---|---|---|
| resume if window still open, else log missed | `late_fire_remaining` (default) | Logs *missed*. |
| skip (log missed only) | `skip` | Logs *missed*. Today this behaves exactly like the default. |
| late-fire even if window passed | `late_fire_always` | Checks the entry conditions now. If they pass, sends the action now with the entry's duration as a time limit and logs *late_fired*; otherwise logs *missed*. |

The form shows this selector only for fire-based triggers, but window entries are caught up the same way using the default policy.

A late-fired command is not owned by the scheduler: it ends only at its time limit (or when you release it). An entry with no duration therefore late-fires with no time limit. The outage record on the timeline lists the missed fires.

See also [what happens on restart](exit-and-conflicts.md#bridge-restart).
