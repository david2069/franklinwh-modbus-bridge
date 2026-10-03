# franklinwh-modbus-bridge -- Architecture & Design

**Status:** Draft v2.0
**Date:** 2026-05-21
**Owner:** david2069
**Repo (target path):** `~/dev/Claude/Projects/franklinwh-modbus-bridge`

---

## 1. Purpose

`franklinwh-modbus-bridge` is a Python integration application that polls a
FranklinWH aGate over **Modbus TCP** and publishes the data as **Home
Assistant entities via MQTT Discovery**.

It runs in two deployment modes from a single codebase:

1. **Home Assistant Add-on** -- installed via the HA Supervisor, ingress Web UI.
2. **Standalone Docker web app** -- runs anywhere, self-contained admin Web UI.

It is the *bridge* tier. The upstream `franklinwh-modbus` PyPI library
(local source: `/Users/davidhona/dev/modbus`) does the actual Modbus
register I/O; this project does not re-implement Modbus.

### 1.1 v1 Scope

v1 delivers the **core pipeline**: poll Modbus, capture the SunSpec model
catalog, and publish points as HA entities via MQTT Discovery. A REST API
serves as the **internal gateway** through which the Web UI, CLI, and all
future consumers interact with the system.

### 1.2 Deferred to v2+

| Feature | Rationale |
|---------|-----------|
| Transform engine | Tied to the REST listener/publisher gateway; no external consumers in v1. |
| REST listener for external consumers | Depends on transforms; v1 consumers are HA (via MQTT) and the admin UI. |
| mTLS / secrets hardening | Deliver a functional system first; harden post-v1. |
| Rate limiting / backpressure | Enforce after v1 when usage patterns are known. |
| Multi-gateway support | Schema accommodates it; UI and orchestration deferred until v1 is proven. |
| InfluxDB metrics | SQLite fallback is sufficient for v1; InfluxDB adds operational complexity. |
| Advanced UI screens | Queue explorer, REST tester, setup wizard -- deferred to v2. |

### 1.3 Relationship to reference projects

| Reference | Path | Role here |
|-----------|------|-----------|
| `franklinwh-modbus` | `/Users/davidhona/dev/modbus` | **Dependency.** Provides `FranklinWHController`, SunSpec read methods, `modbus_sunspec2_reader.py`. Read-only reference. |
| `franklinwh-ha-integrator` | `/Users/davidhona/dev/franklinwh-ha-integrator` | **Pattern reference only.** Proven FastAPI + HA addon + MQTT + backup design. Read-only; nothing is copied or modified. |

> Both reference repos are strictly read-only. No file edits, no git
> operations against them. All work lands in `franklinwh-modbus-bridge`.

---

## 2. Goals & Requirements

### 2.1 Functional requirements (v1)

- Poll a FranklinWH aGate via Modbus TCP using `franklinwh-modbus`.
- Programmatically capture & refresh the SunSpec model/point catalog
  emitted by `modbus_sunspec2_reader.py`, viewable in an admin screen.
- Publish polled data as HA entities via MQTT Discovery.
- REST API as the **internal gateway** for all admin, config, and
  operational interactions (Web UI, CLI, future consumers).
- Local SQLite database for configuration and basic metrics.
- Automated backups; safe manual backup & restore.
- Web UI with dark/light themes (dashboard, models, MQTT status, settings).
- CLI as a thin caller into the REST API layer.
- Runs as HA Add-on **or** standalone Docker, single codebase.

### 2.2 Non-functional requirements

- Python >= 3.11, compatible with the HA add-on base image and the
  `franklinwh-modbus` runtime (`pysunspec2`, `pymodbus>=3`).
- Virtual environments for all local development.
- Headless-first: GUI setup is optional, never mandatory.
- Crash-safe persistence (WAL, online backup).
- Graceful degradation: a missing MQTT broker or HA must not stop Modbus
  polling or the Web UI.

---

## 3. Deployment Topology

```
              +----------------------------------------------+
              |          franklinwh-modbus-bridge             |
              |              (single process)                 |
 Modbus TCP   |  +----------+   +---------------------+      |
+---------+   |  |  Poller  |-->|  Sample Bus (asyncio)|     |
| aGate   |<--+--|  (modbus |   +---------+-----------+      |
| (SunSpec|   |  |  library)|             |                   |
|  +ext)  |   |  +----------+             v                   |
+---------+   |                  +------------------+         |
              |                  | MQTT Publisher   |         |
              |                  | + HA Discovery   |         |
              |                  +--------+---------+         |
              |                           |                   |
              |  +----------------------------------------+   |
              |  |  REST API Gateway (internal)            |   |
              |  |  /api/admin/* /api/health /api/models/* |   |
              |  |  /api/mqtt/*  /api/config/*             |   |
              |  +----+-----------+-----------+------------+   |
              |       ^           ^           ^               |
              |       |           |           |               |
              |   +---+---+  +---+----+ +----+---+           |
              |   |Web UI |  |  CLI   | | Future |           |
              |   |(React)|  |(Typer) | |consumers|          |
              |   +-------+  +--------+ +--------+           |
              |                                               |
              |  +-------------------------------------------+|
              |  |  SQLite (config + basic metrics + backups) ||
              |  +-------------------------------------------+|
              |  +-------------------------------------------+|
              |  |  FastAPI app + React Web UI (8099)         ||
              |  +-------------------------------------------+|
              +----------------------------------------------+
      |                          |
      v                          v
 MQTT broker                Home Assistant
 (HA discovery)             (entities via MQTT)
```

### 3.1 Key architectural decision: REST as internal gateway

The REST API is the **single canonical interface** to the system. All
consumers -- Web UI, CLI, and future external integrations -- interact
through it.

- **Web UI**: React SPA calling REST endpoints over HTTP.
- **CLI (local)**: calls the API layer's Python functions in-process
  (no running HTTP server required for local operations).
- **CLI (remote)**: calls the REST API over HTTP (future, post-v1).
- **Future consumers**: same REST surface, extended with auth and
  transforms in v2.

This means one contract to test, no duplicated business logic, and a
clean extension path for transforms and external consumers.

### 3.2 Runtime environment detection

Reuse the proven pattern from the reference integrator
(`src/config/environment.py`): detect `ha_addon` | `docker` | `dev` from
`APP_ENV`, Supervisor tokens (`SUPERVISOR_TOKEN`), `/data/options.json`,
`/.dockerenv`, and cgroup inspection.

- `ha_addon` / `docker` -> data dir `/data`.
- `dev` -> data dir `./data`.

### 3.3 HA Add-on packaging

`config.yaml` add-on manifest with:

- `ingress: true`, `ingress_port: 8099`, `panel_title`, `panel_icon`.
- `arch: [armhf, armv7, aarch64, amd64, i386]`.
- `map: [data:rw]`, `hassio_api: true`, `auth_api: true`, `services: [mqtt]`.
- `options` / `schema` blocks for headless provisioning (Modbus host/port,
  MQTT, poll interval).

The Supervisor injects `SUPERVISOR_TOKEN`; the bridge uses it for MQTT
service discovery so the user need not re-enter broker credentials.

### 3.4 Standalone Docker

`python:3.12-slim` base, single Uvicorn worker, `/data` volume, healthcheck
on `/api/health`, port 8099. `docker-compose.yml` ships an optional MQTT
broker (Eclipse Mosquitto) for users who need one.

---

## 4. Component Design

### 4.1 Modbus Poller

- Wraps `franklinwh_modbus.FranklinWHController`.
- Single async poll loop targeting one aGate (v1).
- Calls the library read methods --
  `read_battery_status`, `read_grid_status`, `read_solar_status`,
  `read_nameplate`, `read_control_status`, `read_native_mode`,
  `read_alarms`, `healthcheck` -- on a configurable interval (default 30 s).
- The blocking `pymodbus` calls run in a thread executor
  (`asyncio.to_thread`) so they never stall the event loop.
- Emits a normalised `Sample` record onto the internal **Sample Bus**.
- Reconnect with exponential backoff; surfaces connection state via the
  REST API.

```python
@dataclass
class Sample:
    gateway_id: str
    ts: float                 # epoch seconds
    points: dict[str, float | int | str]   # canonical point id -> value
    quality: Literal["ok", "stale", "error"]
```

### 4.2 SunSpec Model Registry & Refresh

- A `ModelCatalog` service shells out to / imports
  `modbus_sunspec2_reader.py` (which supports `--json`) to capture the
  device's SunSpec models and points, **plus** the FranklinWH extension
  registers.
- The captured catalog is persisted to SQLite (`device_models`,
  `device_points` tables) with a `captured_at` timestamp and a content
  hash for change detection.
- The admin **Models** screen lists models/points, shows last refresh
  time, and has a **Refresh** button that re-runs the capture and diffs
  the result against the stored catalog (added/removed/changed points).
- The catalog drives which points are *available* to map to MQTT entities
  -- the UI offers them as a pick-list rather than free text.

### 4.3 MQTT Publisher + HA Discovery

Modelled on the reference integrator's queue-based design:

- **Publisher** -- background task drains a `(topic, payload, retain, qos)`
  queue; auto-reconnect with exponential backoff (`5 s ... 60 s`).
  - Publishes HA Discovery config once per catalog load.
  - Publishes state on every poll cycle; availability online/offline.
  - Topic scheme: `franklinwh/<gateway>/state/...`,
    discovery under `homeassistant/...`.
- **Command Listener** -- subscribes `franklinwh/+/control/+/set`, parses
  `(gateway, point, value)` and routes to the controller (e.g. mode
  changes, power setpoints) with validation.
- Library: `aiomqtt` (async-native). Basic TLS supported via broker config;
  mTLS deferred to v2.

### 4.4 REST API Gateway (Internal)

The canonical interface to the system. All admin, config, and operational
interactions flow through these endpoints.

**Admin routes:**
- `GET /api/health` -- liveness check.
- `GET /api/status` -- poller, MQTT, DB, backup component states.
- `GET/PUT /api/config/{section}` -- read/write configuration.
- `GET /api/models` -- list captured SunSpec models and points.
- `POST /api/models/refresh` -- trigger catalog re-capture.
- `GET /api/mqtt/status` -- broker connection, queue depth, last publish.
- `GET/PUT /api/mqtt/config` -- MQTT broker settings.
- `GET /api/mqtt/entities` -- mapped HA entities and their state.
- `POST /api/backup/create` -- trigger manual backup.
- `GET /api/backup/list` -- available backups.
- `POST /api/backup/restore/{id}` -- restore from backup.
- `GET /api/points` -- current polled point values.
- `GET /api/points/{id}` -- single point + metadata.
- `GET /api/logs` -- recent structured log entries.

**WebSocket:**
- `/ws/admin` -- live log streaming, poll status, connection events
  (avoids UI polling).

The Web UI consumes these endpoints exclusively. The CLI calls the
underlying service functions in-process (same Python API layer, no HTTP
round-trip needed when local).

### 4.5 Configuration Store -- SQLite

- `aiosqlite`, WAL journal mode, `foreign_keys=ON`, integer
  `SCHEMA_VERSION` with forward-only migrations.
- Holds: gateway config, point catalog, MQTT config, entity mappings,
  app settings, basic metrics, schema/startup logs.
- Single file under the data dir; included in backups.

### 4.6 Metrics -- SQLite (v1)

- v1 stores basic sample history in a SQLite `metric_samples` table with a
  TTL prune job (default 14 days) + periodic `VACUUM`.
- A `MetricsSink`/`MetricsReader` interface abstracts the backend so that
  InfluxDB can be added in v2 without changing consumers.
- HA provides its own history/charting for entities published via MQTT;
  local metrics are for the admin dashboard and diagnostics.

### 4.7 Backup & Restore

- `BackupManager` mirrors the reference design: SQLite **online backup
  API** (WAL-safe), runs every N hours (default 6), retention default 7
  days, rotating zip archives in `data/backups/`.
- A backup archive bundles: config SQLite snapshot and a manifest
  (versions, schema version, env).
- **Manual backup**: one-click in UI, `bridge backup create` in CLI.
- **Restore**: validates the manifest and schema version, takes a
  pre-restore safety snapshot, restores into a staging path, then
  atomically swaps -- never overwrites the live DB in place.
- A daily prune job trims the metrics table + `VACUUM`.

### 4.8 Admin Web UI

- **React + Vite + TypeScript + Tailwind CSS**, built to static assets and
  served by FastAPI (`StaticFiles`). Works under HA ingress and standalone.
- **Theming:** light/dark via Tailwind `class` strategy + CSS custom
  properties; `system` option follows OS.
- Live data via the `/ws/admin` WebSocket; falls back to REST polling.
- v1 screens:
  - **Dashboard** -- poller status, MQTT connection, last sample, system health.
  - **Models** -- SunSpec catalog browser + refresh + diff.
  - **MQTT** -- entity mapping, broker status, publish stats.
  - **Settings** -- gateway, MQTT, app configuration.
  - **Backups** -- list, create, restore.
  - **Logs** -- live structured log viewer.

### 4.9 CLI

- A `bridge` console entry point (Typer) for fully headless operation.
- v1 commands: `run`, `config get/set`, `models refresh`,
  `backup create/list/restore`, `status`, `mqtt test`.
- Calls the API layer's Python service functions **in-process** -- no
  running HTTP server required.
- Config is also loadable from a YAML/JSON file and from env vars / the HA
  add-on `options.json`, so the GUI is never required.

### 4.10 Process Supervision & Observability

- Staged async startup (lifespan): detect env -> load config -> init DB ->
  start poller -> start MQTT publisher -> mount routes.
- Structured JSON logging (`python-json-logger`), in-memory ring buffer for
  the Logs screen, configurable level per component.
- `/api/health` (liveness) and `/api/status` (component states).
- Graceful shutdown drains the MQTT queue and closes Modbus cleanly.

---

## 5. Data Model (SQLite, abridged)

```
schema_version(version PK, applied_at)
gateways(id PK, name, host, port, unit_id, enabled, created_at)
gateway_state(gateway_id FK, conn_state, last_ok_ts, last_error)
device_models(id PK, gateway_id FK, model_id, label, captured_at, hash)
device_points(id PK, model_db_id FK, point_name, type, unit, addr, writable)
entity_map(id PK, point_id, ha_component, ha_config_json, enabled)
mqtt_config(id PK, host, port, username, password, tls_mode)
app_config(key PK, value)
metric_samples(gateway_id, point_id, ts, value, quality)
startup_log(id PK, ts, event, detail)
```

---

## 6. Security (v1)

- Admin UI auth: username/password (optional); under HA ingress,
  Supervisor handles access.
- Secrets (MQTT passwords) stored in SQLite in v1 -- acceptable for
  local/addon deployments. Hardened storage (keyring, encryption at rest)
  is a v2 deliverable.
- No secret values written to logs or backup manifests.
- CSRF protection on state-changing admin routes; WebSocket origin checks.

---

## 7. Technology Stack

| Layer | Choice |
|-------|--------|
| Language | Python >= 3.11 |
| Web framework | FastAPI + Uvicorn |
| Modbus | `franklinwh-modbus` (-> `pymodbus`, `pysunspec2`) |
| Config/Metrics DB | SQLite via `aiosqlite` |
| MQTT | `aiomqtt` |
| Validation | `pydantic` v2 + `pydantic-settings` |
| Frontend | React + Vite + TypeScript + Tailwind |
| CLI | Typer |
| Logging | `python-json-logger` |
| Packaging | Docker (`python:3.12-slim`) + HA add-on `config.yaml` |
| Tests | `pytest`, `pytest-asyncio`, `respx`/`httpx` |

---

## 8. Future Work (v2+)

- **Transform engine** -- per-point pipeline (scale, rename, derived
  expressions, deadband, rate-of-change). Tied to the REST
  listener/publisher for external consumers.
- **REST listener for external consumers** -- `/rest/v1/points`,
  history queries, control commands with API-key auth.
- **mTLS** -- mutual TLS for MQTT and REST; central TLS manager with
  cert validation and expiry reporting.
- **Secrets hardening** -- encrypted storage or keyring backend.
- **InfluxDB metrics** -- time-series backend with retention/downsampling;
  energy dashboards with ECharts.
- **Multi-gateway** -- multiple aGate targets from a single instance.
- **HA WebSocket client** -- entity query/search, state subscription,
  service calls for deeper HA integration.
- **Advanced UI** -- setup wizard, MQTT queue explorer, REST API tester,
  user customisation (accent colour, density, chart palette).
- **Rate limiting / backpressure** -- bounded queues, publish throttling.

---

*See `IMPLEMENTATION_PLAN.md` for the phased build plan, repo layout, and
testing strategy.*
