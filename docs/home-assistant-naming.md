# Home Assistant naming (the MQTT namespace)

When you first set up the bridge, it asks you to choose its **Home Assistant
naming**. This page explains what that name is, why you should treat it as
permanent, and what happens with several aGates, several integrations, a
shared MQTT broker, several Home Assistants, and when you remove or reinstall.

All serials and addresses on this page are made up.

## What the name is

The name (the *namespace*, shown in Settings as **Topic Prefix**) is a short
word such as `fwh_mb`. The bridge puts it in front of everything it publishes
to Home Assistant over MQTT:

| What | Example with `fwh_mb`, aGate serial `TESTSERIAL0000ABCD` |
|---|---|
| Entity unique id | `fwh_mb_0000ABCD_battery_soc` |
| MQTT topics (state, commands, availability) | `fwh_mb/0000ABCD/battery/battery_soc`, `fwh_mb/0000ABCD/control/operating_mode/set`, `fwh_mb/0000ABCD/availability` |
| Discovery topic | `homeassistant/sensor/fwh_mb_0000ABCD_battery_soc/config` |
| Device identifier | `fwh_mb_modbus_TESTSERIAL0000ABCD` |
| Device name (no namespace in it) | `FranklinWH Modbus 0000ABCD` |
| Entity id (made by Home Assistant from the names) | typically `sensor.franklinwh_modbus_0000abcd_state_of_charge` |

The device name follows the rule shared by every FranklinWH integration: one
Home Assistant device per integration, per aGate. See
[ha-device-identity.md](ha-device-identity.md).

The choices:

- **`fwh_mb`**: recommended for new installs. No other integration uses it.
- **`franklinwh`**: only to get back the entities of an earlier install that
  used it (installs before 0.2.4 did). Other FranklinWH integrations (FWHAI)
  also use this name, and on a shared Home Assistant that has merged devices
  and doubled commands (issue #120).
- **A custom name**: `fwh_` plus 2–8 lowercase letters or digits, e.g.
  `fwh_home`. Names other integrations use are refused: `fwhai`, `fwh_ai`,
  `fwh_lb`.

## Why it is permanent

Home Assistant knows each entity by its **unique id**. The unique id includes
the name. If you change the name later:

- Home Assistant sees new unique ids and creates **new entities and a new
  device**. The old ones stay behind as unavailable until you remove them.
- The new entities can't take the old entity ids while the old entities
  exist, so they can end up with ids such as `…_state_of_charge_2`.
- History does **not** carry over automatically. Whether it can be carried
  over at all is **not established**, so assume it can't.
- Dashboards, automations and scripts that use the old entities need updating.

So choose once, and treat the choice as permanent.

## Nothing is published until you choose

On a new install the bridge connects to the MQTT broker but publishes nothing:
no entities, no states, no availability. It also accepts no commands from Home
Assistant. A banner says *Home Assistant publishing is waiting for you to
choose a naming*. To choose:

- **In the UI:** the setup wizard's **Home Assistant naming** step, or the
  banner's **Choose naming** button. The step shows what Home Assistant will
  get for your aGates, and you must tick *I understand this is permanent*.
- **Without the UI (Docker):** set the environment variable
  `MQTT_NAMESPACE=fwh_mb` (or `franklinwh`, or a custom name) and start the
  bridge. On a new install this counts as your choice.
- **Add-on:** the `mqtt_namespace` option does the same.

`MQTT_NAMESPACE` is read only while the choice is still open. On an install
that already has a name it is ignored, and the log says so. To change the name
later, see [Changing it later](#changing-it-later).

Existing installs are not affected. An install that was set up before this
step existed keeps its name and publishes as it always has.

## Every combination

| Setup | Devices you'll see in Home Assistant | Ids | Watch out for |
|---|---|---|---|
| **1.** One aGate, one bridge, one Home Assistant, one broker | `FranklinWH Modbus 0000ABCD` | `fwh_mb_0000ABCD_…`, topics `fwh_mb/0000ABCD/…` | Nothing special. |
| **2.** Several aGates, one bridge, one Home Assistant | One device per aGate, each named by its short serial: `FranklinWH Modbus 0000ABCD`, `FranklinWH Modbus 0000EF01` | First aGate: `fwh_mb_0000ABCD_…`, topics `fwh_mb/0000ABCD/…`. Each **added** aGate's unique ids and topics use its **gateway id**, not its serial: added as "aGate EF01", they are `fwh_mb_agate_ef01_…` and `fwh_mb/agate_ef01/…` | The device name follows the serial, but an added aGate's ids follow its gateway id, so changing that id would move its entities. Choose its name when you add it. A demo is named `FranklinWH Modbus demo (mock)`. |
| **3.** One aGate in several integrations (this bridge, Direct Connect, FWHAI), one Home Assistant, one shared broker | Three devices: `FranklinWH Modbus 0000ABCD`, `FranklinWH Direct 0000ABCD`, `FranklinWH Cloud 0000ABCD` | Separate names: this bridge `fwh_mb`, Direct Connect `fwh_dc` (proposed, not yet confirmed by that project), FWHAI `fwhai` | Never give two integrations the same name: see [Shared broker](#shared-mqtt-broker). Two integrations controlling one battery can still fight over it, whatever the names. |
| **4.** As 3, but each integration on its own broker | Three devices, as in 3 | As in 3 | No MQTT collision is possible. Separate names still keep the devices apart in Home Assistant. |
| **5.** One bridge, two Home Assistants on the same broker | Both see the same devices and entities | Unique ids are the same in each Home Assistant, which is fine: they are separate systems | Both can send commands. An automation in either one drives the battery. |
| **6.** One bridge, two Home Assistants on different brokers | Only the Home Assistant on the bridge's broker sees anything | — | The bridge publishes to one broker only. The second Home Assistant needs the brokers bridged (a Mosquitto bridge, for example), or it sees nothing. |
| **7.** Two Modbus Bridges (two sites), one Home Assistant, one broker | One device per aGate per site: `FranklinWH Modbus 0000ABCD`, `FranklinWH Modbus 0000EF01` | The first aGates differ by serial, so one name (`fwh_mb`) works for both sites | Each bridge has its own client id, so they don't knock each other off the broker. **Unique ids and topics of added gateways and demos use the gateway id**: two bridges that each have a `demo` gateway would collide under one name. Give each site its own name (`fwh_mb` and, say, `fwh_site2`) unless you are sure no gateway id is used at both. |
| **8.** The add-on and a Docker bridge for the same aGate | — | — | **Don't.** One Modbus client per aGate. If you must, for a test, give them different names (e.g. `fwh_mb` and `fwh_test`) so their entities and command topics stay apart. |
| **9.** Remove and reinstall, any of the above | See [Remove and reinstall](#remove-and-reinstall) | Same name = same entities back | Choose the same name again. The wizard looks for it. |

### Reading Home Assistant is a separate thing

The bridge can also **read** Home Assistant (Settings → Home Assistant →
Home Assistant instances), for entities used as schedule conditions. That
connection is separate from MQTT publishing. Adding several Home Assistant
instances for reading doesn't change where the bridge's entities are
published: they go to the one MQTT broker the bridge is connected to.

## Shared MQTT broker

Several bridges and integrations can share one broker, provided each has:

- **its own client id.** The broker allows one connection per client id. The
  bridge gives each install its own: `franklinwh_bridge_<8 hex characters>`.
- **its own name (topic root).** This bridge's topics all start with
  `<name>/`, and its discovery topics with `homeassistant/<type>/<name>_`.

**Never choose another integration's name.** If two producers publish under
the same name for the same aGate:

- Home Assistant merges them into **one device**.
- Where their entity names match, they share the same discovery topic and
  unique id, so each **overwrites** the other's entity definition.
- Both subscribe to the same command topics, so one Home Assistant control is
  **executed twice**, once by each integration, on the same aGate.

All three happened on a real install (issue #120). It is why `franklinwh`
is no longer recommended.

## Remove and reinstall

What persists after you uninstall the bridge:

| Where | What is kept |
|---|---|
| Home Assistant | The entities, the device and their history. They show the last value or unavailable. |
| The MQTT broker | The retained discovery configs, unless you remove them first (below). Home Assistant re-creates the entities from them when it restarts. |
| The bridge's own data | Docker: kept in the data volume unless you delete it. **Add-on: deleted on uninstall** (`/data` goes with the add-on), so settings, schedules and history in the bridge are lost. |

**To get the same entities back**, choose the **same name** as before. When a
broker is connected, the Home Assistant naming step looks on it for this
bridge's entities from an earlier install (a read-only check of a few known
topics for your aGate under each standard name). If it finds them it selects
that name and says: *Found entities from a previous install of this bridge
under '…' — choose it to keep your existing Home Assistant entities and their
history.* It can only find them if the discovery configs are still on the
broker and the aGate's serial is known (the aGate has connected).

**If you choose a different name**, you get new entities and a new device. The
old ones stay in Home Assistant as unavailable. Remove the old device in Home
Assistant (Settings → Devices & services → MQTT → the device → Delete).

**To remove cleanly before uninstalling:**

1. In the bridge, go to **Settings → Home Assistant → Publish to Home
   Assistant** and switch each gateway off. This removes its entities from
   Home Assistant and stops publishing it.
2. Uninstall the bridge.

Or delete the device in Home Assistant afterwards. The **Unpublish** button
on its own isn't enough while the bridge keeps running: it publishes again
on its next pass (issue #131).

## Changing it later

**Settings → Home Assistant → MQTT Broker → Change namespace…** changes the
name on an install that has already published. It:

- removes from the broker only the discovery configs **this bridge**
  published under the old name. Entities that another integration published
  under the same name are left alone;
- republishes everything under the new name, which Home Assistant shows as new
  entities and a new device (see [Why it is permanent](#why-it-is-permanent));
- records the change in the log.

## One Modbus client per aGate

Don't run two Modbus integrations against one aGate (this bridge twice, or
this bridge and another Modbus tool). This project's practice is one Modbus
client per aGate: two pollers can get in each other's way, and two
integrations sending battery commands can undo each other's commands.
Different names keep their Home Assistant entities apart. They don't make
two Modbus clients safe.
