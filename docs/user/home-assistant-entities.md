# Fixing, moving and removing Home Assistant entities

This page is for owners. It covers what to do when the bridge's device or entities in Home Assistant
look wrong, and which actions are safe for now. The rules behind it are in
[`docs/install-lifecycle.md`](../install-lifecycle.md).

All bridge buttons named here are in the bridge's **Settings → Home Assistant** tab, under
**MQTT Broker**.

## Symptom: a device with no entities

You see one or both of these:
- In Home Assistant, the device **FranklinWH Modbus &lt;serial8&gt;** says "This device has no entities".
- The bridge's **Dashboard card** says "Home Assistant … has none of our entities in its registry".

**Why.** Version 0.2.4 changed how the bridge identifies its device to Home Assistant.
- Your entities kept their IDs, but Home Assistant left them on the **old** device
  ("FranklinWH &lt;serial8&gt;") and created the new device empty.
- If the FranklinWH HA Integrator (FWHAI) runs on the same Home Assistant with the `franklinwh`
  namespace, the old device is shared with it (#120). That makes the entities even less likely to
  move.

## Fix: move the entities to the bridge's device

1. Bridge: **Unpublish**. Home Assistant removes the bridge's entities.
2. Home Assistant: **Settings → Devices & services → Devices**. Open the empty
   **FranklinWH Modbus &lt;serial8&gt;** device, then **⋮ → Delete**.
3. Bridge: **Republish**. The entities appear on the bridge's device.
4. Bridge: **Dashboard card → Copy**, then paste it into your dashboard. The card reads entity IDs
   from Home Assistant's registry, so it matches whatever IDs Home Assistant gave the entities.

### What this changes

| | After the fix |
|---|---|
| Entities and their values | on the bridge's device, working |
| **Entity IDs** | **new.** Home Assistant builds them from the device name, so `sensor.franklinwh_<serial8>_…` usually becomes `sensor.franklinwh_modbus_<serial8>_…` |
| Dashboards | regenerate them with the **Dashboard card** (step 4) |
| Automations and scripts | **still name the old IDs**, so they stop working. Update them, or rename the IDs back (below) |
| History and Energy dashboard | **stay under the old IDs.** Graphs restart from today unless you rename the IDs back |

### Keep your history: rename the IDs back

For each entity that matters (energy and battery sensors first):
1. Home Assistant: **Settings → Devices & services → Entities**. Open the entity, then
   **⚙ Settings → Entity ID**.
2. Set it to the old ID, e.g. `sensor.franklinwh_<serial8>_battery_soc`.

This only works if no other entity holds that ID. History and long-term statistics follow the ID,
so the graphs join up. Automations that use the old ID work again.

### When not to use it

- **You can't afford a break in history and can't rename the IDs back.** Leave the entities where
  they are. They still work on the old device; only the dashboard card can't see them.
- **FWHAI shares the namespace.** About 13 entities share their unique IDs with FWHAI's. After the
  fix, those can show as unavailable or switch between the two integrations' values. The lasting fix
  is for this bridge to use its own namespace, which needs a reliable **Change namespace** (see
  below).

## Actions to avoid for now

| Action | Why | Until |
|---|---|---|
| **Change namespace…** | It reports old entries as removed, but some stay, and Home Assistant shows duplicate devices (#136). | the verified-removal release (R2) |
| Uninstall, then reinstall the add-on to "start clean" | The add-on loses all its settings, schedules and history on **every** uninstall, even when you choose to keep data. Old entities stay in Home Assistant, and the reinstall can create duplicates. | the data-location release (R6); use **Update** instead |
| **Delete** a gateway that is published | Its entities stay in Home Assistant even though the bridge says they will be removed. Switch its **Publish to Home Assistant** off first, check Home Assistant, then delete. | the purge release (R3+R4) |
| Publishing the add-on's port 8099 | Security fix pending. | 0.2.7 |

## Removing the bridge completely

There is no "remove everything" button yet (it arrives with R3+R4). Until then:
1. Bridge: switch **Publish to Home Assistant** off for each gateway, then **Unpublish**.
2. Home Assistant: check that the bridge's entities are gone. Removals aren't confirmed yet
   (#136), so if some remain, press **Unpublish** again.
3. Home Assistant: delete any FranklinWH Modbus devices that are left with no entities.
4. Uninstall the add-on.

Values that the bridge left on the MQTT broker are harmless. Home Assistant ignores them once the
entities are gone.

## Coming later

The purge release (R3+R4) adds an inventory of everything the bridge has published, plus a
**Remove from Home Assistant** action with a dry run. This page will then say "use Repair", and
keep the steps above as the fallback.
