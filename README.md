# franklinwh-modbus-bridge

FranklinWH aGate Modbus TCP bridge -- polls SunSpec data and publishes Home Assistant entities via MQTT Discovery.

Runs as a **Home Assistant Add-on** or **standalone Docker** container from a single codebase.

## Status

v0.1.0 -- under active development.

## Quick Start

### Standalone Docker

```bash
docker compose -f docker/docker-compose.yml up
```

### Home Assistant Add-on

Add this repository to the HA Supervisor add-on store, then install and configure.

### Local Development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -e ".[dev]"
bridge run
```

## Configuration

Configuration is loaded with the following precedence (highest wins):

1. Environment variables (`MODBUS_HOST`, `MQTT_HOST`, etc.)
2. Config file (`config.yaml`)
3. HA add-on `options.json` (when running as an add-on)
4. Defaults

See `config.example.yaml` and `.env.example` for all options.

## CLI

```bash
bridge run               # start the bridge
bridge status            # show component status
bridge config get        # show current config
bridge models refresh    # re-capture SunSpec catalog
bridge backup create     # manual backup
bridge mqtt test         # test MQTT connectivity
```

## Architecture

See [DESIGN.md](DESIGN.md) for full architecture and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the build plan.

## License

MIT
