# Settings → Home Assistant

`?tab=settings&section=home-assistant` — how the bridge publishes entities to Home
Assistant over MQTT, and which Home Assistant instances it reads from.

![Home Assistant settings](../../images/ui/settings-home-assistant.png)

Cards on this page:

1. [Publish to Home Assistant](#publish-to-home-assistant)
2. [Notifications](#notifications)
3. [MQTT Broker](#mqtt-broker)
4. [Home Assistant instances](#home-assistant-instances)
5. [Publishing Groups](#publishing-groups)
6. [Entities](#entities)

How the bridge's entities are named, moved and cleaned up is covered in
[Fixing, moving and removing entities](../home-assistant-entities.md).

## Publish to Home Assistant

Switches that decide which gateways' entities reach Home Assistant through MQTT discovery.
They **take effect at once**.

| Switch | Default | On | Off |
|---|---|---|---|
| **MQTT publishing** (all gateways) | On | The bridge connects to the broker and publishes. | Nothing is published for any gateway. Entities already in Home Assistant show **unavailable**; they are not removed. Use [Unpublish](#republish-and-unpublish) to remove them. |
| One switch per gateway (*demo* and *disabled* badges where they apply) | On for real gateways; off for the demo created by the setup wizard | That gateway's device and entities are published. | **Its entities are removed from Home Assistant.** Switch it on again to re-create them. |

With **MQTT publishing** off, the card says so: the per-gateway switches have no effect
until it is on again.

The per-gateway switch is the same setting as **Publish this gateway to Home Assistant**
in the [Edit Gateway](settings-site-gateways.md#edit-gateway) dialog.

## Notifications

Notifications to your phone and to Home Assistant's own notification panel (the bell).
Phones use the [Home Assistant Companion app](https://companion.home-assistant.io/); a
phone's notifications can carry buttons you tap to answer.

**Everything goes from the bridge to Home Assistant.** A button tap comes back over the
bridge's own signed-in connection to Home Assistant. You don't need a webhook, YAML, an
open port or a URL for the bridge.

### Push notifications

The one switch for all of it, **on** by default.

| | On | Off |
|---|---|---|
| Phones and other devices below | Receive notifications | Get nothing. **Test** on a device's row still sends, to check it. |
| Home Assistant's notification panel | Gets critical alerts and, if switched on, status notices | Gets nothing. Critical alerts reach only the bridge's own window. |

It is the same setting the bridge has always used for critical alerts to Home Assistant,
so an update keeps your choice.

### Status notices

Under the switch, its own setting, **on** by default. While both are on, the bridge
posts a notification in Home Assistant when:

| When | Notification | Says |
|---|---|---|
| The bridge has started | *FranklinWH Modbus Bridge: started* | The version, how many gateways it is polling and how often, and any that aren't polling. |
| The bridge is shutting down | *FranklinWH Modbus Bridge: stopped* | Polling stops and any battery command is released. |
| A gateway is paused with **Stop** | *FranklinWH Modbus Bridge: <gateway> paused* | Who paused it. It stays paused across restarts until someone presses **Start**. |
| A paused gateway polls again (**Start**, or **Restart** of a paused gateway) | *FranklinWH Modbus Bridge: <gateway> polling* | Who started it. |

The bridge's notice replaces its previous one, and each gateway has one of its own, so
they don't pile up. Each notice is sent once and is also a line in the
[app log](logs.md), sent or not, with the reason when it wasn't sent: status notices
off, push notifications off, no Home Assistant is set up, or Home Assistant didn't
answer.

### Notifications check

Answers "would a notification reach me?". Each line is **Pass**, **Fail** or **Not set
up**, with the fix. **Check again** runs it again.

| Line | Passes when |
|---|---|
| Push notifications switch | The switch is on. |
| Default HA instance | The [default Home Assistant instance](#home-assistant-instances) answers and accepts its token. |
| Button taps listener: *name* | The bridge is connected to that instance and listening for taps. One line per enabled instance. |
| Phones (mobile_app_*) | At least one enabled phone is set up. Only phones show buttons. |
| Last Test with buttons | The last **Test with buttons** was answered on the phone. |

### Companion devices

A device is one of Home Assistant's `notify.*` services, with a name you choose. A phone
with the Companion app has a service called `notify.mobile_app_<phone>`: that is the
usual choice, and the only kind that shows buttons. Any other service (an email, a TV)
gets the text only.

1. Install the [Companion app](https://companion.home-assistant.io/) on your phone, sign in
   to your Home Assistant and allow notifications.
2. Press **+ Add device**. It needs a [Home Assistant instance](#home-assistant-instances);
   as an add-on, *This Home Assistant* is there already.
3. Pick the **HA instance**.
4. Press **Discover** next to **Notify service**. It lists every `notify.*` service on that
   Home Assistant, phones first: the name Home Assistant gives the device, its
   `notify.…` service, and when Home Assistant last saw it. A service ending `_2` that
   repeats another is marked *duplicate of …*: pick the one seen most recently. **filter
   services…** finds yours.
5. Pick one. Its name becomes the **Alias** (change it if you like) and its **Type**
   (phone, tablet, TV, speaker or other) is guessed; change it if it's wrong.
6. Press **Send test** to check the device receives it, before saving.
7. **Save**.

Each device's row has an on/off switch, an icon for its type, its alias, and its service
and Home Assistant instance, then:

- **Edit:** change the alias, type, instance or service.
- **Test:** one plain message, sent even while Push notifications is off.
- **Test with buttons** (phones only): "Test with buttons" with **Yes** and **No**. Tap one
  within 2 minutes; the row then says *Round trip works: you tapped Yes*, or *No reply
  within 120 s*. It needs Push notifications on.
- **×** removes it.

**Broadcast** sends a title and message of your own to every enabled device.

Removing a Home Assistant instance removes its devices too.

### Recent notifications

The last 20 notifications sent and taps received, newest first, with how each went.
Notifications from alerts, triggers and schedules come in later releases.

## MQTT Broker

The connection to the MQTT broker that Home Assistant listens to.

**Depends on:** an MQTT broker. On the Home Assistant add-on, install the
**Mosquitto broker** app. The bridge then configures itself from Home Assistant's MQTT
service (the card says *Configured automatically from Home Assistant's MQTT service*). If no
broker is installed yet, it says *Waiting for a broker* with a link to install one; the
bridge connects as soon as it is running. With Docker or a Python install, enter the broker's
details here, or set `MQTT_HOST`. MQTT is optional there: with no broker set, the card says
*No MQTT broker set* and nothing is published to Home Assistant. For a broker on the same
machine, enter `127.0.0.1`: `localhost`, the field's default, counts as not set.

| Field | Meaning |
|---|---|
| Connection | *Connected*, *Disconnected*, *Disabled* (MQTT publishing off), or *No broker* (none set; Docker and Python only). |
| Broker Host, Port | Where the broker is. Default `localhost`, `1883`. |
| Username | Or *(anonymous)*. |
| TLS Mode | *off*, *tls*, or *tls_insecure* (TLS without checking the certificate). |
| Topic Prefix | The bridge's **namespace**, default `fwh_mb`. Changed with **Change namespace…**. |
| Client ID | The bridge's MQTT client ID. |
| Discovery Prefix | `homeassistant`. Fixed; it must match Home Assistant's. |
| Messages Sent | Since the bridge started. |
| Discovery Published | *Yes*, *No*, or *Unpublished — Republish to restore*. |
| Entity Count | Entities the bridge publishes. |

Buttons: **Edit**, **Republish**, **Unpublish**, **Dashboard card**.

### Editing the broker

![MQTT broker edit form](../../images/ui/mqtt-broker-edit.png)

| Field | Notes |
|---|---|
| Host | Broker hostname or IP. |
| Port | 1–65535. |
| Username | Empty = anonymous. |
| Password | Leave empty to keep the stored one. |
| TLS Mode | Off, TLS, TLS (insecure). |
| Topic Prefix | Read-only here. Use **Change namespace…**. |
| Discovery Prefix | Read-only. |

A field set by an environment variable is read-only, with the variable's name under it:
change it where the bridge runs, then restart.

**Test Connection** tries the settings without saving. **Save & Reconnect** saves and
reconnects. **Cancel** discards.

### Change namespace

![Change MQTT namespace](../../images/ui/change-namespace.png)

The namespace prefixes every topic, entity unique ID and device identifier the bridge
publishes. A namespace of its own keeps this bridge's entities apart from other FranklinWH
integrations on the same Home Assistant.

| Choice | Meaning |
|---|---|
| `fwh_mb` | Modbus Bridge (recommended, the default for new installs). |
| `franklinwh` | Legacy: shared with other FranklinWH integrations. |
| Custom | `fwh_` plus 2–8 lowercase letters or digits, for example `fwh_home`. `fwh_ai`, `fwh_lb` and `fwhai` are reserved for other producers. |

If entities have already been published, the dialog explains what happens:

- Home Assistant creates **new entities and a new device** under the new namespace.
- Dashboards, automations and scripts using the old entity IDs need updating.
- Whether Home Assistant carries the old entities' history over is **not established** —
  assume it doesn't.
- Only entities **this bridge** published are removed from the old namespace. Another
  integration's entities there are left alone.

The button reads **Change and re-create entities** (or **Change** if nothing has been
published). Afterwards the dialog reports how many old entities were removed and how many
belonged to others. The discovery prefix does not change.

### Republish and Unpublish

![Republish confirmation](../../images/ui/republish-confirm.png)

**Republish** re-announces every entity configuration to Home Assistant. Use it if Home
Assistant has lost entities, or after renaming. Safe at any time.

![Unpublish confirmation](../../images/ui/unpublish-confirm.png)

**Unpublish** sends empty discovery messages, so Home Assistant **removes all of the
bridge's entities**. Use it only when decommissioning the bridge or cleaning up stale
entities. The entities **stay removed until you press Republish**.

### Dashboard card

Opens the [Home Assistant page](home-assistant-page.md#dashboard-card) with the dashboard
card YAML dialog. It needs a Home Assistant instance (below).

## Home Assistant instances

The Home Assistant installations the bridge **reads from**. Their entity states become
`ha:<instance>:<entity>` sensors that schedules can use as conditions, after you expose
them on the [Home Assistant page](home-assistant-page.md). An instance is also used by
**Use Home Assistant's location**, the dashboard card YAML, and HA price entities.

Each instance shows: name, badges (*default*, *disabled*, *auto*), URL, token (*set*,
*none* or *Supervisor*), status (*connected · live* over WebSocket, *connected · poll*, or
*offline* with the last error) and its entity count.

On the add-on, the Home Assistant it runs in is added automatically (*auto*) through the
Supervisor. It has no URL or token and cannot be deleted.

Step by step, with creating the token in Home Assistant and sending notifications:
[Home Assistant setup: instance & notifications](../ha-setup.md).

**+ Add Home Assistant** (or **Edit** on an instance):

![Add Home Assistant form](../../images/ui/ha-instance-form.png)

| Field | Notes |
|---|---|
| Name | For example *Home*. Used in sensor names. |
| Base URL | For example `http://homeassistant.local:8123`. Not shown for an *auto* instance. |
| Long-Lived Access Token | Create one in Home Assistant under your profile → Security. Leave empty to keep the stored one. |
| Default | The instance used when none is named. |
| Enabled | Off stops reading it. |

**Test Connection**, **Save**, **Cancel**. The bin icon deletes an instance.

!!! note
    This card uses the same API as the **HA Entities** feature module. If you switch that
    module off in [Feature Modules](settings-admin.md#feature-modules), the instance list
    cannot load.

## Publishing Groups

Groups decide **which entities** are published over MQTT. Every entity belongs to one or
more groups.

![Publishing group expanded](../../images/ui/publishing-groups-expanded.png)

The built-in groups (marked *(default)*):

| Group | Slug | Entities | Contents |
|---|---|---|---|
| Battery | `battery` | 16 | SoC, SoH, power, charge state |
| Battery Capacity | `capacity` | 4 | Total/available capacity, max charge/discharge rates |
| Power | `power` | 5 | Grid, home load, per-phase active power |
| Energy | `energy` | 6 | Grid import/export, solar, battery energy counters |
| Solar | `solar` | 5 | Solar power and PV inputs |
| Status & Diagnostics | `status` | 41 | Voltages, frequency, temperatures, control status, timers |
| Control | `control` | 8 | Writable entities: operating mode, reserves, battery commands |

(Counts as in 0.2.8.)

- The **Enabled** switch turns a group on or off. **Disabling a group removes its entities
  from Home Assistant.** An entity in several groups stays published while any of its
  groups is enabled. The switch asks for confirmation first, because the change reaches
  Home Assistant at once.
- Every change that adds or removes entities (a switch, members, a new or deleted group,
  a promoted point) writes one line to the app log, with the group, the change and the
  user, e.g. `Publishing groups: group 'battery' enabled → disabled (by admin)`.
- Click a group row to list its members. Tick or untick entities to add or remove them;
  **Filter entities…** searches.
- **+ New Group**: Slug (lowercase, 2–63 characters, letters, digits and underscores),
  Display Name and Description. Custom groups can be deleted with **✕**; the built-in ones
  cannot.

!!! tip
    Switching off the **Control** group removes the writable entities (mode, reserves,
    battery commands) from Home Assistant, and the bridge stops accepting commands on its
    MQTT command topics, so nobody can command the battery through Home Assistant or MQTT.
    The web UI and REST API still can.

## Entities

The full list of entities the bridge publishes, with live values: Slug, Name, Source (the
SunSpec point or register), Type (sensor, number, select …) and Value.

- **Filter entities…** searches.
- **Topics** adds the MQTT state topic of each entity.
- With more than one publishing gateway, a dropdown picks which gateway's list to show.
