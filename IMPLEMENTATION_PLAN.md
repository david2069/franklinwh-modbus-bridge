# franklinwh-modbus-bridge -- Implementation Plan

**Status:** Draft v2.0
**Date:** 2026-05-21
**Companion doc:** `DESIGN.md`

This plan turns the architecture in `DESIGN.md` into an ordered, testable
build. Each phase ends with a working, demonstrable increment and its own
tests. Phases are sequenced so that nothing depends on a component built
later.

v1 scope: poll Modbus -> capture SunSpec catalog -> publish MQTT HA
entities. REST API as the internal gateway. Minimal admin UI. Docker + HA
add-on packaging.

---

## 1. Repository Layout

```
franklinwh-modbus-bridge/
+-- CLAUDE.md                  # repo-scope rules + test/commit discipline
+-- DESIGN.md
+-- IMPLEMENTATION_PLAN.md
+-- README.md
+-- LICENSE
+-- pyproject.toml             # package + deps + console_scripts
+-- requirements.txt           # pinned runtime deps
+-- .gitignore
+-- .env.example
+-- config.example.yaml        # headless config template
|
+-- addon/                     # Home Assistant add-on packaging
|   +-- config.yaml            # addon manifest (ingress 8099)
|   +-- Dockerfile
|   +-- run.sh
|   +-- icon.png / logo.png
|
+-- docker/
|   +-- Dockerfile             # standalone image
|   +-- docker-compose.yml     # bridge + optional mosquitto
|   +-- docker-compose.prod.yml
|
+-- src/franklinwh_bridge/
|   +-- __init__.py            # __version__
|   +-- main.py                # FastAPI app + staged lifespan startup
|   +-- cli.py                 # Typer CLI entry point
|   +-- config/
|   |   +-- environment.py     # ha_addon | docker | dev detection
|   |   +-- settings.py        # pydantic-settings loader (env/file/options)
|   |   +-- manager.py         # AppConfig + DB-backed runtime config
|   +-- modbus/
|   |   +-- poller.py          # async poll loop over franklinwh-modbus
|   |   +-- catalog.py         # SunSpec model capture/refresh + diff
|   |   +-- sample.py          # Sample dataclass + Sample Bus
|   +-- publish/
|   |   +-- mqtt_publisher.py  # queue-based publisher + HA discovery
|   |   +-- mqtt_listener.py   # command topic subscriber
|   +-- store/
|   |   +-- db.py              # aiosqlite config store + migrations
|   |   +-- metrics.py         # MetricsSink/Reader (SQLite, v1)
|   |   +-- backup.py          # BackupManager (online backup + restore)
|   +-- api/
|   |   +-- admin.py           # admin REST routes (the internal gateway)
|   |   +-- ws_admin.py        # /ws/admin live stream
|   |   +-- health.py          # /api/health, /api/status
|   +-- web/                   # built React assets land here (StaticFiles)
|
+-- webui/                     # React + Vite + TS + Tailwind source
|   +-- package.json
|   +-- vite.config.ts
|   +-- tailwind.config.js
|   +-- src/...
|
+-- tests/
    +-- unit/
    +-- integration/
    +-- fixtures/              # captured SunSpec JSON, sample payloads
    +-- results/               # saved test runs (traceability)
```

Package name `franklinwh_bridge`; distribution name `franklinwh-modbus-bridge`.

---

## 2. Environment & Tooling

- Python 3.11+; develop inside a venv:
  ```
  cd ~/dev/Claude/Projects/franklinwh-modbus-bridge
  python3 -m venv venv && source venv/bin/activate
  pip install -e ".[dev]"
  ```
- `franklinwh-modbus` installed from PyPI for normal work; for co-development
  against local changes, `pip install -e ~/dev/modbus`
  (reference repo stays read-only -- editable install does not modify it).
- Frontend: Node 20+, `npm install` in `webui/`, `npm run build` emits to
  `src/franklinwh_bridge/web/`.
- Pre-commit: `python -c "import ast; ast.parse(open(f).read())"` syntax
  gate, `ruff`, `pytest -q`.

---

## 3. Phased Build

### Phase 0 -- Scaffold & CI *(foundation)*

- Create repo skeleton (S1), `pyproject.toml`, `requirements.txt`,
  `.gitignore`, `README.md`, `LICENSE`, `CLAUDE.md` (repo-scope rule).
- `git init`; first commit.
- GitHub Actions: lint + test matrix (3.11/3.12).
- **Deliverable:** repo installs, `pytest` runs (zero tests OK), CI green.
- **Tests:** import smoke test.

### Phase 1 -- Config, Environment & Store

- `environment.py` (port the detection heuristics), `settings.py`
  (pydantic-settings: env vars -> file -> HA `options.json` precedence),
  `manager.py` (`AppConfig`).
- `store/db.py`: schema v1, migration runner, config tables.
- **Deliverable:** app loads config in all three envs; DB created on boot.
- **Tests:** env detection unit tests (mock env/paths); migration applies;
  config precedence resolves correctly.

### Phase 2 -- Modbus Poller & Sample Bus

- `modbus/sample.py` (Sample dataclass, asyncio Sample Bus).
- `modbus/poller.py`: wraps `FranklinWHController`, runs blocking reads in
  `asyncio.to_thread`, reconnect/backoff, emits `Sample`s.
- `store/metrics.py`: basic `MetricsSink` writes samples to SQLite; TTL
  prune job.
- **Deliverable:** CLI `bridge run --once` polls a live or mock aGate and
  prints a normalised sample; samples stored in SQLite.
- **Tests:** poller against a mocked controller (no hardware); reconnect
  logic; thread-offload does not block the loop; metrics write/read
  round-trip; prune respects TTL.

### Phase 3 -- SunSpec Model Catalog

- `modbus/catalog.py`: capture via `modbus_sunspec2_reader.py --json`,
  persist `device_models`/`device_points`, content-hash + diff.
- CLI `bridge models refresh`.
- **Deliverable:** catalog captured to DB; diff reports added/removed/
  changed points.
- **Tests:** parse a captured JSON fixture; diff detection; idempotent
  re-capture.

### Phase 4 -- REST API Gateway & Health

- `api/health.py`: `/api/health` (liveness), `/api/status` (component
  states).
- `api/admin.py`: the internal gateway routes --
  `/api/config/*`, `/api/models/*`, `/api/points/*`, `/api/logs`.
- `main.py`: FastAPI app with staged lifespan startup, structured JSON
  logging + ring buffer.
- **Deliverable:** full admin REST API; health endpoints reflect real
  component state; the API is the canonical interface.
- **Tests:** route contracts (httpx test client); lifespan starts/stops
  cleanly; health reflects component state.

### Phase 5 -- MQTT Publisher + HA Discovery

- `publish/mqtt_publisher.py` (queue-based, HA Discovery, availability,
  reconnect/backoff), `publish/mqtt_listener.py` (command topics).
- `entity_map` table + mapping logic (catalog point -> HA component).
- REST routes: `/api/mqtt/status`, `/api/mqtt/config`, `/api/mqtt/entities`.
- **Deliverable:** entities appear in HA via Discovery; commands routed
  back to the controller; MQTT managed through the REST gateway.
- **Tests:** discovery payload shape; publish queue drain; command parse
  & validation against a mock broker; REST MQTT routes.

### Phase 6 -- Backup, Restore & CLI

- `store/backup.py`: scheduled online-backup, retention rotation, manual
  create, manifest, validated atomic restore with pre-restore snapshot.
- REST routes: `/api/backup/create`, `/api/backup/list`,
  `/api/backup/restore/{id}`.
- `cli.py`: Typer entry point -- `run`, `config get/set`, `models refresh`,
  `backup create/list/restore`, `status`, `mqtt test`.
- CLI calls service functions in-process (no HTTP required).
- **Deliverable:** automated + manual backups via REST; safe restore; CLI
  operational for headless use.
- **Tests:** backup integrity under simulated WAL writes; restore
  round-trip; restore aborts on manifest/schema mismatch; CLI commands
  exercise the API layer.

### Phase 7 -- Web UI

- `webui/` React + Vite + TS + Tailwind; v1 screens per `DESIGN.md` S4.8.
- Light/dark/system theming.
- `/ws/admin` WebSocket for live log/status streaming.
- Build outputs to `src/franklinwh_bridge/web/`; served by FastAPI
  `StaticFiles`; `package_data` includes built assets in the Python
  distribution.
- **Deliverable:** functional admin UI on port 8099, ingress-compatible.
- **Tests:** component tests (Vitest); theme toggle; build artifact served
  by FastAPI.

### Phase 8 -- Packaging: Docker & HA Add-on

- `docker/` standalone image + compose (optional Mosquitto).
- `addon/` HA add-on (`config.yaml`, `Dockerfile`, `run.sh`, icons).
- **Deliverable:** runs as standalone Docker **and** as an HA add-on from
  the same source.
- **Tests:** image builds; healthcheck passes; addon options.json parsed;
  ingress reachable.

### Phase 9 -- Integration Testing & Docs

- End-to-end test against a mock aGate: poll -> publish -> verify HA
  entities.
- Finalise `README`, install guide, CLI reference, `CHANGELOG.md`.
- **Deliverable:** v0.1.0 release candidate.
- **Tests:** full E2E; upgrade/migration from a seeded older schema.

---

## 4. Dependency Summary

Runtime: `fastapi`, `uvicorn[standard]`, `franklinwh-modbus`, `pymodbus>=3`,
`pysunspec2`, `aiosqlite`, `aiomqtt`, `pydantic>=2`, `pydantic-settings`,
`python-json-logger`, `typer`, `httpx`, `websockets`, `aiofiles`.

Dev: `pytest`, `pytest-asyncio`, `pytest-mock`, `respx`, `ruff`, `coverage`.

Frontend: `react`, `vite`, `typescript`, `tailwindcss`, `zustand` (state),
`vitest`.

---

## 5. Testing Strategy

- **Unit** -- pure logic (env detection, catalog diff, config precedence,
  metrics sink). No hardware, no network.
- **Integration** -- poller with mock controller, MQTT with mock broker, REST
  contract tests, DB migrations, backup/restore round-trip.
- **E2E** -- full startup against a mock aGate, poll -> publish, asserted
  in MQTT output.
- **Hardware tests** -- gated, opt-in (a real aGate); any change touching
  control/power logic needs explicit sign-off before commit.
- Save run artifacts to `tests/results/` for traceability.
- CI runs unit + integration on every push; E2E nightly.

---

## 6. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| `pymodbus` blocking calls stall the event loop | All Modbus I/O via `asyncio.to_thread`; poll loop never calls blocking code directly. |
| SunSpec extension registers vary by firmware | Catalog is captured per-device, hashed, and diffed; UI surfaces changes; nothing hard-codes register maps. |
| Restore corrupts live config | Atomic staged restore + pre-restore snapshot; manifest/schema validation; never overwrite in place. |
| Single codebase, two deploy targets diverge | `environment.py` is the only branch point; CI builds both images every push. |
| HA Supervisor API / token changes | MQTT Discovery is the primary HA interface (stable); direct WS client deferred to v2. |
| Scope creep | v1 scope is fixed to `DESIGN.md` S2.1; transforms, external REST, mTLS, multi-gateway, InfluxDB are explicitly deferred. |

---

## 7. Milestones

| Milestone | Phases | Outcome |
|-----------|--------|---------|
| **M1 -- Core pipeline** | 0-3 | Polls aGate, captures catalog, stores config + metrics. Headless/CLI only. |
| **M2 -- Gateway + publishing** | 4-5 | REST API gateway live, MQTT HA entities published. |
| **M3 -- Operability** | 6-7 | Backup/restore, CLI, admin Web UI. |
| **M4 -- Release** | 8-9 | Docker + HA add-on images, E2E tested, v0.1.0 RC. |

Each phase: code -> syntax check -> tests -> verify -> commit, one increment
at a time. Await review at phase boundaries.

---

## 8. First Actions (when work starts)

1. `cd ~/dev/Claude/Projects/franklinwh-modbus-bridge`
2. Create the Phase 0 skeleton and `CLAUDE.md` (repo-scope = this repo only).
3. `python3 -m venv venv && source venv/bin/activate && pip install -e ".[dev]"`
4. `git init` + initial commit.
5. Begin Phase 1.
