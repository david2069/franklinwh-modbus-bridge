# franklinwh-modbus-bridge

FranklinWH aGate Modbus TCP bridge -- polls SunSpec data and publishes
Home Assistant entities via MQTT Discovery.

Runs as a **Home Assistant Add-on** or **standalone Docker** container from a
single codebase.

## Before You Start

### Hardware Requirements

| Item | Detail |
|------|--------|
| FranklinWH aGate | Firmware with Modbus TCP enabled |
| Network access | TCP connectivity to the aGate on port 502 |
| MQTT broker | Mosquitto (HA add-on or external) |

### Software Requirements

| Item | Minimum version |
|------|----------------|
| Python | 3.11+ |
| pip | 23+ |
| Git | any recent |
| MQTT broker | Mosquitto 2.x recommended |

### Pre-flight Checklist

1. **Verify aGate network access** -- confirm you can reach the aGate IP
   (default `192.168.1.100`) from the machine that will run the bridge.
   ```bash
   ping 192.168.1.100
   ```
2. **Verify Modbus TCP port** -- confirm port 502 is open.
   ```bash
   nc -zv 192.168.1.100 502
   ```
3. **Verify MQTT broker** -- confirm your broker is running and reachable.
   ```bash
   mosquitto_pub -h localhost -t test -m hello
   ```
4. **Check Python version**.
   ```bash
   python3 --version   # must be 3.11+
   ```

## Installation

### Local Development

```bash
git clone <repo-url> && cd franklinwh-modbus-bridge
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
```

To verify:

```bash
bridge --version
pytest -m "not hardware"
```

### Co-development with Local Modbus Library

If you're working on the `franklinwh-modbus` library simultaneously:

```bash
pip install -e /path/to/franklinwh-modbus
```

### Standalone Docker

```bash
docker compose -f docker/docker-compose.yml up
```

### Home Assistant Add-on

Add this repository to the HA Supervisor add-on store, then install and
configure via the add-on settings page.

## Configuration

Configuration is loaded with the following precedence (highest wins):

1. Environment variables
2. Config file (`config.yaml`)
3. HA add-on `options.json` (when running as an add-on)
4. DB-backed overrides (MQTT settings)
5. Defaults

### Environment Variables

#### Gateway (Modbus)

| Variable | Default | Description |
|----------|---------|-------------|
| `MODBUS_HOST` | `192.168.1.100` | aGate IP address |
| `MODBUS_PORT` | `502` | Modbus TCP port |
| `MODBUS_UNIT_ID` | `1` | Modbus unit/slave ID |

#### MQTT

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_HOST` | `localhost` | MQTT broker hostname |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_USERNAME` | *(none)* | Broker username |
| `MQTT_PASSWORD` | *(none)* | Broker password |

#### Application

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `dev` | Environment: `ha_addon`, `docker`, or `dev` |
| `LOG_LEVEL` | `INFO` | Logging level |
| `DATA_DIR` | `./data` | Data directory (DB, backups) |

### Config File

Create `config.yaml` in the working directory:

```yaml
gateway:
  host: 192.168.1.100
  port: 502
  unit_id: 1

mqtt:
  host: mosquitto.local
  port: 1883
  username: mqtt_user
  password: mqtt_pass
```

### DB-backed MQTT Configuration

MQTT settings are stored in SQLite and can be changed at runtime via the
REST API without restarting the bridge:

```bash
# View current config
curl http://localhost:8000/api/mqtt/config

# Update broker settings
curl -X PATCH http://localhost:8000/api/mqtt/config \
  -H "Content-Type: application/json" \
  -d '{"host": "mosquitto.local", "port": 8883}'
```

Available MQTT config fields: `host`, `port`, `username`, `password`,
`tls_mode` (off/tls/tls_insecure), `enabled`, `client_id`, `qos` (0-2),
`retain_discovery`, `topic_prefix`, `discovery_prefix`.

## Startup

### Development Mode

```bash
source venv/bin/activate
bridge run
```

### With Environment Variables

```bash
MODBUS_HOST=192.168.1.100 MQTT_HOST=mosquitto.local bridge run
```

### Environment Detection

The bridge auto-detects its runtime environment:

| Environment | Detection method | MQTT broker |
|-------------|-----------------|-------------|
| `ha_addon` | `/data/options.json` exists | Auto-detects `core-mosquitto` |
| `docker` | `/.dockerenv` exists | Configured via env vars |
| `dev` | Fallback | Manual configuration |

When running as an HA add-on, the bridge automatically discovers the
Mosquitto add-on broker at `core-mosquitto:1883`.

## Monitoring

### Health Check

```bash
curl http://localhost:8000/api/status
```

Returns component-level status:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "environment": "dev",
  "components": {
    "poller": {"status": "not_started"},
    "mqtt": {
      "connected": true,
      "messages_sent": 142,
      "discovery_published": true
    }
  }
}
```

### MQTT Status

```bash
curl http://localhost:8000/api/mqtt/status
```

### Logs

```bash
# Recent log entries (in-memory ring buffer)
curl http://localhost:8000/api/logs

# Follow bridge logs directly
bridge run  # logs to stdout
```

### CLI Status

```bash
bridge status            # component health summary
bridge mqtt test         # test MQTT broker connectivity
```

## REST API Reference

### Health & Admin

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/status` | Health check with component status |
| GET | `/api/logs` | Recent log entries |
| GET | `/api/config` | Current application config |

### MQTT Administration

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/mqtt/config` | Current MQTT config (password redacted) |
| PATCH | `/api/mqtt/config` | Update MQTT config fields |
| GET | `/api/mqtt/status` | Publisher connection state and counters |
| POST | `/api/mqtt/reconnect` | Force MQTT reconnect |
| POST | `/api/mqtt/publish` | Re-publish HA Discovery payloads |
| POST | `/api/mqtt/unpublish` | Remove all discovery topics (tombstones) |
| POST | `/api/mqtt/test` | Test TCP connectivity to broker |
| GET | `/api/mqtt/detect-broker` | Auto-detect Mosquitto add-on |
| GET | `/api/mqtt/topics` | List all entity topics |

### SunSpec Catalog

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/models` | List captured SunSpec models |
| GET | `/api/models/{model_id}` | Model detail with points |
| POST | `/api/models/refresh` | Re-capture catalog from aGate |

### Backup & Restore

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/backups` | List available backups |
| POST | `/api/backups` | Create a new backup |
| POST | `/api/backups/{name}/restore` | Restore from backup |

## MQTT Entities

The bridge publishes 32 curated Home Assistant entities via MQTT Discovery.
All power values are in **kW** and energy values in **kWh**, matching the
FranklinWH HA integrator's conventions.

### Topic Structure

```
State:     franklinwh/{short_id}/{state_group}/{slug}
Discovery: homeassistant/{ha_type}/franklinwh_{short_id}_{slug}/config
Availability: franklinwh/{short_id}/availability
```

Where `{short_id}` is the last 8 characters of the aGate serial number.

### Battery

| Entity | Type | Unit | Device Class |
|--------|------|------|-------------|
| `battery_soc` | sensor | % | battery |
| `battery_soh` | sensor | % | -- |
| `battery_power_kw` | sensor | kW | power |
| `battery_current_a` | sensor | A | current |
| `battery_state` | sensor | -- | -- |

### Capacity

| Entity | Type | Unit | Device Class |
|--------|------|------|-------------|
| `total_capacity_kwh` | sensor | kWh | energy_storage |
| `available_capacity_kwh` | sensor | kWh | energy_storage |

### Grid

| Entity | Type | Unit | Device Class |
|--------|------|------|-------------|
| `grid_power_kw` | sensor | kW | power |
| `grid_voltage_v` | sensor | V | voltage |
| `grid_frequency_hz` | sensor | Hz | frequency |
| `grid_current_a` | sensor | A | current |
| `grid_apparent_power_va` | sensor | VA | apparent_power |
| `grid_reactive_power_var` | sensor | var | reactive_power |
| `grid_power_factor` | sensor | -- | power_factor |
| `grid_connection_state` | sensor | -- | -- |

### Inverter

| Entity | Type | Unit | Device Class |
|--------|------|------|-------------|
| `inverter_state` | sensor | -- | -- |

### Energy

| Entity | Type | Unit | Device Class |
|--------|------|------|-------------|
| `grid_export_kwh` | sensor | kWh | energy |
| `grid_import_kwh` | sensor | kWh | energy |

### Environment

| Entity | Type | Unit | Device Class |
|--------|------|------|-------------|
| `ambient_temp_c` | sensor | C | temperature |
| `cabinet_temp_c` | sensor | C | temperature |

### Solar & Load

| Entity | Type | Unit | Device Class |
|--------|------|------|-------------|
| `solar_power_kw` | sensor | kW | power |
| `home_load_kw` | sensor | kW | power |
| `pv_total_kw` | sensor | kW | power |

### Control

| Entity | Type | Description |
|--------|------|-------------|
| `operating_mode_sensor` | sensor | Current operating mode (read-only) |
| `control_mode` | select | Self-supply / TOU / Backup |
| `wset_enabled` | switch | Power setpoint enable |
| `power_setpoint_kw` | number | Active power setpoint (kW) |
| `operating_mode` | select | Writable operating mode |
| `self_reserve_pct` | number | Self-supply reserve (%) |
| `tou_reserve_pct` | number | TOU reserve (%) |
| `capacity_max_charge_kw` | number | Max charge rate (kW) |
| `capacity_max_discharge_kw` | number | Max discharge rate (kW) |

## Post-Installation Verification

After installing and starting the bridge, verify everything is working:

1. **Health check** -- confirm all components report OK.
   ```bash
   curl http://localhost:8000/api/status
   # Look for: "status": "ok"
   ```

2. **MQTT connection** -- confirm the bridge connected to your broker.
   ```bash
   curl http://localhost:8000/api/mqtt/status
   # Look for: "connected": true
   ```

3. **Test broker connectivity** -- run the built-in TCP test.
   ```bash
   curl -X POST http://localhost:8000/api/mqtt/test
   # Look for: "success": true
   ```

4. **Verify HA Discovery** -- check that entities appear in Home Assistant.
   Go to **Settings > Devices & Services > MQTT** and look for your
   FranklinWH device.

5. **Check entity topics** -- list all published MQTT topics.
   ```bash
   curl http://localhost:8000/api/mqtt/topics
   # Should show 32 entities with state and discovery topics
   ```

6. **Monitor MQTT traffic** -- subscribe to all bridge topics.
   ```bash
   mosquitto_sub -h localhost -t "franklinwh/#" -v
   ```

## Troubleshooting

### Bridge won't start

- Check Python version: `python3 --version` (must be 3.11+).
- Ensure the package is installed: `pip list | grep franklinwh`.
- Check for port conflicts: `lsof -i :8000`.
- Review logs for startup errors (the bridge logs to stdout).

### Can't connect to aGate

- Verify the aGate IP is reachable: `ping 192.168.1.100`.
- Confirm Modbus TCP is enabled on the aGate.
- Check port 502 is open: `nc -zv 192.168.1.100 502`.
- Try a different unit ID if the default (1) doesn't work.

### MQTT broker not connecting

- Test broker connectivity:
  ```bash
  curl -X POST http://localhost:8000/api/mqtt/test
  ```
- Verify broker credentials in the config.
- Check if the broker requires TLS -- update `tls_mode` in the MQTT config.
- For HA add-on mode, ensure the Mosquitto add-on is installed and running.
- Check auto-detection:
  ```bash
  curl http://localhost:8000/api/mqtt/detect-broker
  ```

### Entities not appearing in Home Assistant

- Confirm MQTT Discovery is published:
  ```bash
  curl http://localhost:8000/api/mqtt/status
  # Check "discovery_published": true
  ```
- Force re-publish discovery:
  ```bash
  curl -X POST http://localhost:8000/api/mqtt/publish
  ```
- Verify the HA MQTT integration is configured and connected to the
  same broker.
- Check discovery prefix matches HA's setting (default: `homeassistant`).

### Stale or duplicate entities

- Remove all discovery topics and re-publish:
  ```bash
  curl -X POST http://localhost:8000/api/mqtt/unpublish
  curl -X POST http://localhost:8000/api/mqtt/reconnect
  ```
- In HA, go to **Settings > Devices & Services > MQTT** and remove the
  old device entry before re-publishing.

### Entity values showing "unavailable"

- The bridge publishes an availability topic. If the MQTT connection drops,
  entities go unavailable automatically.
- Check that the poller is running and receiving data from the aGate.
- Verify the bridge has device info set (needed for topic generation).

### Database migration errors

- The bridge uses forward-only SQLite migrations (currently schema v3).
- If you see migration errors, check file permissions on the `data/`
  directory.
- Create a backup before troubleshooting:
  ```bash
  bridge backup create
  ```
- As a last resort, delete the database file and restart (the bridge
  will recreate it with a fresh schema):
  ```bash
  rm data/bridge.db
  bridge run
  ```

### Port 8000 already in use

- Find what's using the port: `lsof -i :8000`.
- The bridge REST API defaults to port 8000. If another service uses
  this port, set `PORT=8080` (or another free port) before starting.

### High memory or CPU usage

- Check the MQTT queue depth -- if the broker is slow or unreachable,
  messages queue up (max 1000).
- Reduce poll frequency if the system is resource-constrained.
- The SQLite WAL mode journal can grow large under heavy writes; the
  bridge checkpoints automatically.

## Running Tests

```bash
# All tests (unit + integration)
pytest

# Unit tests only (no hardware needed)
pytest -m "not hardware"

# With coverage
pytest --cov=franklinwh_bridge

# Lint and format
ruff check src/ tests/
ruff format src/ tests/
```

## CLI Reference

```bash
bridge run               # start the bridge
bridge status            # show component status
bridge config get        # show current config
bridge models refresh    # re-capture SunSpec catalog
bridge backup create     # create a manual backup
bridge backup list       # list available backups
bridge mqtt test         # test MQTT connectivity
bridge --help            # full CLI reference
```

## Project Structure

```
src/franklinwh_bridge/
  api/               REST endpoints (health, admin, mqtt_api)
  config/            Environment detection, settings, app config
  modbus/            Poller, SunSpec catalog, sample bus
  publish/           MQTT publisher, HA entity definitions
  store/             SQLite DB, migrations, metrics, backup
```

## License

MIT
