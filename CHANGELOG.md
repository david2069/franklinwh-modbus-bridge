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

- **Connection outages seen by the browser are logged.** When the UI's *can't
  reach the bridge* banner clears, the page reports the outage to
  `POST /api/ui/connection-outage`. The bridge logs a warning with how long it
  lasted, the failed requests, the last error, the user and their address.
  Previously the bridge never saw it, because the requests never arrived.
- **Add-on store page**: `addon/CHANGELOG.md` (HA's *Changelog* link, written for
  users), `addon/DOCS.md` (the *Documentation* tab), and an icon and logo. The
  add-on version is now **0.2.0**. It had stayed at 0.1.0, so HA never offered an
  update. The release steps are in `docs/build-and-dependency-policy.md` §6b,
  and `tests/unit/test_addon_release.py` keeps the add-on version, the package
  version and the add-on changelog in step.
- **First-run setup wizard** in the web UI: opens once for an admin on a fresh
  install (after the legal notice), asks *connect my aGate* / *explore with a
  demo* / *I'll set it up myself*, searches the network (or tests a typed
  address — a Tailscale address works), names each aGate from its nameplate
  ("aGate 0091"; several can be set up at once), waits for the first live
  reading, and ends with the Home Assistant checklist. The demo path explains
  what a demo can't do before creating one. Re-run from Settings → Gateways →
  *Run setup again* or the Dashboard's *Set up* link.
- **Setup wizard API** (`/api/setup/*`, admin-only; design in
  `docs/setup-wizard-design.md`): wizard state (`pending` / `done_real` /
  `done_demo` / `skipped`, migration 49 — existing installs never see the
  wizard), the LAN subnets to search for each install type (Supervisor host
  interfaces in the add-on; the address the browser used, then `LAN_SUBNET`,
  then host networking in Docker), a background subnet scan and single-address
  probe through franklinwh-modbus' read-only `discovery` API (a host a running
  gateway already polls is reported, never probed), connect (fills the primary
  gateway, or adds further aGates alongside), demo gateway creation (always a
  separate mock, never the primary), and a Home Assistant checklist (broker,
  HA connection, HA's MQTT integration, bridge entities in HA's registry). The
  UI follows in the next change.
- **HA entity access configures itself in the add-on**: the Home Assistant the
  add-on runs in is added automatically as instance `local` ("This Home
  Assistant"), through the Supervisor — no URL or long-lived token to create.
  Its entities are `ha:local:<entity>` conditions. Docker/standalone installs
  still add instances in Settings → Home Assistant.
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

- **franklinwh-modbus pinned to v0.9.5** (`FWM_REF` in both Dockerfiles; `>=0.9.5`
  in pyproject/requirements). Adds the read-only `discovery` API the setup
  wizard uses, and 0.9.4's fixes (`--dry-run` no longer writes; the
  unverified SPAN-unlock wording is gone — the bridge's own strip of it simply
  stops matching).
- **"Default Gateway" is gone from the UI.** It was only the label the first
  gateway row was created with: new installs name it *aGate*, migration 50
  renames an existing "Default Gateway" to *aGate* + the last four of its
  serial (a name you chose is kept), an unset-up placeholder shows as *aGate —
  not set up*, and the internal `default` id is no longer shown in Settings.
  The id itself is unchanged, so MQTT topics and HA entity ids are too.
- **Settings is split into sub-tabs**: *Site & Gateways*, *Home Assistant*,
  *Energy & Automation*, *Data & Backup* and *Admin*, instead of one long page
  of fifteen cards. The section is part of the URL
  (`?tab=settings&section=gateways`), so it survives a refresh and can be
  linked to; the dashboard's *Set up* / *Fix* buttons, HA Entities and Energy
  Costs now open the right section.
- **Add-on no longer requires an MQTT broker to install**: `mqtt:want`
  instead of `mqtt:need`. With no broker the bridge waits instead of retrying
  localhost, shows an *Install Mosquitto* prompt, and connects on its own as
  soon as the Mosquitto add-on is running — no restart.
- **Mock gateways accept control — simulated**: battery commands (force
  charge/discharge/standby), operating mode and Self/TOU reserves work on a
  mock from HA, the Controls tab and schedules, through the real command path;
  the mock's synthetic data follows them (battery power, SoC within the
  reserve, grid balance, mode). HA now gets the mock's control entities too.
  The Sequencer refuses a mock target with a clear message (it used to error)
  and leaves mocks out of *All Gateways*; SunSpec Explorer and Sequencer are
  dimmed with a *Needs a connected aGate* notice until a real gateway exists.
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

- **The bridge's own log now reaches the console.** `franklinwh_bridge` logged
  only to the in-app Logs tab, so Home Assistant's add-on *Log* and
  `docker logs` showed nothing but uvicorn's lines: no startup version, no
  warnings. Every console line now carries a date and time. The startup line
  (and the add-on's first line) shows the version. Routine successful `GET`s
  (UI polling, the `/api/status` health check) are left out of the access log
  unless `log_level` is DEBUG; writes and every error are still logged.
- **Sequencer: "couldn't reach the bridge" on runs that actually worked.** The UI
  dropped every request after 8 s, but `POST /api/sequence/execute` replies only
  once the whole sequence has finished, so any run with sleeps, verifies or
  waits was reported as a lost connection while the bridge carried on writing
  registers, and its step-by-step output was lost. The request now waits for
  the time the sequence can take, worked out from its own `sleep_ms`,
  `verify_timeout_ms` and `wait_for.timeout_ms` (per gateway for *All*), and
  if even that runs out it says the run may still be going rather than that the
  bridge is unreachable.
- **`.env.example` no longer sets `MODBUS_HOST=192.168.1.100`** — the made-up
  address the unconfigured-gateway fix removed from the code came back for
  anyone who copied the example. It is now commented out; leave it unset to use
  the setup wizard. `LAN_SUBNET` is documented there and passed through in
  `docker-compose.yml`.
- **Controls command the gateway you're viewing**: the Controls tab and the
  Battery Control *Force Release* always sent to the *default* gateway, so
  viewing a mock or a second aGate showed its state but released or forced a
  different gateway (or failed with "Command handler not available"). Error
  toasts now stay until dismissed, and refused commands are logged.
- **HA controls for additional gateways**: only the default gateway's MQTT
  command topics were subscribed, so the controls HA showed for a second aGate
  (or a mock) did nothing — and the command handler ignored which device a
  topic named, so subscribing them would have driven the *default* gateway.
  Every commandable gateway is now subscribed, and commands are routed by the
  device id in the topic.
- **No phantom default gateway**: with no gateway host configured, the bridge
  no longer creates "Default Gateway" at a made-up `192.168.1.100` and polls
  it forever (it could not be deleted). The default gateway is now created
  *unconfigured* — never started — until an address is set in Settings →
  Gateways, which starts it immediately. A `gateway_host` / `MODBUS_HOST` set
  after first boot is now applied (it used to be ignored). Migration 48 fixes
  existing installs; a gateway that ever connected is left alone.
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
