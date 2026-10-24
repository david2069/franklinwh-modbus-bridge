# Data in: Home Assistant entities over WebSocket

MQTT carries the bridge's values **to** Home Assistant. The other direction, reading
Home Assistant entities so schedules can use them, goes over Home Assistant's own
**WebSocket API** with a long-lived access token. The code is `gateway/ha.py` (client) and
`api/ha_api.py` (routes).

The user-facing steps, with screenshots, are in
[Home Assistant setup: instance and notifications](../user/ha-setup.md).

## Instances

The bridge can read from several Home Assistant installations. Each is an **instance**:

| Field | Notes |
|---|---|
| `id` | Assigned by the bridge: `ha_<8 hex>`, or `local` for the add-on's own Home Assistant. Used in sensor ids. |
| `name` | Your label, for example *Home*. Used in sensor labels. |
| `base_url` | For example `http://homeassistant.local:8123`. The WebSocket URL is derived from it (`http` → `ws`, `https` → `wss`, plus `/api/websocket`). |
| `token` | A Home Assistant **long-lived access token**. Stored in the bridge's database, never returned by the API (`has_token` says whether one is set). |
| `is_default` | The instance used when none is named (location, dashboard card YAML). |
| `enabled` | Off: not connected. |

On the **add-on**, the Home Assistant it runs in is added automatically as `local`
(*auto* in the UI). It connects through the Supervisor's proxy with the add-on's own
`SUPERVISOR_TOKEN`, so there is no URL or token to enter, and it can't be deleted (disable
it instead).

Manage instances over the API (admin role; module `ha_entities`). The examples use the
cookie jar from [REST API → Sign in](rest-api.md#sign-in):

```bash
B=http://192.0.2.10:8100
# Test without saving: {"connected": true, "entity_count": 87, "last_error": null}
curl -s -b jar.txt -H 'Content-Type: application/json' \
  -d '{"base_url":"http://192.0.2.20:8123","token":"<long-lived token>"}' \
  $B/api/ha/test

# Add it
curl -s -b jar.txt -H 'Content-Type: application/json' \
  -d '{"name":"Home","base_url":"http://192.0.2.20:8123","token":"<long-lived token>","is_default":true}' \
  $B/api/ha/instances

# List, with live status
curl -s -b jar.txt $B/api/ha/instances
```

```json
[{"id": "ha_31ab8c2d", "name": "Home", "base_url": "http://192.0.2.20:8123",
  "is_default": true, "enabled": true, "has_token": true, "managed": null,
  "status": {"connected": true, "transport": "ws", "last_error": null, "entity_count": 87}}]
```

`PATCH /api/ha/instances/{id}` changes fields (a token is only replaced when you send one);
`DELETE` removes it. Every change reloads the connections at once, without a restart.

## The connection

For each enabled instance the bridge:

1. Seeds its cache with REST `GET /api/states`, so values exist straight away.
2. Opens `ws(s)://<base_url>/api/websocket` and authenticates with the token.
3. Sends `get_states` to reseed, then `subscribe_events` for `state_changed`.
4. Applies every `state_changed` event to its cache as it arrives. Values are live, with no
   polling.

If the socket drops or can't connect, it retries with backoff from 1 s up to 30 s, and does
a REST `GET /api/states` between attempts so the cache doesn't go stale. The instance's
status shows the transport:

| Status in the UI | `status` in the API | Meaning |
|---|---|---|
| *connected · live* | `connected: true`, `transport: "ws"` | Subscribed; values update as they change. |
| *connected · poll* | `connected: true`, `transport: "rest"` | The REST seed worked; the WebSocket isn't up (yet). |
| *offline* + error | `connected: false`, `last_error` | Neither works. A wrong token shows Home Assistant's `auth_invalid` message. |

The token needs only what a normal Home Assistant user has: read states, subscribe to
events, call services. Consider a dedicated Home Assistant user for the bridge.

## From entity to condition sensor

The cache holds every entity, but only entities you **expose** become sensors. Exposing
is an allowlist in the bridge; it changes nothing in Home Assistant.

```bash
# Browse (filters: instance, domain, search, exposed; paged)
curl -s -b jar.txt "$B/api/ha/entities?search=away"

# Expose one
curl -s -b jar.txt -H 'Content-Type: application/json' \
  -d '{"instance_id":"ha_31ab8c2d","entity_id":"input_boolean.away_mode","exposed":true}' \
  $B/api/ha/entities/expose
```

An exposed entity appears in `GET /api/sensors` (the condition picker's source) as
`ha:<instance id>:<entity_id>`:

```json
{"id": "ha:ha_31ab8c2d:input_boolean.away_mode", "label": "Home: Away mode",
 "unit": null, "kind": "bool", "value": true, "source": "ha",
 "group": "HA · Home", "instance": "ha_31ab8c2d"}
```

Home Assistant states are strings. The bridge converts them the same way for the picker,
the engine and **Test Verification**:

| Home Assistant state | Becomes | Compare with |
|---|---|---|
| `unavailable`, `unknown`, `none`, empty | no value: **every condition on it fails** | — |
| a number (`"21.5"`) | number | numbers |
| `on`, `true`, `home`, `open`, `locked` | true (= 1) | `1` |
| `off`, `false`, `away`, `closed`, `unlocked` | false (= 0) | `0` |
| anything else | the text | the text, for example `Self-Consumption` |

Failing closed is deliberate: if Home Assistant is down, a gate on its entity is false and
the schedule doesn't act.

The engine reads the cache on each 15-second tick, so a change in Home Assistant reaches
a schedule within one tick.

## Outbound calls: actions and notifications

Schedules can also act **on** Home Assistant. These go over Home Assistant's REST API with
the same token, not over the WebSocket:

| Schedule action | Request the bridge sends | Notes |
|---|---|---|
| Entity action (switch, light, select, number, button, scene, script, input helpers) | `POST /api/services/<domain>/<service>` with `{"entity_id": …, …data}` | Only exposed entities in those domains can be picked. |
| Notify | `POST /api/services/notify/<name>` with `{"title": …, "message": …}` | No `entity_id`. |

The notify targets in the picker come from `GET /api/ha/notify-targets`, which reads each
instance's `GET /api/services` and lists the `notify` domain's services:

```json
{"targets": [
  {"instance_id": "ha_31ab8c2d", "instance_name": "Home", "service": "mobile_app_docs_phone", "label": "mobile_app_docs_phone (Home)"},
  {"instance_id": "ha_31ab8c2d", "instance_name": "Home", "service": "persistent_notification", "label": "persistent_notification (Home)"}
]}
```

!!! warning "`send_message` is listed but doesn't work"
    Home Assistant's newer entity-based `notify.send_message` service is in the list,
    but it needs an `entity_id`, which the bridge doesn't send. Home Assistant answers
    *400 Bad Request* and the Activity Log shows the action as failed. Pick a named
    service such as `mobile_app_<phone>` or `persistent_notification`.

Each call is best-effort and logged in the schedule's Activity Log (*ha_action* or
*failed* with the error). One failed call never stops the battery action or the others.

## Other uses of an instance

| Feature | What it reads |
|---|---|
| **Use Home Assistant's location** (Settings → Site & Gateways) | A one-off WebSocket `get_config` for the latitude and longitude (`POST /api/solar/location/from-ha`) |
| Dashboard card YAML (`GET /api/ha/dashboard-yaml`) | A one-off WebSocket `config/entity_registry/list`, so renamed entities come out right |
| Home Assistant price entities (dynamic pricing) | Exposed sensors, named by their `ha:<instance>:<entity>` id |
