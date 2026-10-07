# First-Run Setup Wizard — Design

**Status:** proposal, for review before implementation
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
- that only one Modbus client may talk to the aGate at a time,
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
(new `app_config` key, see §6):

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
- **Nothing else is connected to it over Modbus.** The aGate accepts one Modbus
  client at a time; another integration or tool holding the connection will make
  the aGate look absent or unresponsive.
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
   - Docker (bridge networking): same problem, and there is no Supervisor to ask.
     Show the container's own subnet *and* a field to type the LAN subnet
     (prefilled with a guess such as `192.168.1.0/24`), with a note that Docker
     must be able to route to it.
   - Dev / bare metal: the machine's own non-loopback IPv4 subnets.
2. **Limits.** Only private ranges (RFC 1918, plus Tailscale's `100.64.0.0/10`
   only when typed explicitly). At most a /22 (1,022 hosts) per subnet without an
   explicit override. Read-only.
3. **Port check.** TCP connect to port 502, ~300 ms timeout, up to 64 at once.
   A /24 completes in well under 10 seconds.
4. **SunSpec check**, for every host with 502 open — **through
   `franklinwh-modbus`**, not raw Modbus calls in the bridge: the library owns
   all register I/O (CLAUDE.md). Read 2 holding registers at
   PDU **0**, then **40000**, then **50000**; stop at the first that returns the
   SunSpec marker `SunS` (`0x5375 0x6E53`). Real aGates answer at **0**
   (franklinwh-modbus' `FRANKLINWH_SUNSPEC_QUIRKS.md`, confirmed by the emulator
   work); the other bases cover firmware variation. Any unit id gets the same
   answer on an aGate, so use 1.
5. **Nameplate.** From the SunSpec chain, read model 1 (`Mn`, `Md`, `SN`, `Vr`).
   A FranklinWH result shows as *"FranklinWH aGate X — serial …, firmware …"*.
   Non-FranklinWH SunSpec devices (other inverters, meters) are listed greyed
   out as "not a FranklinWH device", so the user isn't left wondering.
   The library already covers this (v0.9.3): `FranklinWHController(host, port,
   unit_id, timeout=…, base_address=…)`, then `connect()` (which scans the
   SunSpec chain from `base_address`) and `read_nameplate()`, then `disconnect()`.
   Retry with `base_address` 40000 and 50000 when 0 finds nothing. The cost:
   `connect()` scans the *whole* model chain, so each probe holds the aGate's
   session for a second or two rather than one read. Fine for a one-off wizard;
   a lighter marker-only probe could be added to the library later if it matters.
6. **Disconnect immediately** after each probe — the aGate's single Modbus
   session must be free for the poller, and a probe left open looks to the user
   like the aGate went offline.

Results are a list; each row is selectable. Three outcomes need their own
wording:

| What the probe saw | Shown as |
|---|---|
| `SunS` + FranklinWH nameplate | **FranklinWH aGate** — selectable |
| Port 502 open, no Modbus answer within 2 s | *"A device at X has Modbus open but didn't answer. Another app may be holding the aGate's single Modbus connection."* |
| Nothing found | *"No aGate found on 192.168.1.0/24."* + links to the checklist and to **Enter address** |

#### Enter address

Host (IP or hostname — a Tailscale address works), port (502), unit id (1), and
**Test** — which runs steps 4–6 on that one host and shows the same result row.

### Step 4R — Confirm

Shows the nameplate and asks for a name (default `"aGate <last 4 of serial>"`)
and the **device type** — `aGate` or `MAC-1` (Meter Adaptor Collar) — prefilled
from the model string where it can be told apart.

**Confirm** sets the *default* gateway's host/port/unit/name/type (PATCH; PR #2
starts it as soon as it has a host). Using the default gateway keeps the legacy
MQTT topics and entity ids existing HA dashboards rely on.

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

## 4. Switching later

- **Demo → real:** *Settings → Site & Gateways → Run setup again → Connect my
  aGate*. Afterwards, offer to delete the demo gateway (and say its HA entities
  go too; its long-term statistics don't).
- **Real → add a demo alongside:** the existing *Add Gateway → Mock* in Settings.

---

## 5. API

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
probe would steal its single Modbus session. If the user's own aGate is already
configured and polling, it's shown as *"already connected"* without probing.

---

## 6. Data

- `app_config.setup_state` — one of the four states in §2.
- No new tables. Scan results live in memory for the scan's lifetime only.

---

## 7. Testing

- **Unit:** subnet selection and limits; SunSpec base fallback order; result
  classification (aGate / other SunSpec / session held / nothing); state
  migration for existing installs.
- **Against the gateway emulator** (`franklinwh-gateway-emulator`, once
  published — a test-only dependency first): a real aGate profile at PDU 0; the
  40000-alias option; the `session_held` / `busy_behaviour: hang` fault for the
  "didn't answer" row; `offline` for the "nothing found" path; and a
  `max_connections=1` check that the probe disconnects before the poller
  connects.
- **Console gate:** each wizard step, including re-opening it (Alpine teardown
  bugs show on the second open).
- **HA VM:** the add-on path end to end — Supervisor subnet discovery and the
  step 6 checklist against a real Mosquitto / MQTT integration.

## 8. Relationship to the gateway emulator

The demo path creates a gateway with `mock: true`. Today that runs on
`MockPoller`; a separate PR will swap it for an in-process instance of the
gateway emulator so demos go through the real poller, catalog and command path.
The wizard doesn't change when that happens. Nothing here depends on the
emulator being published, except the scan tests in §7.

## 9. Open questions


1. **Hide the unconfigured default** from the gateway selector and Settings list
   once a demo exists? It's confusing next to a working demo, but it's also where
   a real aGate will go later. Proposal: hide it from the *selector*, keep it in
   Settings with "not set".
2. **Docker subnet guess:** is a prefilled `192.168.1.0/24` helpful, or should
   the field start empty?
3. **Default scan width:** /24 only, or the host's real prefix up to /22?
4. **Two aGates found:** set up both (first as default, second as a new
   gateway), or one at a time?
