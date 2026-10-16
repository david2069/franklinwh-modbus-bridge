# Data out: MQTT to Home Assistant

The bridge publishes each gateway to Home Assistant with **MQTT Discovery**: a retained
config message per entity, then retained state messages as values change. Home Assistant
builds the device and entities from the config messages. No custom integration is
installed in Home Assistant.

Setting it up from the UI is covered in
[Settings → Home Assistant](../user/ui/settings-home-assistant.md); fixing or removing
entities in [Fixing, moving and removing entities](../user/home-assistant-entities.md).

!!! warning "Identity is frozen"
    Everything on this page that names a device or entity (namespace default, device
    identifiers and names, unique_ids, topics, slugs) is **frozen until the purge release**
    (decision D1 in the [Install lifecycle](../install-lifecycle.md) policy). This page
    describes what exists. Don't build on a change to it.

## Names used below

| Placeholder | Meaning | Example |
|---|---|---|
| `<prefix>` | The bridge's **namespace** (Settings → Home Assistant → MQTT Broker → Topic Prefix). `fwh_mb` on new installs; older installs may still use the legacy `franklinwh`. | `fwh_mb` |
| `<short_id>` | The device's topic id. The **default** gateway uses the last 8 characters of the aGate serial, `<serial8>`. **Any other** gateway uses its gateway id, lower-cased, with anything but letters and digits turned into `_`. | `<serial8>`, `demo` |
| `<SERIAL>` | The full aGate serial. | |
| `<slug>` | The entity's slug, for example `battery_soc`. Listed in Settings → Home Assistant → Entities, or `GET /api/mqtt/topics`. | `battery_soc` |
| `<component>` | Home Assistant platform: `sensor`, `binary_sensor`, `select` or `number`. | `sensor` |

The discovery prefix is `homeassistant`, fixed.

## Topics

| Topic | Retained | Payload | Direction |
|---|---|---|---|
| `homeassistant/<component>/<prefix>_<short_id>_<slug>/config` | yes | Discovery JSON (below). Empty = remove the entity. | bridge → HA |
| `<prefix>/<short_id>/<group>/<slug>` | yes | The value as text. `binary_sensor`: `1` / `0`. | bridge → HA |
| `<prefix>/<short_id>/availability` | yes | `online` / `offline` | bridge → HA |
| `<prefix>/<short_id>/control/<slug>/set` | — | The new value as text | HA → bridge |

`<group>` in a state topic is one of `battery`, `capacity`, `power`, `energy`, `solar`,
`status` or `control`. It is a topic folder only; the **publishing groups** in Settings
are a separate thing.

Real topics for the demo gateway (`short_id` `demo`):

```text
homeassistant/sensor/fwh_mb_demo_battery_soc/config
fwh_mb/demo/battery/battery_soc
fwh_mb/demo/power/grid_power_kw
homeassistant/binary_sensor/fwh_mb_demo_wset_enabled/config
fwh_mb/demo/status/wset_enabled
homeassistant/select/fwh_mb_demo_battery_command/config
fwh_mb/demo/control/battery_command
fwh_mb/demo/control/battery_command/set
fwh_mb/demo/availability
```

For the default gateway the same entities sit under `fwh_mb/<serial8>/…` and
`homeassistant/sensor/fwh_mb_<serial8>_battery_soc/config`.

To see everything a bridge publishes, with each topic:

```bash
curl -s -b jar.txt http://192.0.2.10:8100/api/mqtt/topics
```

It returns one block per publishing gateway (`gateways[]`, each with `short_id` and
`topics[]`: slug, name, `ha_type`, source point, state, discovery and command topics).

## Discovery payload

Built in `publish/mqtt_publisher.py`, `build_discovery_payload()`. For the default
gateway's State of Charge:

```json
{
  "unique_id": "fwh_mb_<serial8>_battery_soc",
  "name": "State of Charge",
  "state_topic": "fwh_mb/<serial8>/battery/battery_soc",
  "availability_topic": "fwh_mb/<serial8>/availability",
  "device": {
    "identifiers": ["fwh_mb_modbus_<SERIAL>"],
    "name": "FranklinWH Modbus <serial8>",
    "model": "aGate",
    "manufacturer": "FranklinWH Technologies Co., Ltd",
    "serial_number": "<SERIAL>",
    "sw_version": "Modbus Bridge v0.2.9",
    "hw_version": "aGate <aGate firmware>"
  },
  "unit_of_measurement": "%",
  "device_class": "battery",
  "state_class": "measurement",
  "icon": "mdi:battery"
}
```

A control entity adds `command_topic` (and `options` for a select, `min` / `max` / `step`
for a number):

```json
{
  "unique_id": "fwh_mb_<serial8>_battery_command",
  "name": "Battery Command",
  "state_topic": "fwh_mb/<serial8>/control/battery_command",
  "command_topic": "fwh_mb/<serial8>/control/battery_command/set",
  "options": ["Not Active", "Force Charge", "Force Discharge", "Force Standby", "Release"],
  "...": "availability_topic and device as above"
}
```

## Device and unique_id scheme

| Item | Default gateway | Any other gateway |
|---|---|---|
| unique_id | `<prefix>_<serial8>_<slug>` | `<prefix>_<short_id>_<slug>` |
| Device identifier | `<prefix>_modbus_<SERIAL>` | `<prefix>_modbus_<gateway id>_<serial>`, lower-cased and slugged |
| Device name | `FranklinWH Modbus <serial8>` | `FranklinWH Modbus <serial8>`; a mock is `FranklinWH Modbus <short_id> (mock)` |
| `sw_version` | `Modbus Bridge v<version>` | same |

`modbus` is in both the identifier and the name so Home Assistant never merges this
bridge's device with another FranklinWH integration's device for the same aGate. The
reasons are in [Home Assistant device identity](https://github.com/david2069/franklinwh-modbus-bridge/blob/main/docs/ha-device-identity.md).

The MQTT client id is `franklinwh_bridge_<8 hex>`, unique per install.

## When things are published and removed

| Event | What the bridge sends |
|---|---|
| Connect to the broker | `online` on each device's availability topic, then every discovery config (retained), then state as samples arrive. |
| Each poll | Retained state for each entity whose value was read. A value read as unknown, on entities that reset, is sent as `None`. A value not read this poll is not re-sent. |
| A battery command changes state | The command entities' state at once, without waiting for the next poll. |
| **MQTT publishing** switch off | `offline` on each availability topic, then disconnects. Home Assistant shows the entities as unavailable; they are not removed. |
| Bridge shutdown or restart | Disconnects **without** sending `offline` (see the note below). |
| A gateway's **Publish this gateway** switch off | An empty retained config for each of its entities (Home Assistant removes them) and `offline` on its availability topic. |
| A publishing group disabled | Its entities' configs are cleared, unless another enabled group holds them. |
| **Unpublish** (`POST /api/mqtt/unpublish`) | Empty retained configs for every entity and an empty availability. Stays unpublished across restarts until **Republish** (`POST /api/mqtt/publish`). |
| **Change namespace** | Removes this bridge's own retained configs under the old prefix (it reads them first and leaves another producer's alone), then publishes under the new one. |

!!! note "Availability only goes offline from the switch"
    In 0.2.9 the bridge registers no MQTT *last will*, and its shutdown doesn't publish
    `offline`. So when the bridge stops, restarts or loses power, the retained
    availability stays `online` and Home Assistant keeps showing the last values. Watch
    the values' age, or the bridge's own `/api/status`, if you need to detect that.

!!! note "TLS"
    The MQTT Broker card stores a **TLS Mode**, but in 0.2.9 the publisher connects
    without TLS whatever it says. Keep the broker on a trusted network.

## Command topics

The bridge subscribes to `<prefix>/<short_id>/control/+/set` for each gateway it
publishes controls for and passes the payload to that gateway's command handler: the same handler, slugs and
checks as `POST /api/gateways/{gw_id}/command` (see the
[slug table](rest-api.md#send-a-battery-command)). The Activity and control logs record
these as requested by `mqtt`.

```bash
# Force charge the demo gateway at 2 kW from any MQTT client on the broker:
mosquitto_pub -h <broker> -t fwh_mb/demo/control/battery_command_power/set -m 2000
mosquitto_pub -h <broker> -t fwh_mb/demo/control/battery_command/set -m "Force Charge"
mosquitto_pub -h <broker> -t fwh_mb/demo/control/battery_command/set -m Release
```

Only the bridge's own namespace is obeyed; a topic for an unknown `short_id` is logged and
ignored.

While the **Control** publishing group is off, the bridge unsubscribes from the command
topics and refuses any command that still arrives (logged as *"Command refused, its
publishing group is off"*). It subscribes again when Control is switched back on.

!!! warning "The broker is the security boundary"
    Command topics carry no bridge login. Anything that can publish to the broker can
    command the battery. Protect the broker with a username and password, and don't expose
    it outside your network, or turn off the **Control** publishing group if nothing
    should command the battery over MQTT.

## Reading the data without Home Assistant

The state topics are plain retained MQTT, so any client can read them:

```bash
mosquitto_sub -h <broker> -v -t 'fwh_mb/demo/#'
```

For history, the REST API has more than MQTT carries: `GET /api/metrics`,
`GET /api/point-history/series` and `GET /api/energy/totals` (see the
[API reference](api-reference.md)).
