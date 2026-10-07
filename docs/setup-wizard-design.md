# First-Run Setup Wizard — Design

**Status:** accepted; backend (`/api/setup/*`) implemented, UI in progress
**Audience:** maintainers, and whoever implements it
**Builds on:** the unconfigured default gateway (PR #2), simulated mock control
(PR #3), the Supervisor auto-configuration (PRs #4, #5) and Settings sub-tabs
(PR #6).

---

## 1. Why

A new install today lands on a dashboard with an unconfigured "Default Gateway"
and a banner saying *Connect your aGate*. From there the user has to know:

- that the aGate's address goes in **Settings → Site & Gateways → Edit**,
- that Modbus TCP must be enabled on the aGate by an installer first,
- that, with no aGate to hand, a *mock gateway* exists and what it can't do.

None of that is discoverable. The wizard asks one question up front — **what do
you want to do?** — and walks each answer to a working state.

### Goals

1. Ask the user's intent first: **connect a real aGate** or **explore with a demo
   gateway**.
2. Real path: find the aGate on the network where possible, confirm it by its
   nameplate, and finish with a live first reading.
3. Demo path: say plainly what a demo gateway can and can't do *before* creating
   one.
4. End with a short Home Assistant checklist (MQTT broker, HA's MQTT
   integration), because that is where a working bridge most often looks broken.
5. REST first (CLAUDE.md): every wizard step is an API call the CLI can use too.

### Non-goals

- Writing anything to the aGate. Discovery is **read-only**.
- Configuring the aGate itself (enabling Modbus TCP is an installer setting).
- Replacing Settings. The wizard sets up the first gateway; everything else
  stays in Settings.

---

## 2. When it appears

Shown to **admins** on the first UI load when `setup_state` is `pending`
(new `app_config` key, see §7):

| `setup_state` | Meaning | Wizard |
|---|---|---|
| `pending` | Fresh install, nothing chosen | Opens automatically |
| `done_real` | A real gateway was set up | Never auto-opens |
| `done_demo` | A demo gateway was created | Never auto-opens; Dashboard keeps a slim *Connect a real aGate* link |
| `skipped` | User chose "I'll set it up myself" | Never auto-opens |

**Migrating existing installs:** a migration sets `done_real` where a real
gateway has ever connected (`last_connected_at` not null), `done_demo` where
only mocks exist, and `pending` otherwise — so nobody who already has a working
bridge sees the wizard.

It can always be re-run from **Settings → Site & Gateways → Run setup again**.
Non-admins never see it; they get the existing banner, which tells them to ask
an admin.

---

## 3. Flow

```
            ┌──────────────────────────────┐
            │ 1. What would you like to do? │
            └──────────────┬───────────────┘
        ┌──────────────────┼──────────────────────┐
        ▼                  ▼                      ▼
 Connect my aGate    Explore with a demo     I'll set it up myself
        │                  │                      │ (setup_state=skipped)
        ▼                  ▼
 2R. Before you start   2D. What a demo can / can't do
        │                  │
        ▼                  ▼
 3R. Find the aGate     3D. Create demo (name, AC type)
   (scan  |  enter IP)     │
        │                  │
        ▼                  │
 4R. Confirm nameplate     │
        │                  │
        ▼                  │
 5R. First live reading    │
        └────────┬─────────┘
                 ▼
     6. Home Assistant checklist
                 ▼
              Finish
```

### Step 1 — Intent

Three choices, each one sentence:

- **Connect my FranklinWH aGate** — "Read and control your aGate over your local network."
- **Explore with a demo gateway** — "Simulated data and controls, no hardware needed."
- **I'll set it up myself** — "Close this; configure gateways in Settings."

### Step 2R — Before you start (real path)

A short checklist, each with a *why*:

- **Modbus TCP is enabled on the aGate.** It's an installer setting; the bridge
  cannot turn it on. Without it nothing below will find the aGate.
- **Know what else talks to it.** Other Modbus integrations can share the aGate
  (a real aGate X answered discovery while two bridges were polling it), but if
  the search reports an *unknown device* where the aGate should be, another
  client or a reboot is the first thing to rule out.
- **The aGate's IP address**, if known (the FranklinWH app or the router's client
  list shows it). Optional — step 3R can search for it.

### Step 3R — Find the aGate

Two tabs: **Search my network** (default) and **Enter address**.

#### Search my network

Scans the local subnet for devices that answer as a FranklinWH aGate:

1. **Which subnets.** This differs by install type, so it is decided in
   `environment.py` — the one place the bridge branches on add-on / Docker / dev
   (CLAUDE.md) — and exposed through a single `candidate_subnets()` call.
   - Add-on: the add-on container sits on HA's internal network, so its own
     interfaces don't reveal the LAN. Ask the Supervisor (`GET /network/info`,
     already permitted by `hassio_api: true`) for the host's interfaces and scan
     their IPv4 subnets.
   - Docker (bridge networking): the container's interfaces are Docker's, and
     there is no Supervisor to ask. Resolve the LAN subnet in this order and
     show where it came from:
     1. **The address the browser used to reach the bridge.** The request's
        `Host` is the Docker host's LAN address (the published port lands
        there), e.g. `192.168.1.50:8100` → `192.168.1.0/24`. A hostname is
        resolved server-side. Skipped for `localhost`/loopback and for public
        addresses (reverse proxies).
     2. **`LAN_SUBNET`** from the environment (added to `.env.example`), for
        setups where 1 doesn't apply.
     3. **Host networking** (`network_mode: host`): the container sees the LAN
        interfaces directly — use them, as on bare metal.
     4. Otherwise **blank**, with *Enter address* offered first.
     This is close to optimal without extra privileges: option 1 covers the
     common case (opening the UI by IP on the LAN) with no configuration.
   - Dev / bare metal: the machine's own non-loopback IPv4 subnets.
2. **Limits.** Only private ranges (RFC 1918, plus Tailscale's `100.64.0.0/10`
   only when typed explicitly). **A /24 (254 hosts) per subnet** — a wider
   prefix is narrowed to the /24 containing the host. Read-only.
3. **Port check.** TCP connect to port 502, ~300 ms timeout, up to 64 at once.
   A /24 completes in well under 10 seconds.
4. **SunSpec check**, for every host with 502 open — **through
   `franklinwh_modbus.discovery`** (franklinwh-modbus v0.9.5), not raw Modbus
   calls in the bridge: the library owns all register I/O (CLAUDE.md). It reads
   the 4-register SunSpec header at PDU **0**, then **40000**, **50000**,
   **30000**, and counts a device as SunSpec only when the `SunS` marker is
   followed by the Common model (**1**). Real aGates answer at **0**. Any unit
   id gets the same answer on an aGate, so use 1.
5. **Nameplate.** Model 1's `Mn`, `Md`, `SN`, `Vr`, in one read. A **FranklinWH
   device** is a SunSpec device returning model 1 whose manufacturer names
   FranklinWH (`is_franklinwh`); it shows as *"FranklinWH aGate X — serial …,
   firmware …"*. Other SunSpec devices (inverters, meters) are listed greyed out
   as "not a FranklinWH device", so the user isn't left wondering.
6. **Disconnect immediately** after each probe (two short reads), so a probe
   never sits on the aGate's Modbus port.

Results are a list. **Each FranklinWH row is selectable, and several can be
selected** — set up one now and leave the rest, or all at once (the first
selected becomes the primary gateway, the others are added as further
gateways). The result kinds (`kind` in the API) and their wording:

| `kind` | What the probe saw | Shown as |
|---|---|---|
| `agate` | SunSpec, model 1, FranklinWH nameplate | **FranklinWH aGate** — selectable |
| `sunspec_other` | SunSpec, model 1, another manufacturer | Greyed out: *"not a FranklinWH device"* |
| `unknown` | Something on port 502 that isn't a SunSpec device (no Modbus reply, no marker, or another first model) | *"Unknown device listening on TCP port 502 at X"* |
| `configured` | A host this bridge already polls — not probed | *"aGate 0091 — already set up in this bridge"* |
| *(no rows)* | Nothing found | *"No aGate found on 192.168.1.0/24."* + links to the checklist and to **Enter address** |

#### Enter address

Host (IP or hostname — a Tailscale address works), port (502), unit id (1), and
**Test** — which runs steps 4–6 on that one host and shows the same result row.

### Step 4R — Confirm

Shows the nameplate and asks for a name (default `"aGate <last 4 of serial>"`)
and the **device type** — `aGate` or `MAC-1` (Meter Adaptor Collar) — prefilled
from the model string where it can be told apart.

**Confirm** configures the primary gateway's host/port/unit/name/type (PATCH;
PR #2 starts it as soon as it has a host). Internally that is still the gateway
with id `default`, which keeps the legacy MQTT topics and entity ids existing HA
dashboards rely on — but see §4: the user never sees "Default".

### Step 5R — First live reading

Waits (up to ~20 s) for the first poll and shows SoC, battery, solar, grid and
home power. A timeout keeps the user on this step with the gateway's own last
error and the step 2R checklist, rather than declaring success.

### Step 2D — What a demo can and can't do

Shown before anything is created, so nobody discovers it the hard way:

| Works | Doesn't |
|---|---|
| Dashboard, power flow, Site view | **SunSpec Explorer, Sequencer** — need real Modbus registers |
| HA entities, including controls | **History and metrics** — synthetic data is deliberately not recorded |
| Controls tab, schedules — **simulated**: they change the demo's synthetic data, never a battery | |

Plus one warning: a demo's energy sensors feed Home Assistant's **long-term
statistics**, which outlive the entity. To keep your real HA's Energy dashboard
clean, try the demo against a test HA.

### Step 3D — Create the demo

Name (default *Demo aGate*) and AC type (single phase / split phase US / three
phase). Creates a **separate** mock gateway — never turns the default gateway
into a mock: the default gateway owns the unprefixed MQTT topics, and synthetic
data there would land in the same HA entities (and statistics) a real aGate uses
later. The UI already views the polling gateway when the default is
unconfigured (PR #2).

### Step 6 — Home Assistant checklist

Live status, each item with a fix link:

| Check | Source | If not OK |
|---|---|---|
| MQTT broker connected | `/api/health` → `mqtt_broker` (PR #5) | Add-on: *Install the Mosquitto broker app* (My Home Assistant link). Docker: *Settings → Home Assistant → MQTT Broker* |
| HA's MQTT integration set up | Add-on: config entries via the Supervisor proxy, if the add-on token may read them (to verify); otherwise shown as a manual step | *In HA: Settings → Devices & services → Discovered → MQTT → Configure* |
| Entities appearing in HA | Count from the `local` HA instance (PR #4) | Usually the item above |
| HA entity access (conditions) | `local` instance connected (add-on) | Docker: *Settings → Home Assistant → Add* |

The second row is the step users miss most (seen on the test VM): Mosquitto
running and the bridge publishing, but HA's MQTT integration never confirmed,
so no entities.

---

## 4. "Default Gateway" is a label, not a concept

"Default Gateway" is only the name the bridge gave the first gateway row. It
should carry no meaning for the user, and the wizard is where it stops being
shown:

- The wizard **names** every gateway it sets up: from the nameplate
  (*"aGate 1234"*, last four of the serial) or the user's choice. Nothing is
  left called "Default Gateway".
- An unconfigured placeholder row is labelled **"aGate — not set up"**
  everywhere (selector, Settings), never "Default Gateway".
- The selector and Settings show **names only** — no "default" badge or wording.

What this does *not* fix: the internal id `default` is still load-bearing —
it owns the unprefixed MQTT topics and HA entity ids, several endpoints fall
back to it, and it can't be deleted. Removing that is the **"primary gateway"**
change (any gateway can be primary; delete allowed except the last), which needs
its own design because changing which gateway owns the legacy topics would
rename entities in users' HA. The wizard is written against "the primary
gateway", so it won't change when that lands.

## 5. Switching later

- **Demo → real:** *Settings → Site & Gateways → Run setup again → Connect my
  aGate*. Afterwards, offer to delete the demo gateway (and say its HA entities
  go too; its long-term statistics don't).
- **Real → add a demo alongside:** the existing *Add Gateway → Mock* in Settings.

---

## 6. API

All under `/api/setup`, admin-only (`require_capability("admin")`), so the CLI
can drive the same flow (`bridge setup …`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/setup/state` | `{state, has_real_gateway, has_mock, environment}` |
| `POST` | `/setup/state` | `{state: "skipped"}` etc. |
| `GET` | `/setup/subnets` | Candidate subnets (Supervisor / local interfaces), each with its source |
| `POST` | `/setup/scan` | `{subnets: [...]}` → `{scan_id}`; runs in the background |
| `GET` | `/setup/scan/{scan_id}` | Progress (`hosts_done/hosts_total`) and results so far |
| `POST` | `/setup/probe` | `{host, port, unit_id}` → one result row (the *Enter address* Test) |
| `POST` | `/setup/connect` | `{host, port, unit_id, name, device_type}` → configures + starts the default gateway |
| `POST` | `/setup/demo` | `{name, ac_type}` → creates the mock gateway |
| `GET` | `/setup/checklist` | The step 6 rows |

Scanning runs off the event loop (`asyncio.to_thread` or a small executor) and
never touches a configured gateway's host while that gateway is polling — the
probe would compete with the gateway's own connection. If the user's own aGate
is already configured and polling, it's shown as *"already set up"* without
probing.

---

## 7. Data

- `app_config.setup_state` — one of the four states in §2.
- No new tables. Scan results live in memory for the scan's lifetime only.

---

## 8. mDNS — where it helps (and where it doesn't)

franklinwh-modbus' scanner also does mDNS (`zeroconf`). Worth using, but not
for the aGate:

- **aGate: no.** It doesn't advertise over mDNS (Modbus-only devices rarely
  do), so finding it needs the port + SunSpec scan above.
- **Home Assistant and MQTT broker: yes, for Docker / standalone installs.**
  `_home-assistant._tcp` gives HA's `base_url` — prefill *Settings → Home
  Assistant → Add* — and `_mqtt._tcp` finds a broker *if* it advertises
  (Mosquitto doesn't by default, so treat it as a bonus). Step 6 can offer
  *"Found Home Assistant at http://192.168.1.10:8123 — use it?"*.
- **Add-on: not needed.** The Supervisor already provides both (PRs #4, #5).
- **Constraint:** mDNS is multicast, which doesn't cross Docker's bridge
  network — it works with host networking or on bare metal, and is silently
  absent otherwise. So it's an optional suggestion, never a required step, and
  `zeroconf` an optional dependency.

## 9. Testing

- **Unit:** subnet selection and limits; SunSpec base fallback order; result
  classification (aGate / other SunSpec / unknown / already set up); state
  migration for existing installs.
- **Against the gateway emulator** (`franklinwh-gateway-emulator`, once
  published — a test-only dependency first): a real aGate profile at PDU 0; the
  40000-alias option; the `session_held` / `busy_behaviour: hang` fault for the
  `unknown` row; `offline` for the "nothing found" path; and a
  `max_connections=1` check that the probe disconnects before the poller
  connects.
- **Console gate:** each wizard step, including re-opening it (Alpine teardown
  bugs show on the second open).
- **HA VM:** the add-on path end to end — Supervisor subnet discovery and the
  step 6 checklist against a real Mosquitto / MQTT integration.

## 10. Relationship to the gateway emulator

The demo path creates a gateway with `mock: true`. Today that runs on
`MockPoller`; a separate PR will swap it for an in-process instance of the
gateway emulator so demos go through the real poller, catalog and command path.
The wizard doesn't change when that happens. Nothing here depends on the
emulator being published, except the scan tests in §9.

## 11. Decisions (review of 2026-10-07)

1. **"Default Gateway" label** — cosmetic and must not carry meaning; the wizard
   names gateways and the UI stops showing "default" (§4). Removing the
   internal `default` id is a separate "primary gateway" design.
2. **Docker subnet** — derive it from the address the browser used to reach the
   bridge, then `LAN_SUBNET`, then host-networking interfaces, else blank
   (step 3R.1).
3. **Scan width** — /24.
4. **Several aGates found** — allow either: set up one, or several at once.
5. **Discovery code** — franklinwh-modbus' scanner, promoted into the package
   as `franklinwh_modbus.discovery` (released in v0.9.5).
6. **mDNS** — optional, for finding HA and an MQTT broker on Docker /
   standalone installs; not for the aGate (§8).
