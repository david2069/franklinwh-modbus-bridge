# FranklinWH Modbus Bridge

Reads your FranklinWH aGate over Modbus TCP on your local network. Publishes
its data to Home Assistant as MQTT entities, and adds scheduling, tariff
tracking and battery control.

> **Unofficial software.** Not endorsed by, supported by or affiliated with
> FranklinWH. It writes commands to grid-connected battery hardware: wrong use
> can discharge your battery when you need it or import power you didn't
> intend to. **Don't contact FranklinWH support about it.** Report problems at
> <https://github.com/david2069/franklinwh-modbus-bridge/issues>.

## Before you start

- **Modbus TCP must be enabled on the aGate.** It's an installer setting; the
  bridge can't turn it on.
- **An MQTT broker** is needed for entities to reach Home Assistant. The
  **Mosquitto broker** app is the usual choice. You can install it before or
  after this add-on; the bridge picks it up by itself.

## First start

1. **Start** the add-on and open **FranklinWH** from the sidebar. There's no
   second login: Home Assistant has already signed you in.
2. Read and confirm the notice.
3. The **setup wizard** opens:
   - **Connect my FranklinWH aGate.** *Search my network* finds aGates on
     your LAN. *Enter address* tests a single IP or hostname; a Tailscale
     address works too. Choose one or more aGates, name them, and the wizard
     waits for the first reading.
   - **Explore with a demo gateway.** Simulated data and controls, with no
     hardware needed. A demo's energy sensors feed Home Assistant's long-term
     statistics, so try it on a test Home Assistant if you want to keep your
     Energy dashboard clean.
   - **I'll set it up myself.** Add gateways under **Settings → Gateways**.
4. The wizard ends with a **Home Assistant checklist**. The step people most
   often miss: in Home Assistant, go to **Settings → Devices & services →
   Discovered → MQTT → Configure**. Until that's done, no entities appear.

You can run the wizard again from **Settings → Gateways → Run setup again**.

## Configuration

All of these are optional. The setup wizard and the web UI cover the same
ground.

| Option | Meaning |
|---|---|
| `gateway_host` | aGate IP or hostname. Leave blank to use the setup wizard. If set, the bridge connects straight away and skips the wizard. |
| `gateway_port` | Modbus TCP port, normally `502`. |
| `gateway_unit_id` | Modbus unit id, normally `1`. |
| `poll_interval` | Seconds between reads. `10` is a sensible default. |
| `log_level` | `INFO` normally; `DEBUG` when diagnosing a problem. |

## What sets itself up

- **MQTT:** the broker that Home Assistant's MQTT service uses (Mosquitto),
  with its credentials. A broker you set in **Settings → Home Assistant → MQTT
  Broker** takes priority.
- **Home Assistant entities:** this Home Assistant is connected as *This Home
  Assistant*. Its entities can be used as conditions in schedules. You can add
  other Home Assistant instances in **Settings → Home Assistant**.
- **Time zone:** taken from Home Assistant, so schedules and tariff windows
  run on local time.

Your data (settings, schedules, history, backups) is kept in the add-on's own
storage, so updates don't lose it.

## Troubleshooting

| Symptom | Check |
|---|---|
| The wizard finds no aGate | Modbus TCP is enabled on the aGate; the aGate is on the same network as Home Assistant. Try *Enter address* with its IP. |
| *Unknown device listening on TCP port 502* where the aGate should be | The aGate may be rebooting, or another Modbus client may be busy with it. Try again in a minute. |
| No entities in Home Assistant | The wizard's checklist: broker connected, and Home Assistant's MQTT integration configured (**Discovered → MQTT → Configure**). |
| *Install Mosquitto* banner | Install and start the **Mosquitto broker** app. The bridge connects within about 30 seconds. |

The **Log** tab shows the bridge's log; set `log_level: DEBUG` for more detail.

## More

- Full install guide (add-on, Docker, development):
  [INSTALL.md](https://github.com/david2069/franklinwh-modbus-bridge/blob/main/INSTALL.md)
- Source and issues: <https://github.com/david2069/franklinwh-modbus-bridge>
