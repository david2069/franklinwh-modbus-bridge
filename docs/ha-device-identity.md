# Home Assistant device identity across FranklinWH integrations

**Status:** owner decision, 2026-10-15. Applies to every FranklinWH integration
that publishes to Home Assistant: the Modbus Bridge, the Local Bridge (Direct
Connect), FWHAI, and any Cloud integration. Each project documents and follows
it; changes go through the owner.

## The rule

**One Home Assistant device per integration, per aGate.** The same aGate seen
through different integrations is shown as different devices, never one merged
device.

| Integration | HA device name | Device identifier |
|---|---|---|
| Modbus Bridge (Modbus TCP) | `FranklinWH Modbus <short serial>` | `<prefix>_modbus_<FULL_SERIAL>` |
| Local Bridge (Direct Connect, local API) | `FranklinWH Direct <short serial>` | `<prefix>_direct_<FULL_SERIAL>` |
| Cloud integrations | `FranklinWH Cloud <short serial>` | `<prefix>_cloud_<FULL_SERIAL>` |

- `<short serial>` is the last 8 characters of the aGate serial, e.g. `24170091`.
- Modbus Bridge (0.2.5, #128): the name uses the short serial for **every**
  gateway. The first gateway's unique ids and topics also use the short serial
  (`<prefix>_0000ABCD_…`); an **added** gateway's use its gateway id
  (`<prefix>_agate_ef01_…`), and its identifier is
  `<prefix>_modbus_<gateway id>_<serial>` (lowercased). A mock is named
  `FranklinWH Modbus <id> (mock)`.
- The integration word is in **both** the name and the identifier. The name
  is what the user sees, and the identifier is what stops HA merging devices.
- `sw_version` names the producer and its version (e.g. `Modbus Bridge v0.2.4`),
  so a device's origin is always visible on its HA page.

How a user chooses the namespace, and what each combination of aGates,
integrations, brokers and Home Assistants looks like:
[home-assistant-naming.md](home-assistant-naming.md).

## Why

When two integrations announced the same identifier (`franklinwh_<SERIAL>`),
Home Assistant merged them into one device. On the owner's HA (issue #120):
- one device held 155 entities from two integrations;
- each integration overwrote the other's entity definitions where names matched;
- one HA control (`operating_mode`) was executed by **both** integrations on the
  same aGate.

## What else must be distinct per integration

Device identity alone stops the merged *device*. Entities and commands also
need separate namespaces, because Home Assistant keys entities by unique_id
and both integrations subscribe to command topics:

| Item | Must be unique per integration | Modbus Bridge |
|---|---|---|
| unique_id prefix | yes | namespace prefix: `fwh_mb` (new installs); legacy `franklinwh` |
| MQTT topic root (state, command, availability) | yes | same namespace prefix |
| discovery topic node id | yes (follows unique_id) | `<prefix>_<short_id>_<slug>` |
| MQTT client id | yes | `franklinwh_bridge_<8 hex>` per install |

Reserved prefixes: Modbus Bridge `fwh_mb`; FWHAI `fwhai`; Local Bridge
`fwh_lb` (to be confirmed by that project). An integration must not adopt
another's prefix.

## Migration (Modbus Bridge)

- From 0.2.4, the device identifier carries `_modbus_`, so the bridge's
  device separates from FWHAI's on update, even on the legacy `franklinwh`
  prefix. Entities keep their unique_ids and move to the new device.
- The 13 entity names that FWHAI also uses still share a unique_id until the
  install moves off `franklinwh`. **Settings → Home Assistant → MQTT Broker →
  Change namespace… → `fwh_mb`** completes the separation. It removes only the
  discovery entries this bridge published.
- The old merged device keeps the other integration's entities, which is
  correct. An empty leftover device can be deleted in HA.
