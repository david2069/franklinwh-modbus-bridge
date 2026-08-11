# Automations: condition dwell + HA-entity actions — design

**Status:** scoped, not built. **Filed:** 2026-08-11.

## Framing — from "battery scheduler" to "Automations Scheduler"

Scheduler v2 has grown a full trigger + condition-tree + gate model, and (this
session) reads Home Assistant entities as condition inputs. Adding **HA-entity
actions** finishes the turn: it becomes a general **Automations Scheduler** whose
*differentiator* is native battery dispatch over Modbus, plus reach into HA for
everything else. This mirrors FWHAI's Automation Builder (duration threshold +
Action Execution Pipeline with switches/selects) — except FWHAI hardcodes those
actions because they're fixed Cloud-API endpoints, whereas we drive **any**
exposed HA entity generically (no hardcoding). See [[feedback-ha-as-integration-hub]],
[[project-multichannel-controls-backlog]].

Two capabilities, independent, shippable separately.

---

## A. Condition dwell / duration ("must hold for HH:MM:SS")

FWHAI: *"Condition must remain true continuously for this duration before
executing."* Maps onto the Bridge **entry gate**.

- **Model:** add `entry_hold_s INTEGER NOT NULL DEFAULT 0` to `schedules`.
- **Engine:** in the reconcile loop track `cond_true_since[schedule_id]`:
  - entry conditions pass → stamp `now` if unset; fire only when
    `now - since >= entry_hold_s`.
  - conditions fail → clear the stamp (timer resets).
  - Same idea as Home Assistant's `for:`.
- **State:** in-memory (a dict on the engine). On Bridge restart the timer
  restarts — the *safe* direction (never fires early).
- **Caveat:** tick is ~15 s, so dwell resolution is ~15 s. Surface in the UI
  ("checked every ~15 s"). Sub-15 s dwell ≈ one tick.
- **UI:** one `HH:MM:SS` field under the entry conditions (0 = off).
- **Effort:** small.

---

## B. HA-entity actions (switch / select / …)

We already hold a **live HA connection** per instance, so we can call HA services
to control entities the user has exposed.

### Decisions (locked with user, 2026-08-11)
- **Incremental model → then pipeline.** v1: keep the existing single battery
  action, add an ordered `ha_actions[]` that runs when the schedule *fires*.
  Later (v3): unify into ordered `actions[]` (`battery.*` | `ha.*`) with the
  Add-Action/reorder UX from FWHAI.
- **One-shot, edge-triggered.** HA actions run once at the fire edge; no
  automatic revert. Battery actions keep their windowed hold/revert (that's the
  battery nature). Revert = an explicit exit action, deferred to v2. Rationale:
  matches how HA/FWHAI automations behave; don't fight HA's own state ownership.

### Mechanism — HA service call
Use **REST** `POST {base_url}/api/services/{domain}/{service}` with
`{"entity_id": ...}` (+ option/value) and the bearer token. Chosen over WS
`call_service` because it needs no request/response id-correlation (the WS loop
today only consumes events). Add `HaInstance.call_service(domain, service,
entity_id, data)` (httpx, returns ok/last_error) and
`HaRegistry.call_service(instance_id, entity_id, service, data)`.

Domain → service table (derive domain from `entity_id`):

| Domain | Control | Service | Payload |
|---|---|---|---|
| `switch`, `input_boolean`, `light` | on/off/toggle | `turn_on` / `turn_off` / `toggle` | — |
| `select`, `input_select` | pick option | `select_option` | `{option}` |
| `number`, `input_number` | set value | `set_value` | `{value}` |
| `button` | press | `press` | — |
| `scene` / `script` | run | `turn_on` | — |

**Select options come for free** — we already cache `attributes.options` in the
entity state, so the action editor shows the real option list. Switches get
on/off/toggle.

### Data model (v1)
`schedules.ha_actions` = JSON list (new column, migration), e.g.
```json
[
  {"instance_id": "ha_xxx", "entity_id": "switch.pool_pump", "service": "turn_on"},
  {"instance_id": "ha_xxx", "entity_id": "input_select.mode", "service": "select_option", "data": {"option": "Eco"}}
]
```
Executor (on fire, after the battery action): iterate in order, call
`registry.call_service(...)`, audit each to the Activity Log
(`ha_action` / entity / result), continue-on-error (log failures).

### UI
Under the action section, an **"HA Actions"** list: **+ Add HA action** → pick an
entity (picker restricted to **controllable domains among exposed entities**,
reusing the searchable combobox), then a control that adapts to the domain
(on/off/toggle for switch-like; an option dropdown for select-like sourced from
`attributes.options`; a number input for number-like). Row reorder/remove.

### Safety
- Outbound control to third-party devices → restrict the picker to exposed +
  controllable entities.
- Audit every call (entity, service, payload, result) to the Activity Log.
- HA offline → action fails, logged, doesn't block the battery action.
- Keep the "great power" warning.

### Effort
Medium: HA `call_service` infra + `ha_actions[]` model/executor + audit + action
editor + tests + live verify against a real HA switch/select.

---

## Phasing
1. **Dwell (A)** — small, self-contained, high value.
2. **HA actions v1** — `call_service` infra + `ha_actions[]` one-shot on fire
   (switch/select/number/button) + editor + Activity-Log audit. ← core ask.
3. **Unified pipeline** — `action` → ordered `actions[]` (battery + HA),
   Add-Action/reorder UX (FWHAI parity).
4. **v2 semantics** — optional on-exit HA action (revert), more domains
   (climate/light attributes, scenes/scripts).

## Open items
- Which controllable domains to include in v1 (proposed: switch, input_boolean,
  light, select, input_select, number, input_number, button).
- Does the action picker draw from **exposed** entities only, or all controllable
  entities (exposing is a read concept; acting is explicit)? Proposed: exposed
  only, for one coherent allowlist.
- Rate-limiting / debounce on repeated fires (a windowed trigger could re-fire
  each tick) — ensure one-shot means once per activation edge, not per tick.
