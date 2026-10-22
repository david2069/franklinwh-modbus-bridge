# Install lifecycle: Home Assistant entities and MQTT

Status: **policy, decisions accepted by the owner on 2026-10-16** (§10). Reviewed by the Direct
Connect bridge session. Nothing in this document is implemented unless it says "today".
Code references are to v0.2.6 (`105918b`).

### Where the rules live

| Layer | What | Where |
|---|---|---|
| Policy | Principles, lifecycle types, required outcomes, reserved namespaces, decision record | this document |
| Release and test procedure | Gates G0–G3, HAOS acceptance runs, sign-off checklist (shared by both bridges) | `franklinwh-direct-connect-bridge` `docs/TEST_POLICY.md` |
| User procedures | Update, reinstall, uninstall and purge, add-on ↔ Docker, step by step | `INSTALL.md`, `addon/DOCS.md`; written as each mechanism ships |

A change to this policy is a PR that updates §10's decision record.

This document defines every way the bridge gets installed, updated, reinstalled and removed,
and what each must do to Home Assistant (devices, entities, entity IDs, history) and to the
MQTT broker (retained topics). It sets out the remediation plan that gets us there.
It is the lifecycle half of the shared test policy (`franklinwh-direct-connect-bridge`
`docs/TEST_POLICY.md`). That policy says what must be tested and recorded; this says which
paths exist and what each must do. Its G1 matrix runs §9 of this document.

## 0. Why

Between 0.2.0 and 0.2.6 the bridge changed how Home Assistant identifies it five times:
- namespace applied (#91)
- `fwh_mb` for new installs (#121)
- device identifier and name (#127)
- device name again (#129)
- automatic clean-up (#132)

Four of those releases shipped on one day. Each change was reasonable on its own. None of
them was defined against a full set of install paths, so each one left something behind:

| What users saw | Caused by |
|---|---|
| Demo entities in the real HA, never removed (124 on the test VM) | 0.2.1–0.2.3, demo published by default (#8, #96) |
| One merged device with FWHAI; `operating_mode` executed by both | shared `franklinwh` identifier and topics (#120, open) |
| Empty "FranklinWH <serial8>" device after upgrading | 0.2.4 identifier change (#127) |
| "Change namespace" reports 65 removed; 34–49 stay; duplicate devices | 0.2.4 feature, QoS 0 and unverified (#136, open) |
| Fresh installs given `fwh_mb` before the user could choose | 0.2.4 (#121); PR #130 open |
| Entities stay "available" with frozen values after stop or uninstall | no MQTT last-will (LWT) message and no offline message on stop (all versions) |
| Deleted gateway's entities stay in HA | `DELETE /api/gateways/{id}` never removes anything (all versions) |
| 19 of 65 demo entities "unknown" in HA | mock point names ≠ entity `stat_key` |
| Branch tested in the add-on store ran the last release | `BRIDGE_REF` pin (#140) |

The fix is not another identity change. It is to:
1. define the paths;
2. give the bridge reliable, verifiable primitives for publishing and removing;
3. stop changing identity until those exist.

## 1. Principles

1. **One identity, decided once.** An install's namespace and device identifiers are fixed at
   first publish. They change only through an explicit, verified migration that leaves no
   orphans (§7, M6).
2. **Offline is not removed** (BR-22).
   - Stop, crash, restart or a broker outage → entities **unavailable**, still there.
   - Delete, purge or unpublish → entities **removed**.
3. **Every removal is verified.** A removal is counted only when the broker no longer holds the
   topic. The UI reports verified counts, never attempted ones (#136).
4. **The bridge removes only what is provably its own** (BR-25). "Ours" means `sw_version` starts
   with `Modbus Bridge` (or the v0.1.0 form), under a namespace this install has used.
5. **Uninstall runs none of our code.** The Supervisor has no uninstall hook. Anything that must
   happen at removal is either an explicit in-app action *before* uninstalling (BR-24), or
   detected and handled by the *next* install.
6. **Mocks are opt-in, labelled and fully torn down** (BR-27). A demo never reaches HA unless the
   user says so, and deleting it removes everything it published.
7. **No surprise for an existing install.** An update never changes a working install's entity
   IDs, device, namespace or history unless its release notes say so, and G1 item 2 proved it.
8. **Configuration storage never depends on a feature switch.** The DB and settings are available
   whatever MQTT, metrics or HA publishing are set to. The Direct Connect bridge broke on this
   (its #81, 0.3.3/0.4.0).

## 2. What we publish (the artefacts)

| Artefact | Where | Built from (today) | Survives uninstall? |
|---|---|---|---|
| Discovery config (retained) | `homeassistant/<type>/<prefix>_<sid>_<slug>/config` | namespace, short id, slug, HA type | yes, until removed |
| State (retained) | `<prefix>/<sid>/<group>/<slug>` | namespace, short id | yes; **nothing ever removes these** |
| Availability (retained) | `<prefix>/<sid>/availability` | namespace, short id | yes; stays `online` today (no LWT) |
| Command subscription | `<prefix>/<sid>/control/<slug>/set` | namespace, short id | n/a |
| HA device | identifier `<prefix>_modbus_<SERIAL>` (others `slug(<prefix>_modbus_<gw>_<serial>)`) | namespace, gateway id, serial | yes, while any discovery config references it |
| HA entity | unique_id `<prefix>_<sid>_<slug>` | namespace, short id, slug | yes, while its config is retained |
| **entity_id** | derived by HA from **device name + entity name** at first registration; we send no `object_id` | device name `FranklinWH Modbus <serial8>` | kept by unique_id; a device rename changes the entity_ids of **new** installs only |
| Bridge device (#324) | configs `homeassistant/binary_sensor/<prefix>_bridge/bridge_problem/config` and `homeassistant/sensor/<prefix>_bridge/bridge_heartbeat/config`; states `<prefix>/bridge/problem` and `<prefix>/bridge/heartbeat` (retained); identifier `<prefix>_modbus_bridge`; unique_ids `<prefix>_bridge_problem`, `<prefix>_bridge_heartbeat`; no availability topic | namespace | yes, until removed. Unpublish removes the configs and both states; a namespace change removes the old configs (E6, E10) |
| HA long-term statistics | recorder | entity_id | **outlives the entity** |
| Bridge data | add-on `/data/bridge.db` (Docker volume `app-data`) | — | add-on: **no** (see M7) |

Short id: the last 8 characters of the serial for `default`; the slugged gateway id for any other
gateway (so `demo` and `demo-2` give `demo` and `demo_2`).

**Identity tuple** = (namespace, short id, slug, HA type) for entities, plus (namespace, gateway id,
serial) for the device. Any change to an input is an identity change (principle 1).
**A device-name change is also an identity change.** HA 2026.10 derives entity_ids from the device
name and ignores `object_id`, as measured by the Direct Connect bridge. So a rename changes the
entity_ids of every new install, as 0.2.4 did. Any change to the device name, unique_id, identifiers,
topics or slug is treated as **L3b**.

## 3. Lifecycle types

Each type has a **detection signal**, so the bridge knows which type it is in, and a **required
outcome**.

| ID | Type | Trigger | Detected by (target) |
|---|---|---|---|
| **L1** | Fresh install, clean | first start, empty data, no artefacts of ours on the broker | no DB and an empty inventory (M3) |
| **L2** | Fresh install over leftovers | first start with empty data, but artefacts of ours exist (reinstall after data was lost, or a second install on the same broker) | no DB, inventory finds our `sw_version` under some namespace |
| **L3a** | Update, no identity change | new version, same data | DB exists; release declares no identity change |
| **L3b** | Update with identity change (device name, unique_id, identifiers, topics or slug) | new version that declares an identity migration | DB exists; migration flag in the release |
| **L4** | Reinstall keeping data | uninstall with "keep data", or Docker container recreated on the same volume | DB exists, `install_id` unchanged |
| **L5** | Restore from backup | HA backup or `bridge backup restore` | DB exists, but its `install_id` was seen running elsewhere, or a `restored_at` marker is set |
| **L6** | Uninstall + purge | user wants everything gone | **in-app action before uninstall** (M4) |
| **L7** | Uninstall + keep | user removes the bridge but keeps HA history for a later reinstall | user does nothing, or chooses "keep" in M4 |
| **L8** | Downgrade | older version on newer data | DB schema newer than the code |
| **L9** | Platform move | add-on ↔ Docker, or a new host | DB carried over (L4/L5 rules), or not (L2) |

In-life events, each with a required outcome:

| ID | Event |
|---|---|
| **E1** | Add a real gateway |
| **E2** | Create a demo gateway |
| **E3** | Publish to HA off, then on (per gateway) |
| **E4** | Disable, or stop, a gateway (including `default`) |
| **E5** | Delete a gateway |
| **E6** | Change namespace |
| **E7** | Change broker (Save & Reconnect) |
| **E8** | MQTT disabled or enabled |
| **E9** | Stop, restart or crash |
| **E10** | Bridge-wide Unpublish or Republish |
| **E11** | Gateway edit that restarts its poller (host, port, phase view, groups) |
| **E12** | A second Modbus client appears on the same aGate (Docker + add-on, or the Direct Connect bridge's Modbus fallback, which is on by default and aims at the gateway's own host :502) |

## 4. Required outcome per type, today, and the gap

| ID | Required outcome | Today (0.2.6) | Gap |
|---|---|---|---|
| L1 | User chooses the namespace before anything is published (#130). One device per gateway; all entities available or truthfully unknown. | `fwh_mb` silently (namespace.py:145-179); PR #130 adds the choice | merge #130 (rebased on 0.2.6) |
| L2 | Detect our leftovers. Offer: **adopt** (same namespace and ids → HA re-links entities and history) or **purge then start clean**. Never create duplicates silently. | not detected; a former `franklinwh` install gets a second device | #130's probe covers 3 topics; needs the full inventory (M3) |
| L3a | Nothing changes in HA or on the broker except new or removed entities listed in the release notes; removed ones are tombstoned and verified. | holds, except the 90 s sweep is unverified (QoS 0) | M1 |
| L3b | Migration runs once, is verified, and is reported: old device removed only when empty, entity_ids kept, history kept. | 0.2.4 had no migration; clean-up only from 0.2.6, current prefix only, not mocks | M6 + rule: identity changes ship alone |
| L4 | Exactly as L3a. | **Broken for the add-on.** Data lives in `/data`, which the Supervisor deletes on **every** uninstall, including "keep data" (measured on HAOS 18.3 / Supervisor 2026.10.2: `Removing app data folder` with `remove_config: false`; the reinstall seeded a new admin, lost the demo gateway and chose the namespace afresh). So every add-on reinstall is L2 today. The mapped `addon_config` folder **survives** a keep-data uninstall and is deleted by a delete-data one (measured). | M7 |
| L5 | As L4, plus a fresh MQTT client id and a warning if another live install shares the `install_id`. | DB restore copies `client_id`, so two installs can collide (#118 again) | M8 |
| L6 | Before uninstalling: remove all our discovery, state and availability topics, every namespace this install used, every gateway (deleted ones too), and HA devices left empty. Show verified counts and a dry run first. | none (BR-24 unimplemented). Unpublish is the closest: bound gateways only, no promoted points, no state, no HA registry | M4 |
| L7 | Entities go **unavailable**, not "available with frozen values"; artefacts stay for a later L2-adopt. | stay `online` with frozen values | M2 |
| L8 | Refuse to start with a clear message, or run read-only. Never publish with a newer identity scheme. | not handled | M9 (later) |
| L9 | Same as L4/L5 if the data moves; same as L2 if it doesn't. Never run both at once (one Modbus client per aGate). | doc only (PR #130 combination 8) | covered by L2/L4/L5 |
| E1 | Device appears when the serial is known; entities available. | holds (reconcile, 0.2.5) | — |
| E2 | Demo is opt-in (wizard **and** API); labelled "(mock)"; entities have values. | wizard: off by default; **`POST /api/gateways` defaults publish_to_ha=True**; 19/65 entities unknown | M10, M11 |
| E3 | Off → verified removal of config, state and availability; on → same unique_ids, so HA re-links entity_ids. | off: config tombstoned (QoS 0); state left; extra gateways get `offline` instead of an empty payload | M1, M5 |
| E4 | Disable or stop → **unavailable** (offline), not removed. Same for `default`. | default: nothing; stop: nothing (stays `online`) | M2 |
| E5 | Verified removal before the row is deleted (BR-23), dry run shown. | **nothing removed**; the UI says it will be | M5 |
| E6 | Verified removal of the old namespace (configs, state, availability; promoted points; v0.1.0 form), then publish under the new one; HA ends with one device per gateway. | QoS 0, unverified, enumerated topics only, state and availability left (#136) | M1, M3 |
| E7 | Ask: purge from the old broker (if reachable) or leave; then publish to the new one. | old broker left as is | M4 scoped to a broker |
| E8 | Disabled → offline; enabled → online. | holds | — |
| E9 | Offline via LWT on crash, explicit offline on stop; online on start. | **no LWT, no offline on stop** | M2 |
| E10 | Unpublish = L6 for the broker without uninstalling; Republish restores. | partial (see L6) | M4 |
| E11 | Same identity; no availability gap beyond one poll; the order is old offline → old disconnect → new connect → online. | restart re-binds; untested for availability races (Direct Connect measured a stuck `offline`, its #82) | M2 ordering |
| E12 | Detected and reported (another client on :502); never two pollers on one aGate. | not detected | M13 |

## 5. Mock and demo gateways

- Created only on request (wizard "Explore with a demo gateway", or Settings). Never by default.
- **Never published to HA unless the user says yes**, through every route. Today the API route
  defaults to publishing.
- Always labelled: device name `FranklinWH Modbus <id> (mock)`, `hw_version` `aGate MOCK`.
- Mock data is never recorded in history or metrics (already true).
- Every entity a mock publishes has a value. The Direct Connect bridge has the same symptom
  (10 of 21 mock entities unknown), so the parity test applies to both repos. The mock must produce the same point keys as the
  real poller. A unit test fails when any published entity's `stat_key` is missing from the mock's
  points. Today 19 of 65 fail, e.g. `voltage_v` vs `grid_voltage_v`.
- Deleting a demo, or purging, removes everything it published (E5, L6). The 0.2.6 clean-up must
  not skip mocks. Old demo devices are the most common leftover (#8).
- The demo gateway id is part of its identity. Two bridges on one namespace that both create
  `demo` collide. Decision D5: documented as **one demo per broker**; no identity change.
- Demo energy sensors feed HA long-term statistics, which outlive the entity. The wizard already
  warns about this; purge cannot remove statistics.

## 6. Coexistence: namespaces reserved and in use

| Producer | MQTT prefix | Device identifier | unique_id | Status |
|---|---|---|---|---|
| Modbus bridge (new installs) | `fwh_mb` | `fwh_mb_modbus_<SERIAL>` | `fwh_mb_<sid>_<slug>` | — |
| Modbus bridge (legacy) | `franklinwh` | `franklinwh_modbus_<SERIAL>` (pre-0.2.4: `franklinwh_<SERIAL>`) | `franklinwh_<sid>_<slug>` | overlaps FWHAI topics and 13 slugs (#120) |
| FWHAI | `franklinwh` (topics); entities `fwhai_<sid>_` on new installs | `franklinwh_<FULL_SERIAL>` | — | **same identifier as our pre-0.2.4 form** → merged device (#120) |
| Direct Connect bridge | `franklinwh-local`, **moving to `fwh_dc`** (its #92) | `franklinwh_<serial, lower case>` → `fwh_dc` form | `franklinwh_<serial, lower case>_<key>` → `fwh_dc` form | differs from our pre-0.2.4 identifier only by case until #92 ships. #92 is a release of its own with migration, after its 0.4.x repairs; device names and entity_ids stay |

Rules:
- Reserve `fwhai`, `fwh_ai`, `fwh_lb` (old Local Bridge name), **`fwh_dc`** (the Direct Connect
  bridge's choice) and **`franklinwh-local`** (its current prefix).
- `franklinwh_` + serial is no producer's to own. Each producer moves to a producer-specific
  identifier. Ours already is (`_modbus_`). Each move ships alone, with migration.
- One Modbus client per aGate. The add-on, Docker and a Direct Connect Modbus fallback never
  poll the same aGate at the same time.

## 7. Mechanisms to build

| ID | Mechanism | Satisfies |
|---|---|---|
| **M1** | **Verified removal primitive.** One function for every tombstone: QoS 1 publish, re-read the retained store, retry what's left, return verified counts. Used by sweep, namespace change, unpublish, delete and purge. | #136, principle 3 |
| **M2** | **Availability truth.** MQTT LWT (`offline`, retained) on the long-lived client; explicit `offline` on stop, on gateway stop or disable (`default` included). Restart order: old offline → old disconnect → new connect → online. Same pattern as the Direct Connect bridge, which already does LWT. | BR-22, L7, E4, E9, E11 |
| **M3** | **Inventory.** Scan the broker for everything of ours: every namespace in a small history table, all HA types, state and availability, ours vs foreign vs stale. Also scan connected HA registries for our devices and entities. API + Settings page with a dry run. | BR-26, L2, E6 |
| **M4** | **Purge** ("Remove from Home Assistant…"). Built on M3 + M1: scope = everything / one namespace / one broker / one gateway. Shown in Settings and in the add-on docs as the step *before* uninstalling. Optionally removes empty HA devices of ours. | BR-24, L6, E7, E10 |
| **M5** | **Delete and unpublish do what they say.** Gateway delete runs M4 scoped to that gateway *before* deleting the row. Publish-off clears state and availability too. | BR-23, E3, E5 |
| **M6** | **Identity migration framework.** For L3b only: record the old tuple, publish new, verify HA moved the entities (needs an HA restart, per the Mac mini VM test), then remove old. Never needed if principle 1 holds. | L3b |
| **M7** | **Data where "keep data" keeps it.** Move the add-on DB to the mapped `addon_config` (`/config` inside the add-on), migrating from `/data` on first start. Measured: `addon_config` survives a keep-data uninstall; `/data` never does. The folder is named after the add-on slug, which includes a hash of the **repository URL**, so installing from a different repository URL (e.g. a branch) is a new install with an empty folder: L2, not L4. | L4 |
| **M8** | **Install identity.** `install_id` (UUID) in the DB plus a namespace history table. `client_id` includes the install id; restore (L5) regenerates it. Two installs on one broker must never share a client id (#118). | L2, L5 |
| **M9** | **Downgrade guard.** Refuse to publish on a newer schema. | L8 |
| **M10** | **Mock parity test** and key alignment (§5). | E2 |
| **M11** | **One default for publish_to_ha on mocks**: off in every route. | E2 |
| **M12** | **Build provenance.** `/api/live` reports version **and** source commit; the add-on can build a branch for testing (#140). | G1 precondition |
| **M13** | **Second-client detection.** Report when the aGate refuses or drops us while another client holds :502; record which clients were active. | E12, one-client rule |

M1 and M3 are written as a self-contained module (no bridge imports; inputs are broker
connection, prefixes and an "is ours" predicate), so the Direct Connect bridge can reuse them for
its `fwh_dc` migration (its #92) instead of building its own.

## 8. Remediation plan

Release rules (from the test policy):
- one functional area per release;
- identity changes alone;
- no same-day releases except hotfixes;
- every release passes G0–G3.

| Step | Release | Area | Contents | Issues |
|---|---|---|---|---|
| S1 | **0.2.7, hotfix, first** | security | Add-on auth: treat a request as ingress only when it comes from the Supervisor's ingress proxy with the ingress headers; everything else authenticates. Regression test; G1 port-auth check | GHSA-hhm2-xvvc-f6ww (private) |
| R0 | — (now) | docs/process | **Freeze identity changes.** Hold PR #130. Merge this doc and the test policy. Correct user docs: Change namespace and Unpublish aren't reliable yet; delete doesn't unpublish. Owner page `docs/user/home-assistant-entities.md` (symptoms, the stopgap below, what to avoid). Guidance for HA Live (below). The UI's "Learn more" links to that page (version-pinned) ship as a small UI release after S1. | — |
| R1 | 0.2.8 | packaging | M12 build provenance; add-on branch builds for testing | #140 |
| R2 | 0.2.9 | MQTT | M1 verified removal, applied to every existing tombstone path; M2 LWT/offline | #136 |
| R3+R4 | 0.3.0 | lifecycle/clean-up | M3 inventory (dry run) + M4 purge + M5 delete/unpublish; mocks included in clean-up; empty HA devices of ours removed | BR-23/24/26, #8 |
| R5 | 0.3.1 | setup wizard | PR #130 rebased: naming choice, reinstall detection on M3, adopt-or-purge for L2 | #130 |
| R6 | 0.3.2 | store/packaging | M7 data location + M8 install identity | L4, L5 |
| R7 | any, small | mock | M10 + M11 (no identity change) | demo unknowns |
| R8 | — | identity | Not done (decision 5): one demo per broker is documented instead. FWHAI and Direct Connect identifier moves happen in their own repos (Direct Connect: `fwh_dc`, its #92) | #120 |

**HA Live, until R4:**
- Update in place; don't uninstall with data deletion.
- Don't use Change namespace (#136).
- Don't delete gateways that are published; switch Publish off first, then check HA.
- Don't uninstall and reinstall the add-on expecting to keep settings: until R6 every reinstall starts fresh (§4 L4).
- Leave port 8099 unpublished, and don't install untrusted add-ons alongside the bridge, until S1
  ships (private advisory).
- Leftover devices can be deleted by hand in HA once they have no entities.
- **Stopgap for an L3b left half-done** (entities stuck on the pre-0.2.4 device; the new device
  empty): Unpublish → delete the empty device in HA → Republish → regenerate the Dashboard card.
  Used on HA Live on 2026-10-16 and it worked. **Cost:** new entity_ids, so automations break and
  history splits unless the IDs are renamed back. Steps and caveats are in
  `docs/user/home-assistant-entities.md`.

## 9. Acceptance matrix (runs as TEST_POLICY G1)

For every run, take a **before and after inventory** of the broker (every retained topic under
`homeassistant/` and every prefix in §6) and of HA (devices, entities, entity_ids, states). Save them
in the G1 trace as `api/inventory-before.json` and `api/inventory-after.json`, in both repos. The
check is the diff, not "the UI says OK". HA keeps devices from earlier installs, so a run judges
**only the devices its own install produced**; everything else must merely be unchanged.

| # | Path | Start state | Pass if |
|---|---|---|---|
| 1 | L1 + E2 | clean HA + broker | one demo device only after opting in; every entity has a value; no orphans |
| 2 | L3a | previous release, real or demo gateway | identical device, unique_ids and entity_ids; no new orphans; history continuous |
| 3 | L3b (only when a release has one) | previous release | one device per gateway after migration; entity_ids and history kept; zero old configs |
| 4 | L4 | configured install, uninstall keeping data, reinstall | as #2 |
| 5 | L2 | `franklinwh` install's artefacts on the broker, no data | detection offers adopt or purge; adopt → no duplicates; purge → zero old |
| 6 | L6 | configured install with deleted gateways and a namespace change behind it | purge → inventory empty (ours); foreign untouched; then uninstall → nothing reappears |
| 7 | L7 / E9 | running install, stop it | entities `unavailable` within the LWT window |
| 8 | E5 | two gateways, delete one | its artefacts gone (verified), the other's untouched |
| 9 | E6 | `fwh_mb` → `fwh_vmt` → `fwh_mb`, 10 repetitions | zero old configs every time (#136 reproduction) |
| 10 | coexistence | Modbus bridge + Direct Connect bridge (+ FWHAI if available) on one HA and broker | no merged devices, no shared unique_ids, no cross-commands; **socket count on the aGate's :502 is one** |
| 11 | E11 | edit a gateway's poll interval and phase view | same device and entity_ids; availability gap ≤ one poll; never left `offline` |

## 10. Decision record

Accepted by the owner on 2026-10-16.

| # | Decision | Reason |
|---|---|---|
| D1 | **Identity changes are frozen until R3+R4 ships.** No change to namespace defaults, device identifiers, device names, unique_ids, topics or slugs. | Every past failure came from an identity change with no purge or migration behind it. |
| D2 | **L2 default is adopt.** A fresh install that finds our leftovers offers adopt (keep the old namespace and ids; HA keeps entity_ids and history) as the default; purge is an explicit choice. | Adopt loses nothing and can be undone; purge cannot. |
| D3 | **Purge removes empty HA devices of ours**, through the registry API: only devices whose identifiers are ours and that hold **zero** entities, after the MQTT removal is verified, and listed in the dry run first. | HA otherwise keeps empty devices (measured). |
| D4 | **Add-on data moves to `addon_config`** (M7, R6). | Measured: `/data` is deleted on every uninstall; `addon_config` survives "keep data". |
| D5 | **No per-install demo id.** Document "one demo per broker"; purge and delete remove demos. | Demos are throwaway; not worth an identity change. |
| D6 | **Release order:** S1 security hotfix → R1 provenance → R2 verified removal + offline → R3+R4 inventory and purge → R5 naming choice (#130) → R6 data location and install id; R7 any time. | Security first; R3 alone gives users nothing. |
| D7 | **This document is the repo's lifecycle policy**; the shared TEST_POLICY is the release and test procedure; user procedures go in INSTALL.md and addon/DOCS.md. | One home per rule. |
| D8 | **D1 exception (owner, 2026-10-21):** a new bridge-level device and its two sensors (Bridge problem, Bridge heartbeat; #324 spec v1.2) may be added. No change to any existing device, unique_id or topic. | An alert must show even for a gateway that never answered, which has no device. |
