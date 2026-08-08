# Multiple HA Instances → One Bridge: HA Entity Access — Design (Backlog)

**Status:** Backlog / design-for-review — **not scheduled.** Scoped 2026-08-08
against the current codebase. Nothing built yet.

**One-line goal:** let the Bridge connect to **multiple Home Assistant instances**
(many-to-one, one flagged default) and expose their entities as **Automation
condition sensors** — so pricing, home loads, weather, and any third-party data
already in HA can drive Bridge automations, with no per-provider Bridge code.

---

## 1. Why (and why it fits)

- **HA-as-integration-hub** ([[feedback-ha-as-integration-hub]]): most third-party
  data (Amber/Octopus pricing, smart-plug loads, weather) already has mature HA
  integrations. Reading those entities into the Bridge's Automations is the
  primary enabler that de-risks the pricing, home-loads, and RateRudder backlogs
  in one move.
- **Multiple instances = flexibility** (owner req 2026-08-08): dev/prod setups
  have several HA containers (Modbus / Energipays / FWHAI); one HA may own pricing,
  another loads, another weather. The Bridge composes across them.
- **It mirrors an architecture the Bridge ALREADY has.** Multiple aGate
  **gateways → one Bridge** via `GatewayRegistry` is the same "many-to-one" shape.
  An `HaRegistry` of HA-instance clients is the exact parallel — not a new
  paradigm.

## 2. Current state (verified)

- The Bridge talks OUTBOUND to HA today only via **MQTT discovery** (publishes
  aGate entities to one broker). This design is the INBOUND direction: read
  entity STATES from N HA instances via HA's own API — a different mechanism.
- No HA-client code, no HA-instance config/table, no inbound HA auth exist.
- The scheduler sensor registry (`gateway/scheduler_sensors.py` `snapshot()` +
  `sensor_catalog()`) and `/api/sensors` + the Automation Builder dropdown are the
  insertion points for a second, HA-sourced condition namespace.

## 3. Architecture

- **`HaRegistry`** (parallels `GatewayRegistry`): holds N `HaInstance` clients,
  each with config `{id, name, base_url, token, enabled, default}`. Exactly one is
  the **default/primary** (e.g. the co-hosted addon HA).
- **`HaInstance` client:** connect to HA's **WebSocket API**, authenticate, and
  `subscribe_events` to `state_changed` → maintain a live **entity-state cache**;
  REST (`/api/states`) for the initial bulk read + reconnection resync. Auto-
  reconnect with backoff (like the poller).
- **Auth per instance:** a **long-lived access token** (external instances); the
  **co-hosted addon** instance can use the **Supervisor token** (no user token
  needed). Tokens stored in the DB — at minimum access-controlled; consider
  encrypting at rest with the app secret_key (ties to the multi-user secret_key
  work, [[project-multiuser-pwa-wizard-backlog]]).
- **Entity namespacing:** condition sensors are `ha:<instance_id>:<entity_id>`
  (e.g. `ha:pricing:sensor.amber_general_price`). Guarantees no collision across
  instances and with the aGate `battery.*`/`energy.*` sensors.
- **Value coercion:** HA states are strings; coerce to number/bool where possible
  (the condition evaluator already coerces + fails closed on None/unavailable, so
  `unavailable`/`unknown` HA states → None → fail closed — safe).

## 4. Consumption in Automations

- The HA entities become a **second condition-source namespace** alongside the
  aGate sensors. `snapshot()` gains HA entity values (merged from the registry's
  cache — the registry is the stateful source; the pure `snapshot(points, now)`
  stays for aGate points, with HA values layered in by the caller, or a companion
  `ha_snapshot()`), surfaced in `/api/sensors` and the Automation Builder dropdown
  — **grouped** (aGate sensors vs each HA instance) so the (potentially large)
  list stays usable.
- Optional later: a friendly attribute/label passthrough (units, device_class)
  from HA so the builder shows sensible operators/values.

## 5. Data model

```sql
CREATE TABLE ha_instances (
    id         TEXT PRIMARY KEY,        -- ha_<uuid8>
    name       TEXT NOT NULL,
    base_url   TEXT NOT NULL,           -- http(s)://host:8123  (or supervisor)
    token      TEXT,                    -- long-lived token; NULL = use Supervisor
    is_default INTEGER NOT NULL DEFAULT 0,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at REAL, updated_at REAL
);
```

Optional `ha_entity_prefs` later (which entities to surface, friendly labels) if
exposing *every* HA entity is too noisy. CRUD mirrors the `services`/`gateways`
pattern. Connection state kept in memory (like gateways).

## 6. REST + UI

- `/api/ha/instances` CRUD (add/edit/remove instances, set default, test
  connection) — REST-gateway-first ([[project-rest-gateway]]).
- `/api/sensors?include=ha` returns the HA-sourced sensors with live values.
- An **"HA Instances"** settings tab (add URL + token, test, mark default),
  modeled on the existing multi-gateway UI; the Automation Builder dropdown gains
  the grouped HA sensors.

## 7. Phasing

1. **One instance, read-only:** `HaInstance` (REST bulk + WS state_changed) →
   entity cache → expose as `ha:<inst>:<entity>` condition sensors in
   `/api/sensors` + the builder. Prove pricing/load gating end to end.
2. **N instances + default:** `HaRegistry`, `ha_instances` table + CRUD + config
   UI, per-instance connection state.
3. **Ergonomics:** grouped dropdown, entity-pref filtering, value/unit
   passthrough; token-at-rest encryption.
4. **Enables:** pricing (HA Amber/Octopus entities), home loads / smart circuits,
   RateRudder-style signals — all as HA entities, no per-provider code.

Dev test bed: the existing Modbus / Energipays / FWHAI HA containers exercise the
N-instance path from day one.

## 8. Risks

- **Token security** (inbound now, not just MQTT out) — store carefully; prefer
  Supervisor for the co-hosted instance.
- **WS lifecycle** — reconnect/backoff, resync the cache on reconnect, handle HA
  restarts; treat a disconnected instance's entities as None (fail closed).
- **Entity volume** — a full HA can have hundreds of entities; filter/group so the
  builder stays usable (entity-pref allowlist).
- **State typing** — HA states are strings incl. `unavailable`/`unknown`; coerce +
  fail closed (evaluator already does).
- **Auth interaction** — inbound HA tokens vs the Bridge's own (future) user auth
  are independent; don't conflate.

## 9. Open questions

- Surface ALL HA entities, or an allowlist per instance (likely allowlist for
  usability)?
- WebSocket vs REST-poll for the first slice (WS is the right long-term answer;
  REST-poll is a simpler Phase-1 start).
- Token at rest: plain (DB-access-gated) vs encrypted with the app secret_key
  (depends on multi-user landing first).
- Do we also want to *write* to HA (call services) from Bridge automations later,
  or read-only for now (this design = read-only)?
