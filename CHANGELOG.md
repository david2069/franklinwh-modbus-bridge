# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project uses [Semantic Versioning](https://semver.org/). This file
didn't exist until 2026-07-15, well into active development — the
`[Unreleased]` entries below were reconstructed from the full git history
(no tagged release has ever been cut; `pyproject.toml` has stayed at
`0.1.0` throughout), grouped by feature area rather than reproduced
commit-by-commit. Going forward, new entries should be added here as work
lands, not backfilled later.

## [Unreleased]

### Added

- **Fixed / standing charges**: per-service `pricing.fixed_charges` (daily
  supply, metering, membership, …) accrued over the billing period and
  published as `fixed.*` sensors (per-day, accrued, projected, left to
  cover, days remaining) usable as automation conditions. Folded into the
  Energy Costs net total — live ("Net this period" = demand + export
  charge + fixed − export credit) and in the stored billing-period
  history (new `fixed_charges` column, migration 35; rows written before
  it keep 0).
- **Core bridge**: config layer (environment/settings/manager), SQLite
  store, Modbus poller with sample bus, SunSpec model catalog
  (capture/persist/hash/diff), REST API gateway, MQTT publisher with Home
  Assistant Discovery, backup/restore manager, full Typer CLI.
- **Web UI**: dashboard (power flow, SoC gauge, live + historic chart),
  controls, SunSpec Explorer, Settings, Logs tab, Modbus Sequencer editor.
  Multi-phase AC support, resizable panels, light/dark theme.
- **Multi-gateway (MG-1..MG-6)**: gateway registry, per-gateway MQTT
  namespacing, health checker, site aggregation, dashboard gateway
  selector/drill-down, Sequencer multi-gateway targeting, per-gateway log
  filtering, mock gateways with realistic diurnal patterns for demos.
- **Multi-phase / electricity services**: per-phase site aggregation
  (Topology A), per-gateway `phase_view` preference (Topology B), utility
  services card and gateway linkage.
- **Publishing Groups**: promote unpublished SunSpec catalog points into
  published HA entities, management UI in Settings, entity catalog API.
- **Scheduler (SCH1-3)**: declarative time → command-handler dispatch
  engine, FWHAI-style TOU editor UI, service/site multi-aGate fan-out,
  conflict policy (defer/override/wait) and release/hold window-exit
  behaviour, and (2026-07-15) true one-time (dated) entries — a `when_spec`
  can now target an exact calendar date instead of only a recurring
  weekday+time-window rule.
- **Events history**: durationed alarm/PV-Clip history table, dashboard
  chart legend for dashed event markers, persistent filterable Events
  card with time-range presets + custom range, discoverable severity
  chips (Fault/Warning/Info, doubling as a legend) and source filter.
- **Observability**: metrics snapshots, grid-mode events, alarm chart
  markers, `M701.MnAlrmInfo` and `M715` DERCtl points exposed in Live
  Points, persistent operational stats, metrics archival, DB storage
  dashboard.
- **Battery control**: MQTT command entities with watchdog (force
  charge/discharge/standby, target SoC, self/TOU reserve %, operating
  mode), Max Charge/Discharge presets, Command Power %/Duration controls.
- **Vendor/hardware documentation**: comprehensive `docs/vendor-issues.md`
  catalog of Modbus point gaps, guessed bitfields, and blocked writes,
  cross-referenced against FranklinWH's official SunSpec PICS certificate
  and SPAN's internal 2023 conformance review; scheduling & multi-aGate
  orchestration design; multi-phase topology design.
- Standalone diagnostic `tools/test_cloud_soc_persistence.py` — verifies
  whether `franklinwh-cloud`'s `set_mode(requestedSOC=...)` actually
  persists the reserve SoC on real hardware (readback gap the cloud
  client's own test suite leaves open).

### Changed

- **Add-on installs from the repository URL**: add
  `https://github.com/david2069/franklinwh-modbus-bridge` under Settings → Apps
  → App store → Repositories (or use the My Home Assistant button in
  INSTALL.md) and click Install. Replaces building a local add-on with
  `tools/build_addon.py` and copying it to `/addons`, which is removed.
- Rewrote the MQTT layer around curated `EntityDef` entities (kW/kWh
  units, DB-backed config, REST admin API) instead of auto-mapped SunSpec
  points.
- Reworked Max Charge/Discharge and Target SoC as presets to fix a race
  condition.
- Corrected `M701.Alrm` bit mapping (off-by-one, fabricated bit names) and
  switched alarm severity to a per-name computation instead of inheriting
  the row's blanket severity, against FranklinWH's official PICS
  certification.
- Pinned the `franklinwh-modbus` dependency progressively more tightly
  (immutable git ref → published release tag → full `constraints.txt`)
  for reproducible builds.
- Made the raw full-resolution metrics retention window configurable
  (`metrics_raw_age_days`) instead of fixed.

### Fixed

- Root-caused and fixed `0xFFFF` solar register corruption (concurrent
  Modbus access from overlapping read paths).
- MQTT discovery race condition; MQTT command subscription timing (was
  deferred until `device_info` was set, dropping early commands).
- Scheduler dispatch races: serialised `tick()` to prevent double-dispatch,
  suppressed re-firing of a watchdog-expired dispatch within its own
  window, and hardened startup so a flaky gateway start can't kill the
  scheduler/health checker for the whole process.
- Gateway lifecycle bugs: delete now removes FK children (catalog, state)
  before the row; stale row state resyncs on start/stop failures; a
  gateway's metrics are purged on delete and mock-gateway metrics are
  never persisted.
- Metrics archival: bucket-aligned cutoff to stop duplicate/orphaned rows;
  missing `RANGE_MAP` entries for several chart ranges (8h/12h/3d/5d);
  stale `CURRENT_SCHEMA_VERSION` bumped to match actual migrations.
- Several Web UI defects: Chart.js + Alpine.js stack overflow (chart
  moved to a closure variable), uninitialised-scale console errors,
  Sequencer editor scroll/syntax-highlighting regressions, dashboard
  flicker when switching gateways, Explorer "Hide zeros" flicker,
  Explorer "Read Now" resolving the wrong controller.
- `grid_mode` source annotation corrected to `701.DERMode`; operating-mode
  dropdown aligned to the library's `TOU` vocabulary.
