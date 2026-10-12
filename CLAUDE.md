# CLAUDE.md — franklinwh-modbus-bridge

## Scope

This file applies to `franklinwh-modbus-bridge` only. The reference repos
(`~/dev/modbus` and `~/dev/franklinwh-ha-integrator`)
are **read-only** — no file edits, no git operations against them.

## Project

FranklinWH aGate Modbus TCP bridge. Polls SunSpec data via `franklinwh-modbus`
and publishes Home Assistant entities via MQTT Discovery. REST API is the
internal gateway for all consumers (Web UI, CLI, future integrations).

## Development

```bash
cd ~/dev/Claude/Projects/franklinwh-modbus-bridge
source venv/bin/activate
pip install -e ".[dev]"
```

For co-development against local modbus library changes:
```bash
pip install -e ~/dev/modbus
```

## Commands

- `pytest` — run all tests (unit + integration)
- `pytest -m "not hardware"` — skip tests requiring a real aGate
- `ruff check src/ tests/` — lint
- `ruff format src/ tests/` — format
- `python tools/console_gate.py` — browser console gate against a **running**
  bridge; exits non-zero on any console error, uncaught exception or failed
  request. **Required after any JS/HTML change.** Drives the installed Google
  Chrome (no browser download). Safari can't do this job: `safaridriver`
  implements none of WebDriver's log endpoints, so the console can't be read.
- `bridge run` — start the bridge
- `bridge --help` — CLI reference

## Conventions

- Python >= 3.11, type hints on all public functions.
- `asyncio` throughout; blocking I/O via `asyncio.to_thread`.
- `pydantic` v2 models for all API request/response schemas.
- SQLite via `aiosqlite`, WAL mode, forward-only migrations.
- Tests: `pytest` + `pytest-asyncio`. Unit tests mock the controller,
  never require hardware. Hardware tests are gated behind `-m hardware`.
- One increment at a time: code -> syntax check -> tests -> verify -> commit.

## Live bridge safety

The dev compose bind-mounts `./src` and runs `uvicorn --reload`: **saving any
Python file under `src/` restarts the running bridge** (one restart per save).
A restart releases any active battery command and re-fires schedules whose
window is open. Before editing `src/` while the bridge runs: check no command
or schedule is active, keep 17:30–20:00 clear, and tell the owner first.
Full practice and audit trail: `docs/restarts-and-shutdown.md`.

## Architecture rules

- REST API is the canonical interface. All functionality is exposed through
  REST routes first, then consumed by the Web UI and CLI.
- CLI calls API layer functions in-process (no HTTP round-trip for local ops).
- `environment.py` is the only branch point between ha_addon/docker/dev.
- The `franklinwh-modbus` library handles all Modbus register I/O.
  This project wraps it; it does not re-implement Modbus.
