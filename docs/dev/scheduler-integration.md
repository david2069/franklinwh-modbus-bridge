# Use case: integrating with the Scheduler

Two ways to drive the bridge's scheduler from outside:

1. **Let Home Assistant decide, let the bridge act.** A schedule entry gates on a Home
   Assistant entity. Home Assistant (or you) flips the entity; the bridge sends the
   command, supervises it and releases it. No bridge credentials leave the bridge.
2. **Call the scheduler API** from your own client: create entries, fire one now, stop a
   run, read the Activity Log.

Use the first wherever you can. The bridge then owns the whole run: conflicts, the time
limit, exit conditions, verification, failure alerts and the release.

Everything here was run against a demo gateway. The sensor id `ha:ha_31ab8c2d:…` is that
bridge's instance id; yours differs (`GET /api/ha/instances`).

## 1. A Home Assistant entity as the trigger

**Goal:** while the EV charger is on, hold the house battery in standby so it doesn't
empty itself into the car, and tell your phone when that starts and ends.

**Home Assistant side:** an entity that is `on` while the car charges. Any switch, binary
sensor or helper works; here `switch.ev_charger`. Add the instance and expose the entity
([Home Assistant setup](../user/ha-setup.md)).

**Bridge side:** one entry.

| Field | Value |
|---|---|
| Trigger | Recurring window, every day, 17:00–23:00 |
| Action | Force Standby |
| Entry conditions (ALL) | `ha:<instance>:switch.ev_charger` `==` `1` |
| Exit conditions (ANY) | `ha:<instance>:switch.ev_charger` `==` `0` |
| HA action | Notify `mobile_app_<phone>`, **entry & exit**, message `EV charging: battery hold %battery.soc_pct%%` |

The same entry over the API:

```bash
curl -s -b jar.txt -H 'Content-Type: application/json' $B/api/schedules -d '{
  "name": "Hold battery while the EV charges",
  "when_spec": {"days": [], "windows": [{"start": "17:00", "end": "23:00"}]},
  "action": "force_standby",
  "target_type": "gateway", "target_id": "<gateway id>",
  "entry_conditions": {"match": "ALL", "conditions": [
    {"sensor": "ha:ha_31ab8c2d:switch.ev_charger", "op": "==", "value": 1}]},
  "exit_conditions": {"match": "ANY", "conditions": [
    {"sensor": "ha:ha_31ab8c2d:switch.ev_charger", "op": "==", "value": 0}]},
  "ha_actions": [{"kind": "notify", "instance_id": "ha_31ab8c2d",
    "service": "mobile_app_<phone>", "when": "both",
    "title": "Battery", "message": "EV charging: battery hold %battery.soc_pct%%"}]
}'
```

The reply is the stored entry with `id`, `active_now` and `next_fire`.

How it behaves:

- Inside the window, every 15 s, the engine checks the gate against the live cache. The
  moment the charger reads `on`, it sends Force Standby and the notification.
- When the charger turns `off`, the exit condition ends the run: the bridge releases the
  battery to its own mode and sends the exit notification. At 23:00 the window closes and
  it releases anyway.
- If Home Assistant is unreachable the entity has no value, the gate is false, and nothing
  is sent. During a run, a missing value can't satisfy the exit condition either; the
  window end still releases.
- Check a gate before you rely on it:

```bash
curl -s -b jar.txt -H 'Content-Type: application/json' \
  "$B/api/scheduler/evaluate?gateway=<gateway id>" -d '{"match": "ALL", "conditions": [
    {"sensor": "battery.soc_pct", "op": ">", "value": 20},
    {"sensor": "ha:ha_31ab8c2d:input_boolean.away_mode", "op": "==", "value": 1}]}'
```

```json
{"result": true, "per_condition": [
  {"sensor": "battery.soc_pct", "op": ">", "value": 20, "live_value": 35.9, "result": true},
  {"sensor": "ha:ha_31ab8c2d:input_boolean.away_mode", "op": "==", "value": 1, "live_value": true, "result": true}]}
```

A Home Assistant automation can drive the same thing through a helper you control: an
`input_boolean` that your automation turns on and off, and that the entry gates on. That
keeps the decision in Home Assistant and the battery control in the bridge.

### A notification-only entry

The [Home Assistant setup guide](../user/ha-setup.md#step-6-try-it-an-away-mode-alert)
builds an entry with **no battery action** (`"action": "none"`) that only notifies while
`input_boolean.away_mode` is on. That is the simplest way to see a Home Assistant entity
gate a schedule end to end.

## 2. The scheduler API

All of these need a signed-in **admin** account (the `api` role can read but not write
schedules), and the **Automations** module switched on. Full field lists are in the
[API reference → Schedules](api-reference.md#schedules).

| Task | Call |
|---|---|
| List entries, with `active_now` and `next_fire` | `GET /api/schedules` |
| Create | `POST /api/schedules` (body: `ScheduleCreate`) |
| Change, enable or disable | `PATCH /api/schedules/{id}`, for example `{"enabled": false}` |
| Delete | `DELETE /api/schedules/{id}` |
| Fire now | `POST /api/schedules/{id}/execute` (add `?force=true` to skip the entry conditions) |
| End the current run | `POST /api/schedules/{id}/stop` |
| Activity Log | `GET /api/schedules/log?schedule_id={id}&status=failed&limit=100` |
| Day's timeline | `GET /api/schedules/timeline?day=0` (0 = Monday) |
| Condition sensors and live values | `GET /api/sensors?gateway={gw}` |
| Evaluate a condition tree now | `POST /api/scheduler/evaluate?gateway={gw}` |
| Export / import entries | `GET /api/schedules/export`, `POST /api/schedules/import` |

Actions for `"action"`: `force_charge`, `force_discharge`, `force_standby`, `release`,
`mode` (`params.mode`), `reserve_self` / `reserve_tou` (`params.pct`), and `none` (Home
Assistant actions only). Force power is `params.power_w` in watts; `power_pct` is refused.
What each does is in [Scheduler → Actions](../user/scheduler/actions.md).

### Execute and Stop are different from a scheduled run

`…/execute` is the **▶ Run now** button and `…/stop` the **■ Stop** button (see
[Run now and Stop](../user/scheduler/exit-and-conflicts.md#run-now-and-stop)). Tried on
the demo gateway, with an entry like the one above plus `"duration_s": 600`:

```bash
curl -s -b jar.txt -X POST $B/api/schedules/sch_850687ba/execute
# {"status":"fired","dispatched":["demo"],"gated":[]}
curl -s -b jar.txt -X POST $B/api/schedules/sch_850687ba/stop
# {"status":"not_active","released":[]}      ← the battery is still in Force Standby
```

| | Scheduled run | `…/execute` |
|---|---|---|
| Entry conditions | checked | checked, unless `?force=true` (*gated* in the reply and the log) |
| Owned by the engine (verify, exit conditions, release at window end) | yes | **no**: the command's only time limit is the entry's `duration_s`. A window entry without one runs **until you release it** |
| HA actions and notifications | run | **not run** |
| A `none` entry | runs its HA actions | answers `unknown_action` |
| `…/stop` | releases the run | **doesn't** see it (`not_active`) |

To end an executed command early, send `Release` to the gateway:
`POST /api/gateways/{gw}/command` with `{"slug": "battery_command", "value": "Release"}`.

### Watching outcomes

Poll the Activity Log for one entry, newest first:

```bash
curl -s -b jar.txt "$B/api/schedules/log?schedule_id=sch_0e0bbea1&limit=5"
```

```json
{"events": [
  {"result": "ok", "detail": "dispatched Doc: Away mode alert — No battery action", "...": "..."},
  {"result": "ha_action", "detail": "[fire] notify.persistent_notification: Away mode is on. Battery at 33.6%, mode Emergency Backup."},
  {"result": "ha_action", "detail": "[fire] notify.mobile_app_docs_phone: Away mode is on. Battery at 33.6%, mode Emergency Backup."},
  {"result": "gated", "detail": "entry gated: ha:ha_31ab8c2d:input_boolean.away_mode == 1 (live=False)"}
]}
```

The `result` values are listed in
[Exit, release and conflicts → Activity Log results](../user/scheduler/exit-and-conflicts.md#activity-log-results).
For a push instead of polling, put a Notify action on the entry: failures, ineffective
dispatches and recoveries are sent to every notify target on it (see
[Failure alerts](../user/scheduler/actions.md#failure-alerts)).

## Things to know

- **One owner per battery.** Don't run a Home Assistant automation that commands the
  battery (over MQTT) at the same time as a bridge schedule. Let one of them own it.
- **Schedules use the bridge's clock.** Windows and cron times are in the bridge's
  timezone (see [Triggers](../user/scheduler/triggers.md)).
- **Changes apply at once.** Every create, update or delete reloads the engine; the next
  15 s tick uses it.
- **A restart re-fires open windows.** A window that is open when the bridge starts is
  dispatched again (by its missed-fire policy). Plan restarts outside your windows.
