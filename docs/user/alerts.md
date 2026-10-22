# Alerts and the bridge's health sensors

The bridge raises an **alert** when something needs you. Examples: a battery command the
aGate refused, an aGate that stopped answering, or live data that stopped arriving.
Alerts are kept in the bridge, so one that happened while nobody was looking is still there
the next time you open it.

## Which alerts

| Alert | When | Severity |
|---|---|---|
| Battery command refused | The aGate rejected a command, or the bridge refused to send it. The message says which. Repeats within 10 minutes count as one. | critical |
| Write ignored by the aGate | The aGate accepted a mode or reserve write, but the value didn't change. | critical |
| aGate unreachable | It hasn't answered for 5 minutes. Clears after 2 minutes back. | critical |
| Live data missing | No reading has worked for over 2 minutes. Clears after 1 good minute. | critical |
| Some live data missing | Registers 15500–15509 (solar, home load, mode, reserves) have been missing for 2 minutes while other readings arrive. | warning |
| A bridge task stopped | One of the bridge's own background tasks (polling, schedules, the reachability check, log writing, alert handling) stopped running or crashed. | critical |

## Where you see them

- **Home Assistant notifications.** Every critical alert also appears in HA's notifications
  (the bell in HA's sidebar), even with no phone set up. When an admin acknowledges the
  alert in the bridge, the HA notification is removed. Dismissing it in HA does **not**
  acknowledge it in the bridge.
  HA notifications are on by default and can be switched off. Then critical alerts reach
  only the bridge.
- **Your phone.** With the Home Assistant Companion app, see
  [Phone, notifications and remote access](phone-and-remote-access.md).

## The bridge's own device in Home Assistant

Besides one device per aGate, the bridge publishes a device for itself, **FranklinWH Modbus
Bridge**, with two diagnostic sensors:

| Sensor | What it shows |
|---|---|
| **Bridge problem** | On while any alert still needs someone, for any aGate, including one that never answered. Its attributes list the open alerts (`level`, `open`, `alerts`). It stays readable even while an aGate or the bridge is down. |
| **Bridge heartbeat** | The time the bridge last checked in. It updates every 60 seconds. If the bridge stops or hangs, the time stops moving. |

## Automations to add in Home Assistant

The bridge can't tell you it has stopped: these two automations in HA can. Check the entity
IDs on the bridge's device page in HA (**Settings → Devices & services → MQTT → FranklinWH
Modbus Bridge**). The IDs below are the usual ones, and the notify service is an example:
use your own phone's.

```yaml
- alias: "FranklinWH bridge stopped checking in"
  triggers:
    - trigger: template
      value_template: >
        {{ now() - as_datetime(states('sensor.franklinwh_modbus_bridge_bridge_heartbeat'))
           > timedelta(minutes=5) }}
  actions:
    - action: notify.mobile_app_your_phone
      data:
        title: "FranklinWH bridge"
        message: "The bridge hasn't checked in for 5 minutes. It may have stopped."

- alias: "FranklinWH bridge problem"
  triggers:
    - trigger: state
      entity_id: binary_sensor.franklinwh_modbus_bridge_bridge_problem
      to: "on"
  actions:
    - action: notify.mobile_app_your_phone
      data:
        title: "FranklinWH bridge"
        message: "The bridge has an alert. Open it to see what to do."
```

A template trigger that uses `now()` is checked every minute, so the first automation fires
within about a minute of the 5 minutes passing.
