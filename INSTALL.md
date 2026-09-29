# Installing the FranklinWH Modbus Bridge

Three supported ways to run it. Pick one:

| Method | Best for | Auth | MQTT setup |
|---|---|---|---|
| **Home Assistant add-on** | You already run HA | Handled by HA (ingress) | Automatic |
| **Docker Compose** | Standalone / non-HA | Built-in login | Manual |
| **Development** | Working on the bridge | Bypassable | Manual |

---

> ## ⚠️ Unofficial software — please read
>
> `franklinwh-modbus-bridge` is **unofficial** and is **not endorsed, supported, or
> affiliated with FranklinWH** in any way. FranklinWH and aGate are trademarks
> of their respective owners, used here only to describe what this software
> talks to.
>
> It is provided **"AS IS"**, for **educational and informational purposes
> only**, without warranty of any kind, express or implied, including but not
> limited to warranties of merchantability or **fitness for any particular
> purpose**. The authors and contributors accept **no responsibility or
> liability** for any consequences of its use.
>
> By running it you acknowledge that you are accessing an interface not
> intended for your use, assume all risk associated with that — including risk
> to your hardware, your warranty and your electricity supply — and will use it
> responsibly.
>
> **This software issues write commands to grid-connected battery hardware.**
> Incorrect use can discharge your battery when you need it, import power when
> you did not intend to, or leave the system in an unexpected state.
>
> **Do not contact FranklinWH support about this software.** Bugs, defects and
> feature requests belong here, not with the vendor:
> <https://github.com/david2069/franklinwh-modbus-bridge/issues>


## 1. Home Assistant add-on (recommended if you run HA)

### Prerequisites

- Home Assistant OS or Supervised (the **Supervisor** is required — HA Container
  can't install add-ons; use Docker Compose there instead).
- An MQTT broker. The **Mosquitto broker** add-on is the usual choice — install
  and start it *before* this add-on and there is nothing further to configure.
- Your aGate's IP address, reachable from HA, with **Modbus TCP enabled** on the
  device (installer setting).

### Install

This repository is **private**, so Home Assistant cannot fetch it as an add-on
repository (it has no credentials) and the add-on cannot `pip install` the
bridge from git. It installs as a **local add-on** instead.

The add-on also cannot simply live in `addon/`: the Supervisor builds an add-on
with *its own directory* as the Docker build context, and `COPY` cannot reach
`..` for `src/`. Nor can the manifest sit at the repo root — `settings.py`
already reads `./config.yaml` as the bridge's own configuration.

So assemble a self-contained add-on folder first:

```bash
python tools/build_addon.py --verify     # --verify docker-builds it locally
```

That writes `dist/addon/` containing the manifest plus the source it needs.
Copy it to your HA host's `/addons` share, under any folder name:

```bash
scp -r dist/addon root@homeassistant:/addons/franklinwh_modbus_bridge
```

(Or use the **Samba share** / **Advanced SSH** add-on to drop it in `/addons`.)

Then in HA: **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**. The
add-on appears under *Local add-ons*. Click **Install** — the first build
compiles a few Python wheels and takes several minutes.

To update after changing the code, re-run `build_addon.py`, re-copy, then
**Rebuild** the add-on.

3. On the **Configuration** tab set:

   | Option | Meaning |
   |---|---|
   | `gateway_host` | aGate IP, e.g. `192.168.1.50`. May be left blank — you can add gateways in the UI later. |
   | `gateway_port` | Modbus TCP port, normally `502`. |
   | `gateway_unit_id` | Modbus unit id, normally `1`. |
   | `poll_interval` | Seconds between polls. `10` is a sensible default. |
   | `log_level` | `INFO` normally; `DEBUG` when diagnosing. |

4. **Start**, then open the panel from the sidebar.

### What the add-on does for you

- **No second login.** The UI is served through HA ingress; the Supervisor has
  already authenticated you, so the bridge accepts the session as an admin.
  There's no separate password to manage.
- **MQTT configures itself.** At startup the bridge asks the Supervisor for the
  registered MQTT service and uses those credentials. You only touch MQTT
  settings if you want a *different* broker from the one HA uses — an explicitly
  configured host always wins over discovery.
- **Data persists** in the add-on's own storage, so upgrades keep your
  schedules, history and settings.

### Multiple Home Assistant instances

Separate from the add-on's own HA: the bridge can read entities from **several**
HA instances at once (**Settings → Home Assistant**). Each gets a live
WebSocket connection, and its entities become automation conditions namespaced
`ha:<instance>:<entity>`. That works whether the bridge runs as an add-on or
standalone — useful when you have more than one HA, or want entities from a
second house.

---

## 2. Docker Compose (standalone)

```bash
git clone https://github.com/david2069/franklinwh-modbus-bridge.git
cd franklinwh-modbus-bridge
cp .env.example .env        # set MODBUS_HOST, MQTT_HOST, …
docker compose up -d
```

Open <http://localhost:8100>. First run prints a generated admin password to the
log — `docker compose logs | grep -i password` — change it after signing in.

MQTT is configured by hand here (**Settings → MQTT**): there is no Supervisor to
ask.

---

## 3. Development

```bash
python -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
pip install git+https://github.com/david2069/franklinwh-modbus.git@v0.9.3
bridge run
```

`ALLOW_INSECURE_AUTH=1` bypasses the login for local work. Never set it on
anything reachable from a network.

---

## Gateway types

Each gateway is declared as one of:

| Type | Device | Notes |
|---|---|---|
| `agate` *(default)* | aGate X | Full battery system: battery control, inverter, the complete SunSpec model set. |
| `mac1` | MAC-1 Meter Adaptor Collar | **Metering only.** No battery to command, and far fewer SunSpec models and points — so models absent on a collar are expected, not a fault. |

Set the type when adding a gateway. It's configuration rather than something the
device reports: a collar can't announce over Modbus that it's a collar. The
model string shown in the UI still comes from the device's own nameplate.

Automations can reference the identity of whichever gateway they target:

- `gateway.serial` — nameplate serial number. The only identifier that survives
  renaming a gateway, so prefer it when a rule must mean one specific box.
- `gateway.model` — model string from the nameplate, e.g. `aGate X`.
- `gateway.device_type` — `agate` or `mac1`.
- `gateway.battery_capable` — false for a MAC-1; useful as a guard on any rule
  that issues a battery command.

---

## Upgrading

**Add-on:** Settings → Add-ons → FranklinWH Modbus Bridge → **Update**. Database
migrations run automatically at startup.

**Docker:** `git pull && docker compose up -d --build`.

Migrations are forward-only. Take a backup (**Settings → Backup**) before a
major upgrade if the data matters to you.

---

## Troubleshooting

**"Can't reach the bridge" banner.** The UI can't reach the API. Over a VPN,
check the tunnel is up — the banner distinguishes "this device is offline" from
"online but the bridge isn't reachable".

**No entities in Home Assistant.** Check MQTT is connected (**Settings → MQTT**)
and that HA's MQTT integration is set up. The bridge publishes via MQTT
Discovery, so entities appear by themselves once the broker link works.

**Gateway shows "unreachable".** Confirm Modbus TCP is enabled on the aGate
(installer setting), the IP is right, and nothing else holds the connection —
the aGate accepts only one Modbus client at a time.

**Times look wrong.** Schedules run on the *bridge's* clock. If you're viewing
from a different timezone than the host, "Daily 18:00" means 18:00 where the
bridge is. The Schedule timeline is drawn on the bridge's clock for the same
reason.
