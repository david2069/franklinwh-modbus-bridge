# Scheduler parity: the Modbus Bridge side

Direct Connect compared the two bridges' schedulers behaviour by behaviour
([its parity page](https://github.com/david2069/franklinwh-direct-connect-bridge/blob/main/docs/dev/scheduler-parity.md),
classified in its PR #212). It read this bridge's side from our
[test plan](scheduler-test-plan.md) markers only. This page is our side, **checked against
our code** at `main` 2d3e134:

- the **83 behaviours only Direct Connect has**: why each is missing here, or whether we
  actually have it;
- the **121 behaviours that differ**: what this bridge does per its code, whether the
  parity page describes it correctly, and the class of the difference.

Analysis only: no code was changed. The owner decides which gaps to close, on either side.

## How rows are classed

**Why missing**: *Not ported* (a known gap: issue, backlog or code note) · *Transport
limit* (the aGate Modbus TCP server can't do it; the register or field is named) · *Design
choice* (left out on purpose, cited) · *Depends on X* · *Unknown* (no record why). Two more
came out of the check: *Present* (this bridge has it, documented or not) and *N/A — Direct
Connect defect* (the row describes a Direct Connect bug this bridge doesn't have).

**Class**: *Behavioural* (a different outcome for the same intent: trigger, conditions,
action, target, exit, recorded result) or *Implementation variance* (the same outcome by
different code). Tie-break: anything that could change a battery write, a device write or
a recorded outcome is behavioural. *Impact* ranks behavioural rows: battery write, device
or HA write, recorded outcome, display.

## Summary

### The 83 Direct Connect-only behaviours

| Why missing here | Rows |
|---|---|
| N/A — Direct Connect defect | 28 |
| Present (undocumented) | 17 |
| Unknown | 12 |
| Present (documented) | 11 |
| Transport limit | 6 |
| Not ported | 4 |
| Design choice | 3 |
| Depends on | 2 |

| Class | Rows |
|---|---|
| None | 43 |
| Implementation variance | 23 |
| Behavioural | 17 |

### The 121 behaviours that differ

| Class | Rows |
|---|---|
| Behavioural | 106 |
| Implementation variance | 14 |
| Same | 1 |

| Impact (behavioural rows) | Rows |
|---|---|
| battery write | 59 |
| display | 23 |
| recorded outcome | 19 |
| device/HA write | 5 |

### Behavioural differences, battery writes first

The ones most worth a decision. Full detail is in the tables below.

**Battery writes**

- **Restarts** (CON-10, RST-03, OUT-38): engine state is in memory only, so a restart
  re-fires every open window, including one the user stopped or that already exited.
  Filed as **#231** (S2). Direct Connect persists fired and stopped occurrences.
- **Run now** (OUT-27, OUT-20, TGT-09): not owned by the engine, so Stop can't release it
  (#210); it fans out to every member gateway and respects entry conditions.
- **Window end with another entry active but gated** (EXT-05, Example 5): the force keeps
  running unreleased.
- **Always entries** (TRG-28, TRG-29, TRG-30): fire once per bridge run, a finished entry
  can fire again when another entry dispatches (#220), and the force isn't released when
  the gate turns false (#165).
- **Empty power** (ACT-04): full trusted rating here, 0 W in Direct Connect. Power is
  clamped to the trusted rating and refused without one (ACT-03, ACT-10); Direct Connect
  falls back to 5000 W and doesn't cap.
- **Time limits** (TRG-34, ACT-17): a window force has no time limit here; Direct Connect
  cuts it at a hidden 90 minutes. Duration is capped at 2 h here.
- **Triggers that resolve differently**: start == end never runs here and is 24 h in
  Direct Connect (TRG-09); weekly with no days never fires here, every day there (TRG-16);
  monthly with no day fires on the 1st here, every day there (TRG-23); an overnight
  one-off runs in the early morning of its own date here, past midnight there (TRG-13);
  the after-midnight part of an overnight window counts on the next day here (TRG-08).
- **Arbitration** (PRI-01 to PRI-13, CON-02): default conflict is defer here, override
  there; wait is an alias of defer; a higher priority never pre-empts a running entry; the
  equal-priority tie-break is oldest-first here, by name there.
- **Restore prior mode** (EXT-12, EXT-27): never writes the mode back (#27).
- **Unknown outcome** (OUT-31): no "unknown" verdict here; a write that landed but
  reported an error fails the WSetEna readback and is sent again.
- **Stale data** (CON-08, TRG-55): entries are evaluated on cached values; Direct Connect
  skips a gateway whose data is older than the larger of 90 s and 3 polls (#25).
- **Late fire** (TRG-54): the opt-in `late_fire_always` sends a battery command after an
  outage or restart; Direct Connect never does.

**Device and HA writes**

- Placeholders are not filled in an HA entity action's data (HAA-14).
- Exit conditions apply only to a running force: mode, reserve and HA-only entries never
  get an exit edge or exit HA actions (CND-36).
- Smart circuits and off-grid can't be done over Modbus (ACT-33 to ACT-35, PRI-17):
  transport limit.

**Recorded outcomes**

- Mode and reserve failures are logged "ok" (#222).
- When the target has no gateway nothing is sent and nothing is logged (ACT-31, TGT-10).
- `schedule_log` has no cap or retention (OUT-40).

### Where the parity page, or our own docs, describe this bridge wrongly

- **TRG-06**: reads as if MB fires once overall, but separate windows each fire in MB too; only adjacent windows merge into one run
- **TRG-13**: our marker leaves out that the after-midnight slice runs in the early morning of D, before the start
- **TRG-19**: MB's next_fire disagrees with the window in the same way; our marker omits this
- **TRG-23**: our marker covers the "no time_of_day" case; the "no day" case is not marked
- **HAA-01**: our marker misses the import path, which stores incomplete HA actions that never run and leave no log row
- **PRI-01**: our marker says other values are rejected, but import stores them
- **dependencies · An action is refused** (section): the doc says retries run every 60 s (they run at 15 s, 30 s, then 60 s) and that HA actions show 'ok' (they log 'ha_action')

Our doc markers that this check found wrong (HAA-01, PRI-01: true for POST and PUT, not
import) are filed as **#232**. The rest are corrected with the scheduler test phase.

### Defects found by this check

- **#231** (S2): Stop doesn't survive a restart; a stopped window fires the battery again.
- **#232** (S3): import skips POST validation; a bad `conflict` value acts as override.
- **#230** (S3, outside the scheduler): the pinned `franklinwh-modbus` v0.9.5 has the
  inverted rating clamp (library #28, fixed on its `develop`, not released). Not reachable
  from this bridge today.

## Questions for the owner

1. **Restarts:** should fired, stopped and exited windows survive a restart (#231), and
   should start-up get an interrupted-dispatch policy (notify, release or resume) instead
   of always release then re-fire?
2. **Run now:** should the engine own it, so Stop can release it (#210)?
3. **Window end:** if another entry is active but gated, should the old force be released
   (EXT-05)?
4. **Always entries:** re-arm on each rising edge and release when the gate turns false
   (#165)?
5. **Empty power:** full trusted rating (this bridge) or 0 W (Direct Connect)?
6. **Saving a running entry:** re-send the force now (this bridge) or at the next window
   (Direct Connect) (IDX-03)?
7. **Ambiguous triggers:** reject start == end and a weekly trigger with no days at save,
   instead of either bridge's guess?
8. **`wait` vs `defer`:** keep `wait` as an alias, or give it its own meaning?
9. **Stale data:** add a stale-snapshot gate to #25, or track it separately?
10. **Exit conditions** for mode, reserve and HA-only entries (CND-36)?
11. **Rows with no record either way** (marked *Unknown*): port them, or record them as
    design choices?
12. **Module switch:** should turning the Schedule module off stop the engine too
    (CON-06)? Today it only hides the pages.

## Direct Connect-only behaviours (83)

| # | Page · heading | Why missing | Source | Class | Note |
|---|---|---|---|---|---|
| IDX-08 | index · The entry list | Present (undocumented) | code | None | PATCH applies only the fields sent (`model_dump(exclude_unset=True)`), so the enable toggle changes only `enabled` (api/schedules_api.py:608-633). Same outcome as DC. |
| IDX-09 | index · The entry list | N/A — DC defect | code | None | MB `ScheduleUpdate.enabled` defaults to None and PATCH skips unset fields, so a PATCH without `enabled` never re-enables an entry (api/schedules_api.py:139, 619). |
| IDX-10 | index · Templates, export and import | Unknown | none found | Implementation variance | MB has four automation templates, no force presets by name. They open disabled for review, so no write until the user enables one (static/js/schedule_tab.js:1044-1053, 1069-1070). |
| IDX-11 | index · Visual timeline | N/A — DC defect | code | None | MB filters the timeline per gateway in the UI (static/js/schedule_tab.js:887-889). Server and engine use the same bridge clock (api/schedules_api.py:553, gateway/scheduler.py:396). |
| IDX-12 | index · Using schedules from Home Assistant (Direct Connect only) | N/A — DC defect | issue #32 | None | MB has no Pause/Resume. Stop releases the run and blocks the rest of the window (gateway/scheduler.py:610-660, api/schedules_api.py:529). A separate Pause/Resume is proposed in #32. |
| CON-08 | concepts · The tick | Not ported | issue #25 | Behavioural | MB skips disabled gateways (not in the registry, gateway/registry.py:75, 126) but evaluates and dispatches on stale or never-polled points. Age-out is asked for in #25 item 2. `POINTS_MAX_AGE_S` guards supervision only (gateway/scheduler.py:69, 1255-1268). |
| CON-09 | concepts · Window, activation and run | Present (undocumented) | code | Implementation variance | In memory: the engine owns one run per target, and `_expired` stops a re-fire in the window (gateway/scheduler.py:908, 943, 956). A failed send is retried with backoff until the window closes (gateway/scheduler.py:991-997, 1234-1253). There is no stored occurrence. |
| CON-10 | concepts · What the engine remembers | Unknown | doc | Behavioural | MB keeps all engine state in memory, so a restart re-fires open windows: a release, then a new dispatch. This is documented (docs/user/scheduler/concepts.md:87, docs/user/scheduler/restarts-and-clock.md RST-03) but no decision record says why. |
| TRG-49 | triggers · Summary | Unknown | none found | Behavioural | Only weekly (`days_of_week`) and monthly (`months`) carry calendar limits (gateway/scheduler_triggers.py:100-192). Interval, cron, always and oneoff cannot be limited to days, months or dates. |
| TRG-50 | triggers · Recurring windows | Unknown | none found | Behavioural | MB has no start/end date range and no reason when a calendar limit blocks today. A window entry has only `days` or a single `date` (api/schedules_api.py:60-65). |
| TRG-51 | triggers · One-off date window | N/A — DC defect | code | None | An MB dated window entry honours its windows on that date only (gateway/scheduler.py:129-138). A `oneoff` trigger fires at `fire_at` for its duration (gateway/scheduler_triggers.py:76-86). Note that `oneoff` has no UI (#172). |
| TRG-52 | triggers · Next fire | N/A — DC defect | code | None | MB `next_fire` and `active_now` use the same naive bridge clock as the tick (api/schedules_api.py:262-279, gateway/scheduler.py:396). Per-gateway clocks wait on #162. |
| TRG-53 | triggers · Editing a trigger in the form | Present (undocumented) | code | Implementation variance | `_blankForm` pre-seeds every type's fields (windows 14:00-19:00, time 18:00, interval 60 min, cron `0 0 1 * *`) (static/js/schedule_tab.js:985-996). The values differ from DC's. #172 notes that a stale `duration_s` is kept when switching types. |
| TRG-54 | triggers · After an outage or a restart (catch-up) | Present (documented, differs) | code | Behavioural | Default `late_fire_remaining` only logs *missed*, as DC does. The MB-only opt-in `late_fire_always` re-dispatches after an outage or a restart (gateway/scheduler.py:468-546, docs/user/scheduler/triggers.md:285). `skip` acts like the default (#172). |
| TRG-55 | triggers · After an outage or a restart (catch-up) | Not ported | issue #25 | Behavioural | No staleness gate before evaluation: `_snapshot` uses `latest_points()` whatever its age (gateway/scheduler.py:1464-1480, gateway/instance.py:440-446). Stale cached values can pass a gate (#25). |
| TRG-56 | triggers · After an outage or a restart (catch-up) | Present (undocumented) | code | Implementation variance | MB has no stored runs to coalesce. There is one owner and one retry record per target, and a retry for an entry that lost the target is dropped (gateway/scheduler.py:991-995). |
| CND-36 | conditions · Conditions (intro) | Unknown | doc | Behavioural | Exit conditions are checked only while a sustained force run is owned and active (gateway/scheduler.py:891-917, documented as EXT-07). Mode, reserve and HA-only entries never get an exit edge, an audit row or exit-triggered HA actions. |
| CND-37 | conditions · Home Assistant entities | Present (undocumented) | code | None | An HA price entity in `$`, AUD or dollars is scaled ×100 to c/kWh (gateway/aemo_nem.py:109-123). Same outcome as DC. |
| SEN-18 | conditions · Every sensor in the picker | Present (undocumented) | code | Implementation variance | The picker groups by sensor prefix (gateway/scheduler_sensors.py:991-1013), with HA entities as `HA · <instance>` (gateway/ha.py:366). The group names and order differ from DC's. `dispatch.*` sensors land in Other (#172). |
| SEN-19 | conditions · Tariff | N/A — DC defect | code | None | MB ages out the NEM price for sensors after 30 min (`SENSOR_MAX_AGE_S`, gateway/aemo_nem.py:58, 369, 381). |
| ACT-29 | actions · Summary | Present (undocumented) | code | None | Unknown actions are rejected at save and import (api/schedules_api.py:182-184, 382). If one reaches the engine, it is skipped with a warning and the dispatch is still audited *ok* (gateway/scheduler.py:1214-1216, 1085). |
| ACT-30 | actions · Battery actions: Force Charge and Force Discharge | Present (undocumented) | code | Implementation variance | No pre-read: a force is refused unless the battery rating is trusted (publish/command_handler.py:432-436). A failed Modbus write returns ok=False, which is audited *failed* and retried (publish/command_handler.py:395-399, gateway/scheduler.py:1045). |
| ACT-31 | actions · Battery actions: Force Charge and Force Discharge | Present (undocumented) | code | Behavioural | A target with no running gateway resolves to no pairs, so nothing is sent but also nothing is audited (main.py:565-587). A gateway with no host fails the send and is retried (#189). |
| ACT-32 | actions · Non-battery actions: Reserve and Operating Mode | N/A — DC defect | code | None | MB really writes reserves via `self_reserve_pct`/`tou_reserve_pct` (gateway/scheduler.py:334-337, publish/command_handler.py:881). Some sites write-lock 15508/15509 (docs/vendor-issues.md). |
| ACT-33 | actions · Smart circuits, off-grid and notify (Direct Connect only) | Transport limit | doc | Behavioural | Smart circuits are not exposed over Modbus (~/dev/franklinwh-modbus/docs/backlog.md:78 FEAT-ACCESSORY-SMART-CIRCUITS). They are Local API (TCP/9000) only (docs/vendor-issues.md:337-339). |
| ACT-34 | actions · Smart circuits, off-grid and notify (Direct Connect only) | Transport limit | doc | Behavioural | There is no smart-circuit write to parse On/Off for (~/dev/franklinwh-modbus/docs/backlog.md:78). |
| ACT-35 | actions · Smart circuits, off-grid and notify (Direct Connect only) | Transport limit | code | Behavioural | The library only reads off-grid state (`is_off_grid`, ~/dev/franklinwh-modbus/src/franklinwh_modbus/modes.py:167). `set_offgrid` is a Local API opcode (docs/modbus-library-docs-update-proposal.md:208). |
| ACT-37 | actions · Summary | Transport limit | doc | Implementation variance | There is no smart_circuit_schedule action to list as unavailable, because smart circuits are not on Modbus (~/dev/franklinwh-modbus/docs/backlog.md:78). |
| ACT-38 | actions · Battery actions: Force Charge and Force Discharge | Unknown | issue #167 | Implementation variance | MB is the Modbus path, so it has no client to be unavailable. The form does not disable force when the rating is untrusted, and the engine refuses at run (publish/command_handler.py:432-436). #167 tracks the same gap on the dashboard only. |
| ACT-39 | actions · What is written | N/A — DC defect | code | None | MB tracks ownership per (type, id, gateway) with one command handler per gateway, so forces on two gateways are independent (gateway/scheduler.py:416, main.py:565-587). |
| ACT-40 | actions · What is written | N/A — DC defect | code | None | MB never sleeps in a tick. A failed send is retried on later ticks after 15 s, 30 s, then 60 s (gateway/scheduler.py:56, 1234-1253, 996). |
| ACT-41 | actions · What is written | N/A — DC defect | code | None | MB judges by `last_dispatch` and then verifies against points: *verified*, *not_effective* or *unverifiable* (gateway/scheduler.py:1270-1373). |
| ACT-42 | actions · What is written | N/A — DC defect | code | None | MB retries until the window closes, with no deadline formula (gateway/scheduler.py:835-842). Always entries have the separate #165 issue. |
| HAA-13 | actions · Failure alerts | N/A — DC defect | code | None | MB notifies the entry's notify targets on the first failure, on recovery and when a window ends with no successful send (gateway/scheduler.py:1250-1253, 836-842, 1390-1412). |
| HAA-14 | actions · Placeholders in notifications | Unknown | doc | Behavioural | Placeholders are rendered only in a notification's title and message (gateway/scheduler.py:1179-1180). Entity action data is sent raw (gateway/scheduler.py:1188), as documented in docs/user/scheduler/actions.md:245. |
| HAA-15 | actions · Home Assistant actions | Unknown | none found | Implementation variance | MB has no raw-service mode. The UI is guided only, and `data` is a typed dict, so bad input fails at save with 422 rather than being skipped at run (api/schedules_api.py:84). |
| HAA-16 | actions · Home Assistant actions | Present (undocumented) | code | None | An entity action calls `POST /api/services/{domain}/{service}` with `{entity_id, **data}` (gateway/scheduler.py:1188, gateway/ha.py:208-212). Same as DC. |
| HAA-17 | actions · Placeholders in notifications | Unknown | issue #172 | Implementation variance | Only `%group.name%` sensor ids are substituted (gateway/scheduler.py:1095-1120). There are no run.\*, schedule.\* or gateway.id variables, though `gateway.serial` and the like exist as sensors. #172 notes the regex limits. |
| HAA-18 | actions · Home Assistant actions | Present (undocumented) | code | Implementation variance | Notify is an HA action kind on any entry, and action `none` gives a notify-only entry (api/schedules_api.py:72-96, gateway/scheduler.py:1162-1185). A missing target is skipped silently, not with a reason. |
| TGT-17 | targets · Entries with no target id | N/A — DC defect | issue #189 | None | MB binds an unbound entry to the gateway id `default` (main.py:569-572). If `default` is disabled, the entry runs nowhere with no audit row, which is the same outcome as the DC defect. |
| TGT-18 | targets · In the form | Present (undocumented) | code | None | A mock gateway has no Modbus and is always rating-trusted (gateway/mock_gateway.py, gateway/battery_rating.py:18). |
| TGT-19 | targets · In the form | N/A — DC defect | code | None | Test Verification uses the entry's bound gateway (static/js/schedule_tab.js:1378). Site and service targets fall back to `default` (#172). |
| TGT-20 | targets · How a fan-out works | N/A — DC defect | code | None | MB releases through the handler of the gateway that owns the run: Stop at gateway/scheduler.py:622-641, shutdown per gateway handler (EXT-20), start-up per gateway instance at gateway/instance.py:391-420. |
| TGT-21 | targets · Pick a target | Present (documented) | code | Implementation variance | MB always shows a Target type plus a Gateway list (templates/tabs/schedule.html:1223-1246); there is no "Default gateway" choice, and an API null target_id means the literal id `default` (main.py:569-573). |
| TGT-22 | targets · Pick a target | Present (documented) | code | None | Same outcome in MB: an unknown gateway id resolves to no handler and is skipped silently every tick (main.py:569-573, gateway/scheduler.py:724), documented as TGT-08. |
| TGT-23 | targets · Pick a target | Present (undocumented) | code | Implementation variance | MB has no gateway_scope; its fan-out lives in target_type, which the form round-trips (static/js/schedule_tab.js:1414-1416), and PATCH merges only sent fields (api/schedules_api.py:619); other API-only fields are still dropped by a form save (issue #218). |
| EXT-21 | exit-and-conflicts · How a run ends | N/A — DC defect | code | None | MB delete reloads the engine (api/schedules_api.py:648-662) and the next tick releases any target it still owns (gateway/scheduler.py:735-743); the exception is a hold-mode standby (EXT-19). |
| EXT-22 | exit-and-conflicts · Exit conditions | Present (documented) | code | None | Exit trees use the same evaluator as entry trees (gateway/scheduler.py:902-904) and groups nest to any depth (gateway/scheduler_conditions.py:278-285, CND-17); MB has no test that targets a nested exit tree. |
| EXT-24 | exit-and-conflicts · How a run ends | N/A — DC defect | code | None | MB releases once: a met exit tree drops ownership and marks the window done (gateway/scheduler.py:906-918), so later flaps do not release again. |
| EXT-25 | exit-and-conflicts · How a run ends | N/A — DC defect | code | None | An MB window entry is bounded by its window (gateway/scheduler.py:835-881) and stages only params.duration_s; a stale entry duration_s left by the form (issue #218) affects Run now only. |
| EXT-26 | exit-and-conflicts · How a run ends | Present (documented) | code | None | MB's command-handler watchdog checks duration and target SoC every 5 s from cached poll points (publish/command_handler.py:87, 946-1000), documented as ACT-17 and ACT-18. |
| EXT-27 | exit-and-conflicts · Release policy | Design choice | issue #27 | Behavioural | MB snapshots the mode at dispatch (gateway/scheduler.py:1071) but restore_prior_mode never writes it back and only audits a change (gateway/scheduler.py:343-360, 1429-1433). |
| EXT-28 | exit-and-conflicts · Release policy | Present (undocumented) | code | None | release_policy has a form control (templates/tabs/schedule.html:1261) and is loaded and sent back on save (static/js/schedule_tab.js:1158, 1418); PATCH is a merge (api/schedules_api.py:619). |
| PRI-16 | priorities · Manual control | Present (documented) | code | Implementation variance | MB defer and wait both re-check each tick and take over when the manual command ends (gateway/scheduler.py:964-979), so MB defer and wait act like DC wait; the defer is logged once, not every tick. |
| PRI-17 | priorities · Entries on the same target | Transport limit | doc | Implementation variance | Smart circuits are not exposed over Modbus (franklinwh-modbus docs/backlog.md:78; docs/energy-totals-sensors-design.md:81) and MB has no off-grid action (gateway/scheduler.py:307-340), so every non-none action shares one battery lane (gateway/scheduler.py:722). |
| PRI-18 | priorities · Entries on the same target | Present (documented) | code | None | MB puts action `none` entries in their own lane, so they never contend (gateway/scheduler.py:722-733); notify actions belong to an entry and claim nothing. |
| PRI-19 | priorities · Manual control | N/A — DC defect | code | None | MB marks no date on defer; it audits the defer once per target with a set (gateway/scheduler.py:970-978), so it is the same for all trigger kinds. |
| OUT-26 | outcomes · The dispatch record | Present (documented) | code | None | The command handler writes a dispatch_outcomes row for every force it sends, scheduled or Run now, with requester schedule:<id> (publish/command_handler.py:526-535); the row has no window end. |
| OUT-27 | outcomes · Run now | Not ported | issue #210 | Behavioural | Run now is not owned by the engine (gateway/scheduler.py:555-557), so Stop answers not_active and cannot release it (gateway/scheduler.py:622-659); other entries see it as manual control. |
| OUT-28 | outcomes · Run now | N/A — DC defect | code | None | MB Run now takes no gateway parameter and always uses the entry's own target (api/schedules_api.py:516-525, gateway/scheduler.py:572-578). |
| OUT-29 | outcomes · Retries | Present (documented) | code | Implementation variance | MB has no grace sweep: outage catch-up records missed fires (gateway/scheduler.py:468-547), and a window that closes after failed attempts is logged failed (gateway/scheduler.py:836-844). |
| OUT-30 | outcomes · Activity Log results | Unknown | none found | Implementation variance | MB keeps no run or occurrence rows, only in-memory owners per target (gateway/scheduler.py:412-452), so there is no backlog to coalesce. |
| OUT-31 | outcomes · Retries | Design choice | doc | Behavioural | MB has no unknown verdict: it reads WSetEna back after a send and treats anything but 1 as failed and retries (publish/command_handler.py:515-521, docs/dispatch-reliability-design.md), so a write that landed but reported an error is sent again. |
| OUT-32 | outcomes · Retries | N/A — DC defect | code | None | MB has no unknown state to reach; the readback decides ok or failed (publish/command_handler.py:515-524). |
| OUT-33 | outcomes · Retries | N/A — DC defect | code | None | MB retries across ticks with a 15/30/60 s backoff and never sleeps inside the tick (gateway/scheduler.py:991-997, 1234-1253). |
| OUT-34 | outcomes · Retries | N/A — DC defect | code | None | A refused or failed MB force is logged failed (publish/command_handler.py:1014-1022, gateway/scheduler.py:1044-1046); MB still logs mode and reserve failures as ok (issues #166, #222). |
| OUT-35 | outcomes · Retries | N/A — DC defect | code | None | MB's audit sink takes the same positional arguments the engine passes (gateway/scheduler.py:1537-1550, main.py:589-593). |
| OUT-36 | outcomes · Run now and Stop | N/A — DC defect | issue #32 | None | MB has no Pause; Stop is terminal for the window (gateway/scheduler.py:610-659), and Pause/Resume is tracked in #32. |
| OUT-37 | outcomes · Run now and Stop | Not ported | issue #32 | Behavioural | MB has no Resume, so a stopped run cannot re-enter its open window (gateway/scheduler.py:643, 656); this is BR-18, recorded as "not yet" in docs/BRIDGE_CATALOGUE.md:342. |
| OUT-38 | outcomes · Run now and Stop | Depends on Pause/Resume (#32) | issue #32 | Implementation variance | MB has no paused state to resolve; its Stop mark is in memory only (gateway/scheduler.py:428, 643), so a restart lets a stopped entry fire again in an open window. |
| OUT-39 | outcomes · Where to look | Unknown | none found | Implementation variance | MB has no /runs or /stats; the closest is the Activity Log filtered by schedule_id and status (api/schedules_api.py:496-513) and the dispatch-outcomes list. |
| OUT-40 | outcomes · Activity Log results | Unknown | none found | Implementation variance | MB's schedule_log has no severity class and no cap or retention at all (store/db.py:523-532, 1907-1922). |
| OUT-41 | outcomes · Activity Log results | Present (undocumented) | code | None | An action `none` entry is still logged "ok — dispatched <name> — No battery action" (gateway/scheduler.py:1085-1088, 290). |
| OUT-42 | outcomes · Retries | Present (documented) | code | None | MB logs "dispatch failed: <reason> — attempt N, retrying in Ns" and does not own the window (gateway/scheduler.py:1234-1249). |
| OUT-43 | outcomes · Run now | N/A — DC defect | issue #210 | None | MB Run now runs no HA actions at all (#210); scheduled messages render from the full sensor snapshot (gateway/scheduler.py:1095-1120). |
| CLK-05 | restarts-and-clock · The clock | Present (documented) | code | Implementation variance | MB never reads the aGate clock; it always uses the bridge's local clock (gateway/scheduler.py:396), with a timezone guard at start (config/clock.py:96-150), so it is never UTC by accident. |
| CLK-06 | restarts-and-clock · The clock | Transport limit | issue #162 | Implementation variance | SunSpec Modbus does not expose the aGate's clock or timezone (issue #162), so MB has no device clock to check; per-gateway zones are planned in #162. |
| RST-05 | restarts-and-clock · Bridge restart | Depends on Pause/Resume (#32) | issue #32 | Implementation variance | With no Pause in MB there is nothing to resolve at start-up; BR-18 requires a restart while paused to end as stopped (docs/BRIDGE_CATALOGUE.md:147-149). |
| RST-06 | restarts-and-clock · Bridge restart | Design choice | doc | Behavioural | MB has one fixed start-up policy: release any persisted force "for safety" (gateway/instance.py:391-420), then the first tick fires open windows again (docs/restarts-and-shutdown.md); there is no notify, release or resume choice. |
| RST-07 | restarts-and-clock · Bridge restart | N/A — DC defect | code | None | In MB a force never outlives a restart: gateway start-up releases any persisted force (gateway/instance.py:391-420). |
| RST-08 | restarts-and-clock · Bridge restart | N/A — DC defect | code | None | MB reconciles and releases each gateway in its own instance (gateway/instance.py:264-270, 391-420) and on shutdown through each handler (EXT-20). |
| — | dependencies · A Home Assistant action did not run | Present (undocumented) | code | None | The behaviour exists (guard skips audited gated, unreadable values render "?", failures audited; gateway/scheduler.py:1113-1115, 1168-1200), but MB's troubleshooting page has no section for it. |
| — | examples · More examples (Direct Connect only) | Present (undocumented) | code | None | MB has its own Templates menu (templates/tabs/schedule.html:25-31, static/js/schedule_tab.js:1063); examples.md mentions only one template and has no template recipes. |

## Behaviours that differ (121)

| # | Page · heading | Modbus Bridge per code | Page accurate? | Class | Impact | Note |
|---|---|---|---|---|---|---|
| IDX-01 | index · The entry list | The list defaults to Enabled only, with a "N hidden" count (static/js/schedule_tab.js:236-241). | yes | Behavioural | display | Same idea in both; DC's show-all default makes a disabled entry easier to find. |
| IDX-02 | index · The entry list | Duplicate POSTs a disabled "(copy)" entry straight away (static/js/schedule_tab.js:1552-1584). | yes | Behavioural | display | DC looks better: nothing is stored until Save, so no leftover copies. |
| IDX-03 | index · The entry list | Asks to confirm, then a changed action or power re-dispatches on the reload tick (gateway/scheduler.py:984-989, 1039). | yes | Behavioural | battery write | MB applies an edit to the running force; DC waits for the next window. Owner should pick one. |
| IDX-04 | index · Templates, export and import | A template opens disabled; after Save MB asks "Enable now?" only when the form opened disabled (static/js/schedule_tab.js:1478-1493). | yes | Behavioural | display | MB's prompt is the friendlier flow. |
| IDX-06 | index · Visual timeline | Enabled entries only, per-weekday preview with a gateway selector, fire+duration of at least 8 min, interval drawn (cap 96), monthly/cron/always not drawn (gateway/scheduler_triggers.py:324-384). | yes | Behavioural | display | MB draws more (interval, per gateway). |
| IDX-07 | index · Visual timeline | A one-off is drawn on its weekday in every week, even after the date has passed (api/schedules_api.py:585-590). | yes | Behavioural | display | DC is correct here; MB's preview is misleading. |
| CON-02 | concepts · The tick | Loads enabled entries only, ordered priority desc then oldest first; an incumbent keeps the gateway (store/db.py:1772-1788, gateway/scheduler.py:755-806). | yes | Behavioural | battery write | Differs only in the equal-priority tie-break (oldest vs name). MB's tie-break is the more predictable one. |
| CON-05 | concepts · The tick | Every create, update, delete and import reloads the engine and ticks at once (api/schedules_api.py:194-200, gateway/scheduler.py:463-466). | yes | Implementation variance | | Same result; DC acts up to 15 s later. |
| CON-06 | concepts · The tick | The automations module switch only returns 403 on the schedule routes; the engine still starts and runs (main.py:963, 1211-1218). | yes (our marker says unclear; the code confirms it) | Implementation variance | | Both always run the engine. MB's switch suggests it is off when it is not. Owner question. |
| TRG-02 | triggers · Fire-based triggers: the active window | Empty or 0 duration_s gives a 60 s active window, then the force is released when the window ends (gateway/scheduler.py:197-199). | yes | Implementation variance | | Both release a force about a minute after the fire. |
| TRG-06 | triggers · Recurring windows | Active inside any window; the done-for-this-window mark clears when no window is open, so separate windows each fire (gateway/scheduler.py:138, 845). | NO — reads as if MB fires once overall, but separate windows each fire in MB too; only adjacent windows merge into one run | Implementation variance | | Differs only for back-to-back windows (one run in MB, two occurrences in DC). |
| TRG-08 | triggers · Recurring windows | Weekday test uses the current day, so the part after midnight belongs to the next day (gateway/scheduler.py:135-138). | yes | Behavioural | battery write | DC's "the window belongs to the day it opened" is what users expect. MB runs Mon 00:00-06:00 for a Mon-only 22:00-06:00. |
| TRG-09 | triggers · Recurring windows | start == end matches nothing, so it never runs (gateway/scheduler.py:122-123). | yes | Behavioural | battery write | Open DC #198; owner to pick never or 24 h, and arguably reject it at save. |
| TRG-10 | triggers · Recurring windows | Times parse modulo 24 h (24:00 = 00:00, so 18:00-24:00 runs to midnight); the API pattern accepts 25:10 as 01:10 (gateway/scheduler.py:80-86). | yes | Behavioural | battery write | Same for 24:00; differs only for out-of-range or empty windows. Both should reject them at save. |
| TRG-11 | triggers · One-off date window | A dated entry is active only on its date and ignores days (gateway/scheduler.py:129-136). | yes | Behavioural | battery write | MB is better: in DC, stale stored days can stop a one-off from ever firing. |
| TRG-13 | triggers · One-off date window | An overnight one-off runs on date D only: from 00:00 to end and from start to midnight; it never runs on D+1 (gateway/scheduler.py:131-138). | NO — our marker leaves out that the after-midnight slice runs in the early morning of D, before the start | Behavioural | battery write | DC is right; MB fires before the intended start. |
| TRG-16 | triggers · Weekly at time | A weekly trigger with no days never fires and is accepted at save (gateway/scheduler_triggers.py:100-106). | yes | Behavioural | battery write | Neither is ideal; rejecting at save is best. |
| TRG-19 | triggers · Every N minutes | An interval of 24 h or more fires daily at the anchor, and next_fire shows anchor + interval (gateway/scheduler_triggers.py:118-136, 304-321). | NO — MB's next_fire disagrees with the window in the same way; our marker omits this | Same | | Both share the bug (fires daily, shows N days). Not a parity gap. |
| TRG-22 | triggers · Monthly / calendar | Rejects a day outside 1-31 and months that are not integers 1-12 (api/schedules_api.py:169-179). | yes | Behavioural | battery write | MB is better: DC treats day 0 as "every day". |
| TRG-23 | triggers · Monthly / calendar | No day defaults to day 1, so it fires monthly on the 1st (gateway/scheduler_triggers.py:155). | NO — our marker covers the "no time_of_day" case; the "no day" case is not marked | Behavioural | battery write | MB is better: DC fires every day. |
| TRG-24 | triggers · Custom cron | An invalid or empty cron is rejected at save (api/schedules_api.py:159-168). | yes | Behavioural | display | Neither fires; MB reports the error at save. |
| TRG-27 | triggers · Always evaluate | Always active; dispatches when the entry gate passes, then holds the gateway as incumbent (gateway/scheduler.py:189-191, 784-789). | yes | Implementation variance | | First fire matches DC's rising edge; the real differences are TRG-28 and TRG-30. |
| TRG-28 | triggers · Always evaluate | After a run ends, the always entry is marked expired and never re-fires until restart; an HA-only always fires once per bridge run (gateway/scheduler.py:845, 943, 956-957). | yes | Behavioural | battery write | Both fire once: until restart in MB, ever in DC. Neither re-arms on a new rising edge. Owner question. |
| TRG-29 | triggers · Always evaluate | The in-memory expired mark per target is cleared by any other entry's dispatch on that target, so a finished entry can fire again (gateway/scheduler.py:1039). | yes | Behavioural | battery write | MB can re-fire after another entry runs; DC's stored row never re-fires. Both are off-spec. |
| TRG-30 | triggers · Always evaluate | The incumbent keeps the gateway with no gate re-check, so the force is not released when conditions stop (gateway/scheduler.py:784-789). | yes | Behavioural | battery write | DC is better and matches MB's own form text (schedule.html:689-692). |
| TRG-32 | triggers · Next fire | Next window start within 8 days, or next_fire_at for triggers; none for always (gateway/scheduler.py:141-169). | yes | Behavioural | display | 8 days covers windows; minor. |
| TRG-33 | triggers · Next fire | active_now = enabled and inside the window, even when gated or not running (api/schedules_api.py:278). | yes | Behavioural | display | DC is closer to "running". MB's version also drives the save-confirm prompt. |
| TRG-34 | triggers · Editing a trigger in the form | A window save leaves an old duration_s in place, but only Run now uses it as a time limit (gateway/scheduler.py:570-571); a scheduled window force has no time limit. | yes | Behavioural | battery write | MB's scheduled force lasts the whole window; DC's hidden 90 min (#160) cuts it short. |
| TRG-40 | triggers · After an outage or a restart (catch-up) | On catch-up, a fire or window start in (outage start, now] that is not active now is recorded missed (gateway/scheduler.py:210-227). | yes | Behavioural | recorded outcome | MB records windows nobody saw; DC only claimed runs. |
| TRG-41 | triggers · After an outage or a restart (catch-up) | One missed row per entry per outage, even for a window never ticked (gateway/scheduler.py:220-227). | yes | Behavioural | recorded outcome | MB's history is more complete. |
| CND-02 | conditions · Rules for every condition | An unknown sensor or operator evaluates false, with no check at save; import warns about unknown sensors (gateway/scheduler_conditions.py:279; api/schedules_api.py:406-418). | yes | Behavioural | display | Run-time is the same (fail closed); only MB's import warns. |
| CND-03 | conditions · Rules for every condition | One cached snapshot per gateway per tick, built without a network call (gateway/scheduler.py:1464-1480, main.py:651-680). | yes | Implementation variance | | Same values; DC also polls HA REST every tick. |
| CND-06 | conditions · Operators | == and != on text are case-sensitive; numbers compare numerically (gateway/scheduler_conditions.py:105-113). | yes | Behavioural | battery write | DC (case-insensitive) is more forgiving. MB has matchlist for that. |
| CND-08 | conditions · Operators | in / not_in accept numbers and ranges, with brackets ignored (gateway/scheduler_conditions.py:116-202). | yes | Behavioural | battery write | MB is better: in DC, "(10, 20)" loses members. |
| CND-15 | conditions · Value and Lookup | The Details panel shows a Lookup row as "sensor op undefined" (static/js/schedule_tab.js:753-762). | yes | Behavioural | display | Both have Lookup display bugs, in different places. |
| CND-19 | conditions · Groups and items: ALL / ANY | The form prunes empty groups and saves an empty block as null (static/js/schedule_tab.js:1318-1347). | yes | Implementation variance | | null and [] have the same effect. |
| CND-20 | conditions · Groups and items: ALL / ANY | An API exit {ALL, []} is true and releases a started run on the next tick (gateway/scheduler.py:902-905). | yes | Behavioural | battery write | MB fires briefly then releases; DC never starts. DC is better; ideally reject at save. |
| CND-22 | conditions · Scope: which gateway's values | Demand, bonus, billing-window and fixed-charge points come from the default gateway and are merged into every gateway's snapshot (main.py:643-646, 672-677). | yes | Behavioural | battery write | DC is better on multi-gateway sites. |
| CND-26 | conditions · Hold for (dwell) | entry_hold_s 0 fires at once; the API caps it at 0-86400 s (api/schedules_api.py:128). | yes | Behavioural | display | Only the save-time bound differs. |
| CND-27 | conditions · Hold for (dwell) | A retry after a failed send skips the hold (gateway/scheduler.py:1023). | yes | Behavioural | battery write | MB retries 15 s after a failure; DC waits the whole hold again. MB looks better. |
| CND-29 | conditions · When conditions are evaluated | A failing gate is audited once per window (re-armed when the gate passes), with up to 4 failing leaves (gateway/scheduler.py:265-276, 1003-1019). | yes | Behavioural | recorded outcome | MB's log is tied to the window; DC's is daily. |
| CND-30 | conditions · Test Verification | Test posts to /api/scheduler/evaluate for the schedule's target gateway, or the default gateway for site and service (api/scheduler_api.py:121-129). | yes | Behavioural | display | MB is better: it tests against the schedule's own gateway. |
| CND-31 | conditions · Test Verification | Evaluate builds points without dispatch_* and device_type, so those sensors differ from the engine's (api/scheduler_api.py:29-62). | yes | Behavioural | display | Both Test paths diverge from their engines, in different ways. |
| CND-32 | conditions · Test Verification | POST /api/scheduler/evaluate needs the admin role (main.py:1215-1218). | yes | Behavioural | display | This is the bridges' auth model (MB has roles, DC has no login), not a scheduler gap. |
| CND-34 | conditions · Home Assistant entities | HA values come from WebSocket state_changed events, with a 30 s REST fallback (gateway/ha.py:48, 135-140). | yes | Implementation variance | | Same values; MB is lighter on HA. |
| CND-35 | conditions · Home Assistant entities | Import warns about unknown sensors and ha: sensors whose instance is missing (api/schedules_api.py:406-418). | yes | Behavioural | display | MB is better (DC #202). |
| SEN-04 | conditions · Grid | grid.connected comes from connection_state, else grid mode (gateway/scheduler_sensors.py:82-92). | yes | Behavioural | battery write | MB is better: DC reads 0 W on grid as off-grid (#206). |
| SEN-08 | conditions · Tariff | Window flags come from the first service with the feature and read False when none is set (gateway/scheduler_sensors.py:353-357). | yes | Behavioural | battery write | "== 0" passes in MB and fails closed in DC. DC's None is safer. |
| SEN-10 | conditions · Tariff | spot_price and feed_in_price read None when the cache is older than 30 min (gateway/aemo_nem.py:369-374). | yes | Behavioural | battery write | MB is better (DC #207). |
| ACT-02 | actions · Summary | Rejects an unknown action, a mode without params.mode and a reserve without pct (api/schedules_api.py:182-189). | yes | Behavioural | recorded outcome | MB is better: an error at save, not a failure at run (DC #202). |
| ACT-03 | actions · Power and sizing | Force watts are capped at the trusted rating for the direction; no trusted rating means the command is refused (publish/command_handler.py:436-454). | yes | Behavioural | battery write | MB is safer; DC's 5000 W fallback can over- or under-drive. |
| ACT-04 | actions · Power and sizing | Empty power stages 0, which means the full trusted rating (gateway/scheduler.py:315-329, publish/command_handler.py:451). | yes | Behavioural | battery write | Opposite outcomes (full power vs 0 W). Owner to set the intended meaning (DC #209). |
| ACT-05 | actions · Power and sizing | Watts only; params.power_pct is rejected with 400 (api/schedules_api.py:190-191). | yes | Behavioural | display | MB removed % on purpose (#133). Only the units offered differ. |
| ACT-10 | actions · Power and sizing | Staged watts are clamped to max_power_w, then to the trusted cap (publish/command_handler.py:343, 451). | yes | Behavioural | battery write | MB caps, DC does not. MB's nameplate clamp can block a calibrated rating above nameplate. |
| ACT-13 | actions · Power and sizing | A capability refusal sets last_dispatch not ok, so it is audited failed and retried (publish/command_handler.py:1014-1022, gateway/scheduler.py:1043-1046). | yes | Behavioural | recorded outcome | MB is better (DC #191). |
| ACT-14 | actions · What is written | Sends the setpoint (with duration), writes WSetRvrtTms, then reads WSetEna back; a value other than 1 means failed (publish/command_handler.py:491-524). | yes | Behavioural | recorded outcome | MB verifies the write; DC trusts the library's return. |
| ACT-15 | actions · What is written | The bridge watchdog releases; the aGate is noted not to revert at WSetRvrtRem 0 (publish/command_handler.py:504-512). | yes | Implementation variance | | Both rely on their own watchdog. MB also hands a duration to the device. |
| ACT-17 | actions · Parameters you can set only through the API | params.duration_s is clamped to 0-7200 s and enforced by the watchdog; a window force with none has no limit (publish/command_handler.py:367-370). | yes | Behavioural | battery write | MB caps at 2 h and has no hidden 90 min. DC's limit cuts long windows short (#160). |
| ACT-18 | actions · Parameters you can set only through the API | target_soc releases at the target, checked every 5 s; a target already reached at start is cleared (publish/command_handler.py:474-489, 980-1005). | yes | Implementation variance | | Same engine result; only MB hides it from the form. |
| ACT-19 | actions · Parameters you can set only through the API | A form save replaces params, dropping API-only duration_s and target_soc (static/js/schedule_tab.js:1295-1307, 1414). | yes | Behavioural | battery write | DC is better: in MB, a form save silently removes a time limit or target. |
| ACT-23 | actions · Non-battery actions: Reserve and Operating Mode | Requires params.mode at save; an unknown mode name fails at run with "Unknown mode" (api/schedules_api.py:186-187, publish/command_handler.py:840-846). | yes | Behavioural | recorded outcome | MB catches a missing mode at save. Neither checks mode names. |
| ACT-28 | actions · No battery action (HA only) | Run now never runs HA actions; on a none entry it returns unknown_action and sends nothing (scheduler.py:595-597). | yes | Behavioural | device/HA write | DC is more useful here: Run now on an HA-only entry does something. Owner: should MB Run now run fire-phase HA actions? |
| HAA-01 | actions · Home Assistant actions | POST/PATCH reject entity actions without entity_id and notify without message (schedules_api.py:72-105); import skips this check (schedules_api.py:371-421), and the engine then skips incomplete rows silently (scheduler.py:1162-1166). | NO — our marker misses the import path, which stores incomplete HA actions that never run and leave no log row | Behavioural | recorded outcome | MB is better on save; the import gap should be closed. |
| HAA-02 | actions · Home Assistant actions | The form filters out incomplete HA rows on save without telling the user (schedule_tab.js:1423-1430). | yes | Behavioural | recorded outcome | Both are weak: MB loses rows silently, DC stores rows that are skipped at run time. |
| HAA-06 | actions · When they run | Exit HA actions run once, and only for an activation that fired: on window close, exit condition, in-window end, Stop, disable or delete (scheduler.py:850, 917, 952, 644, 1202-1206). | yes | Behavioural | device/HA write | MB is better: DC fires exit actions for windows that never fired and skips them on Stop. |
| HAA-07 | actions · When they run | Fire HA actions run after a successful force send; a failed send returns before them, and they run once per activation (scheduler.py:1041-1053). | yes | Behavioural | device/HA write | MB is better: DC repeats the actions on every retry. |
| HAA-08 | actions · Guards | A guard is a single leaf; when false, the action is skipped with a gated audit row (scheduler.py:1122-1126, 1168-1175). | yes | Behavioural | recorded outcome | MB logs the skip; DC accepts a tree. A tree sent to MB's API is evaluated as one leaf and fails closed. |
| HAA-10 | actions · Placeholders in notifications | The placeholder regex matches only two-part ids, so three-part ids (energy.*.total_kwh) and ha: ids are left as typed (scheduler.py:1120). | yes | Behavioural | display | DC handles three-part ids; neither handles ha: ids. |
| HAA-12 | actions · What is logged | Each HA action gets its own ha_action or failed row; with no HA hooks, one failed row is written (scheduler.py:1148-1153, 1191-1200). | yes | Behavioural | recorded outcome | MB is clearer; the exception is incomplete imported rows, which leave no row (see HAA-01). |
| TGT-01 | targets · Pick a target | A gateway target resolves while the gateway is registered, with no is_running check, so a disconnected gateway is still tried (main.py:569-573, registry.py:48-50). | yes | Behavioural | recorded outcome | MB writes failed/retry rows for an unreachable gateway; DC skips it silently. |
| TGT-02 | targets · Pick a target | The Site target is offered in the form and fans out to every running gateway, mocks included (main.py:574-578, schedule.html:1228). | yes | Implementation variance | | Same fan-out result; MB exposes it in the UI. |
| TGT-06 | targets · How a fan-out works | Each Activity Log row names the gateway, with (mock) for a mock (scheduler.py:1552-1564, main.py:685-690). | yes | Behavioural | recorded outcome | MB is better (DC #205). |
| TGT-09 | targets · How a fan-out works | Run now sends to every member gateway that passes its own gate; the UI passes no gateway (scheduler.py:578-605, schedule_tab.js:1509). | yes | Behavioural | battery write | MB is better: DC's top-bar gateway can redirect the command. |
| TGT-10 | targets · Entries with no target id | target_id null resolves to the literal id default; with no gateway called default, the entry resolves to nothing and logs nothing (main.py:570). | yes | Behavioural | battery write | Owner: should MB log a row when a target resolves to no gateway? |
| TGT-11 | targets · Entries with no target id | null and 'default' form two separate arbitration groups for the same gateway (scheduler.py:686-692). | yes | Behavioural | battery write | DC arbitrates them together. Only the API can create null, since the form always sends default. |
| TGT-15 | targets · On the timeline | The timeline filters by gateway: Site segments show on every gateway, Gateway segments on their own, Service segments only in All (schedule_tab.js:884-891). | yes | Behavioural | display | MB is better. |
| EXT-01 | exit-and-conflicts · How a run ends | At window end, only an owned sustained run is released (release policy, or hold); logged ok 'window exit — released…' (scheduler.py:835-884). | yes | Behavioural | battery write | MB is better: DC releases forces it does not own (#189). |
| EXT-03 | exit-and-conflicts · How a run ends | The in-memory _expired mark is cleared on window close, so the next window fires again (scheduler.py:845). | yes | Implementation variance | | Same result within one run; the restart difference is covered by RST-03. |
| EXT-04 | exit-and-conflicts · How a run ends | Disable and delete both drop the entry; the reload tick releases an owned run when no other entry is active on that target, and runs exit HA actions (scheduler.py:731-753, 456-466). | yes | Behavioural | battery write | MB is better: DC does not release on delete. |
| EXT-05 | exit-and-conflicts · How a run ends | At window end, another active entry on the same target becomes the winner: if its gate passes it takes over, and if gated the old command is left running with no release (scheduler.py:774-802, 1005-1023). | yes | Behavioural | battery write | Both are wrong in opposite ways; MB's leftover force is the riskier one. |
| EXT-07 | exit-and-conflicts · Exit conditions | Exit is checked only while an owned run is active, so an exit already true still dispatches once, then releases on the next tick (scheduler.py:896-905). | yes | Behavioural | battery write | DC is better: no needless start/stop cycle. |
| EXT-09 | exit-and-conflicts · Exit conditions | Exit conditions apply only to owned sustained force runs; one-shot, none and Run now are never exit-checked (scheduler.py:896-900). | yes | Behavioural | device/HA write | DC can end mode and HA-only entries early. Owner: is MB's narrower scope intended? |
| EXT-10 | exit-and-conflicts · Exit conditions | The exit_condition_met detail lists the failing leaves, so it names the rows that did not match (scheduler.py:909, 273-276). | yes | Behavioural | display | Both are weak; MB's text is actively misleading and should name the matching rows. |
| EXT-12 | exit-and-conflicts · Release policy | restore_prior_mode releases and only audits a mode change; it never writes a mode (scheduler.py:343-360, 1429-1433). | yes | Behavioural | battery write | Deliberate in MB (#27); DC writes the mode back. |
| EXT-17 | exit-and-conflicts · What a release does on the gateway | A release reads WSetEna back; still 1 gives release_unverified; the staged target SoC is cleared (command_handler.py:687-739). | yes | Behavioural | recorded outcome | MB is better. |
| EXT-20 | exit-and-conflicts · Bridge shutdown | The engine stops first, then each gateway's own handler releases its active command (reason shutdown, fast); no Activity Log row, no exit HA actions (main.py:1083, 1102; command_handler.py:1057-1067). | yes | Behavioural | battery write | MB is better: DC releases to the first gateway's host (#192). |
| PRI-01 | priorities · Priorities and conflicts | POST/PATCH accept priority 0-1000 and conflict defer/override/wait, default defer (schedules_api.py:118-119); import skips both checks, and any other conflict value acts as override (scheduler.py:964). | NO — our marker says other values are rejected, but import stores them | Behavioural | battery write | Default conflict differs (defer vs override). Close the import gap. |
| PRI-02 | priorities · Entries on the same target | Winner: the incumbent, else the first gate-passing entry by priority and age, else the top active entry (scheduler.py:755-802). | yes | Behavioural | battery write | MB arbitrates across ticks; DC only within one tick. |
| PRI-03 | priorities · Entries on the same target | The incumbent keeps the gateway; a higher priority never pre-empts, whatever its conflict setting (scheduler.py:779-784). | yes | Behavioural | battery write | MB is more predictable; DC's default override pre-empts (#196). |
| PRI-06 | priorities · Entries on the same target | Losers are logged skipped once per contest and can still win later in the same window (scheduler.py:804-829). | yes | Behavioural | battery write | MB lets a loser run once the holder ends; DC gives up for the occurrence. |
| PRI-07 | priorities · Manual control | Foreign = the handler is active with a command this target key does not own (any manual, MQTT, REST, other-target schedule or Run now); checked for force actions only (scheduler.py:959-963). | yes | Behavioural | battery write | MB reads live handler state; DC reads dispatches rows. |
| PRI-08 | priorities · Manual control | defer and wait behave the same: leave the command, drop stale ownership, log deferred once (scheduler.py:964-980). | yes | Behavioural | battery write | MB's wait is an alias of defer. Owner: give them different meanings or merge them? |
| PRI-10 | priorities · Manual control | With defer or wait, the entry dispatches once the manual command ends, if the window is still open and not completed (scheduler.py:937-953, 970-980). | yes | Behavioural | battery write | In MB, defer resumes; in DC, defer gives up for the day. |
| PRI-13 | priorities · Entries on different targets | Arbitration is per (target_type, target_id), so Site and Gateway entries on one gateway see each other as manual control (scheduler.py:686-692, 960-980). | yes | Behavioural | battery write | DC arbitrates per physical gateway, which looks better. |
| PRI-14 | priorities · Who sent what (requester) | Sends carry requester schedule:<id>, recorded on the control log (by=) and on dispatch_outcomes (scheduler.py:1208-1218, command_handler.py:330, 532). | yes | Behavioural | recorded outcome | MB records more; in both bridges Run now and scheduled sends share the same id. |
| OUT-01 | outcomes · Activity Log results | Create, update (with changed fields), enable, disable, delete and import (as created 'imported') are logged (schedules_api.py:323, 486, 639, 656). | yes | Behavioural | recorded outcome | MB is better. Our marker cites helpers (203-260) rather than the routes. |
| OUT-03 | outcomes · Activity Log results | The fired chip matches nothing because dispatches log ok; the log loads 40 rows (schedule_tab.js:290, 322). | yes | Behavioural | display | DC is better; MB's filter chips need fixing (ok, failed, skipped are missing). |
| OUT-04 | outcomes · Retries | Failed force sends retry after 15 s, 30 s, then every 60 s; the first failure and the window-end failure are notified (scheduler.py:61, 1232-1253). | yes | Behavioural | battery write | MB retries sooner and notifies. |
| OUT-05 | outcomes · Retries | A successful retry is logged ok with 'recovered after N failed attempt(s)' and notified (scheduler.py:1086-1093). | yes | Behavioural | recorded outcome | MB is better. |
| OUT-13 | outcomes · The dispatch record | The handler writes a dispatch_outcomes row for every battery command from any source, sent or failed, and moves it to released, release_unverified or superseded (command_handler.py:526-535, 648-676). | yes | Behavioural | recorded outcome | MB is fuller. |
| OUT-18 | outcomes · Notifications | Alerts are sent on first failure, window-end failure, setpoint lost, not_effective and recovery, through the entry's Notify action (scheduler.py:1250, 843, 1294, 1090). | yes | Behavioural | device/HA write | MB is better (DC #172). |
| OUT-19 | outcomes · Run now and Stop | Stop shows when active_now (enabled, window open, always for Always entries), whether or not anything is running; bridge clock (schedule.html:407-414, schedules_api.py:278). | yes | Behavioural | display | DC's 'fired this occurrence' check looks better. |
| OUT-20 | outcomes · Run now | Run now checks entry conditions per gateway unless force=true, sends with the entry's duration and takes no ownership (scheduler.py:549-608). | yes | Behavioural | battery write | MB respects conditions. Owner: an un-owned Run now command cannot be stopped from the Stop button (see OUT-24). |
| OUT-24 | outcomes · Stop | Stop releases every owned gateway via its own handler, using the release policy; runs exit HA actions, logs stopped and blocks re-fire (scheduler.py:610-659). | yes | Behavioural | battery write | MB is better on multi-gateway; but MB Stop does not release a Run now command, which DC's Stop does. |
| OUT-25 | outcomes · Stop | Stop with nothing owned returns not_active but blocks the rest of the window on every target gateway (scheduler.py:650-659). | yes | Behavioural | battery write | MB matches what the user expects from Stop. |
| CLK-01 | restarts-and-clock · The clock | The engine uses naive datetime.now(), the bridge's local clock, for every gateway (scheduler.py:401). | yes | Behavioural | battery write | Same result when the container TZ is right; DC follows each aGate's clock. |
| CLK-02 | restarts-and-clock · The clock | The UI previews use the same bridge clock as the tick, with a zone label (schedule.html:53, 503; schedule_tab.js:867-884). | yes | Behavioural | display | MB is consistent; DC mixes clocks (#161). |
| CLK-03 | restarts-and-clock · The clock | The timezone is recorded at install and compared at every start; a mismatch or UTC is flagged for confirmation (clock.py:96-150, 175-217). | yes | Behavioural | display | MB is better. |
| CLK-04 | restarts-and-clock · Daylight saving | Activation runs on naive wall time: a window in the skipped hour can be missed, and one in the repeated hour re-fires (scheduler.py:182-191, 129-138). | yes | Behavioural | battery write | DC's date keys stop a daily re-fire; MB can dispatch twice on the fall-back night. |
| RST-01 | restarts-and-clock · Bridge restart | Graceful shutdown makes each gateway's handler release its own active command, using the fast release (command_handler.py:1057-1067, 701-707). | yes | Behavioural | battery write | MB is better (DC #192). |
| RST-02 | restarts-and-clock · Bridge restart | On gateway start, a persisted active command is reset and its reversion timers cleared (instance.py:264-270, 391-420). | yes | Behavioural | battery write | MB always releases; DC defaults to notify-only. Owner: offer a policy? |
| RST-03 | restarts-and-clock · Bridge restart | Engine state is in memory only, so an open window dispatches on the first tick, even if it had already fired, been stopped or exited (scheduler.py:406-452). | yes | Behavioural | battery write | DC is better: a stopped or exited run does not restart after a reboot in DC. |
| RST-04 | restarts-and-clock · Bridge restart | Start-up order: gateways (stale release), then the engine, then startup catch-up for gaps (main.py:946-985, connectivity.py:113-140). | yes | Behavioural | battery write | MB is better (DC #199). |
| — | dependencies · A condition is always true when it should not be | Tariff window sensors return False, not None, when unconfigured, so '== 0' passes; grid.connected comes from connection_state; HA unavailable is None (scheduler_sensors.py:358-362, 82-90; ha.py:55). | yes | Behavioural | battery write | DC's None (fail closed) looks better for the window flags. |
| — | dependencies · An action is refused or does nothing | A refused or failed force is logged failed and retried; mode and reserve writes are logged ok without checking the result (scheduler.py:1041-1043). | NO — the doc says retries run every 60 s (they run at 15 s, 30 s, then 60 s) and that HA actions show 'ok' (they log 'ha_action') | Behavioural | recorded outcome | MB is better for forces; both miss failed mode writes. |
| — | dependencies · An entry never fires | An Always entry fires once per bridge run; _expired and idempotent ownership never clear while the window stays open (scheduler.py:937-938, 984-989). | yes | Behavioural | battery write | DC: once ever (#190). Both need a re-arm rule. |
| — | dependencies · Dependency matrix | Force commands need a running gateway and a trusted rating per direction; reserve and mode writes are sent (command_handler.py:237-241, 433). | yes | Behavioural | battery write | MB gates on the trusted rating and runs reserve; DC never runs reserve (#129). |
| — | examples · 1. Evening top-up | Weekday window 16:00-17:00, Force Charge 2000 W, SoC gate, stop at 90 %; all supported by the form. | yes | Implementation variance | | Same intent; DC uses a different field layout (#160). |
| — | examples · 2. Evening peak discharge until the minimum SoC | A daily 17:00-21:00 window; exit releases and marks the window completed; gated entries recheck until 21:00 (scheduler.py:896-918). | yes | Implementation variance | | Same outcome; DC needs 17:00 + 240 min. |
| — | examples · 4. Storm forecast: fill the battery | weather.code and weather.condition are in the catalog; 'like thunder*' matches 'Thunderstorm' case-insensitively (scheduler_sensors.py:877-881, scheduler_conditions.py:253-255). | yes | Behavioural | battery write | The MB recipe works as written; on DC it cannot be built as written. |
| — | examples · 5. Peak-demand window | The window flag reads False when undefined, so the gate fails; at window end with an active but gated sibling, the force is not released (EXT-05). | yes | Behavioural | battery write | Both have an overlap defect, in opposite directions (DC #189, MB EXT-05). |
