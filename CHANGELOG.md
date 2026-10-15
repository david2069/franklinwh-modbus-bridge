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

- **Publishing to Home Assistant is opt-in per gateway, and switchable live** (#96).
  - The setup wizard's Home Assistant step asks "Publish *<gateway>*'s entities
    to Home Assistant?" for the gateway it just set up (`POST
    /api/setup/publish-to-ha`, admin only). A demo gateway is now created with
    `publish_to_ha = 0`; a real aGate stays published. As an add-on this is the
    only HA question; in Docker it is shown once "use with Home Assistant" is Yes.
  - Settings → Home Assistant → "Publish to Home Assistant": a switch per
    gateway (`PATCH /api/gateways/{id}` `publish_to_ha`) and one for MQTT
    publishing as a whole (`PATCH /api/mqtt/config` `enabled`). Both apply at
    once. Off removes that gateway's entities from HA.
  - `POST /api/gateways` accepts `publish_to_ha`, set before the gateway first
    starts. The wizard checklist lists each gateway's publish state, and shows
    "Bridge entities in Home Assistant" as skipped when nothing is published.

- **Interim REST API access: `api` role, account expiry, sign-in audit view,
  failed-login throttling** (#99).
  - Role `api` = `view` + `control` only (owner, 2026-10-15): reads and
    battery commands over `/api/…`, no admin surface. REST only: `/`, `/user`
    and `/login` answer 403 with a "This account is for the REST API only"
    page and a Sign out button.
  - `users.expires_at` (epoch s, null = never). Sign-in after it is refused
    (same 401 as any failure; audit `login_failed` "account expired"), and an
    open login ends on its next request (end reason `expired`, ended by
    "account expiry date"). Settings → Admin → Users: set/clear an expiry
    (end of the chosen day, bridge time), "expired" / "expires in N days"
    badges, and a Generate button for a long random password. The last
    enabled admin can't be given an expiry; `bridge recovery
    reset-admin-password` clears one.
  - Migration 58 rebuilds `users` to widen the role CHECK and add
    `expires_at`, keeping every row, id and password hash.
  - Settings → Admin → "Sign-in activity": `GET /api/auth/events` (now with
    `username` and `event` filters), newest first, bridge-time column.
  - Throttling per (username as typed, client IP): 5 failures within 15 min
    → 429 with `Retry-After` for 60 s, password not checked meanwhile, one
    `login_throttled` audit event (WARNING) per lockout; a successful sign-in
    resets it. Same answer for names that don't exist. In memory: a restart
    clears it.
  - `directconnectapi` is not seeded; the owner creates it in Users.

- **Weather + solar forecast from Open-Meteo** (#88), matching the Local Bridge.
  - Free, no key. Off until a latitude and longitude are set in Settings →
    Site & Gateways → "Location & Solar" (or read from a configured Home
    Assistant). PV array: kWp (5.0), tilt (20°), azimuth (0°; Open-Meteo
    convention 0 = S, -90 = E, 90 = W, ±180 = N).
  - Hourly kW = GTI/1000 × kWp × 0.85. Cached 15 min, 15 s timeout. A failed
    fetch returns the previous forecast marked `stale`, or an `error` stub.
  - Automation sensors, same names as the Local Bridge: `weather.temp_c`,
    `weather.condition`, `weather.code`, `solar_forecast.today_kwh`,
    `.today_peak_kw`, `.tomorrow_kwh`, `.remaining_kwh` (site local time),
    `.day_pct` and `.vs_expected_pct` (actual vs forecast). Missing data is
    None, so a condition on it fails closed.
  - REST: `GET /api/solar/forecast?force=`, `GET`/`PUT /api/solar/settings`,
    `POST /api/solar/location/from-ha`. No migration (stored in `app_config`).
  - Dashboard "Solar Forecast" card: weather, today/tomorrow kWh and peak,
    hourly bars with a NOW marker, actual vs forecast. Times in the bridge's zone.

- **Home Assistant dashboard card YAML** (#86). `GET /api/ha/dashboard-yaml`
  (`instance`, `layout=grouped|single`, `diagnostics`) and a "Dashboard card"
  button on the HA Entities tab (shortcut in Settings → MQTT) give copy-paste
  YAML of plain core `entities` cards, the same shape as FWHAI's.
  - Entity ids come only from HA's device and entity registries, so renamed
    and `_2` ids are right. A row counts as ours only if its device carries an
    identifier this bridge publishes and its unique_id starts with the current
    `<topic_prefix>_<short_id>_`; another producer's identical
    `franklinwh_…` unique_ids are left out. Disabled entities are dropped.
  - Groups: Power & Energy, Battery, Controls, Status, then Diagnostics only
    when asked for. With several gateways, card titles name the device.
  - If HA isn't connected or its registry can't be read, the route returns an
    error and no YAML; it never guesses ids.
  - Copy works over plain-http ingress: it falls back from
    `navigator.clipboard` to `execCommand('copy')`, then to "Press Ctrl/Cmd+C".

- **Truthful dispatch: verify, retry, re-assert, notify** (#58, Phase 1 of
  docs/dispatch-reliability-design.md).
  - A battery command counts as active only if the library accepted it AND
    WSetEna reads back as 1. Otherwise it is recorded as failed, with the reason.
    A release reads WSetEna back too.
  - Scheduled dispatches: a failed send is retried with backoff until the
    window ends. While running, a dropped setpoint is re-asserted (up to 3 times),
    and after 90 s battery power is checked: `verified` or `not_effective`.
  - The entry's HA notify target is told of failure, recovery, giving up and
    not-effective.
  - `dispatch_outcomes` (migration 56): one row per command with requester,
    requested vs actual power and final state;
    `GET /api/gateways/{id}/dispatch-outcomes`.
  - Who and how for every command (#59): `user:<name>`, `mqtt`,
    `schedule:<id>`, `bridge:<reason>`. Dispatches the bridge did not send are
    detected. The Sequencer needs control permission and records each run.
- **Link-health measurement, `link_stats`** (#74, Phase 0b, migration 57).
  - Per gateway per hour, kept 90 days: every aGate request by kind and code
    path, with outcome (no-reply timeouts separate from closed connections) and
    latency p50/p95/max.
  - Also connections opened, reconnects and library rescans, failed-poll runs,
    polls served from cache, overlapping requests and socket counts.
  - `GET /api/gateways/{id}/link-stats`. Measurement only; dispatch is unchanged.
- **Point history records raw 701.DERMode and register 15016** (#76, #36).
  DERMode is the grid-mode bitfield, so an islanding event's "PV Clipped"
  minute can be told apart (raw 4 vs 6). 15016 is an unmapped status register
  that changes with off-grid and dispatch. Both are recorded only once ticked
  in Settings → Point history.
- **Point history records raw vendor registers 15013 and 15035** (#73): battery
  W at 1 W and SoC x10 (one site's readings, not documented). They can be
  charted from the DC card. They are only recorded once ticked in Settings →
  Point history.
- **docs/dispatch-reliability-design.md** (#57): dispatch reliability and
  Modbus connection design, approved 2026-10-12.
- **Login record, audit trail and "You have been logged out" with the reason**
  (#50).
  - Each login is recorded: who, when, IP and browser.
  - Logins, failed logins, logouts and ended logins are audited, and also appear
    in the Logs tab.
  - Admins can list and revoke logins over REST.
  - A page that loses its login shows why: logged out, account disabled,
    deleted or password reset by an admin, revoked, expired, or dropped by the
    browser. It used to show "Disconnected".
  - Migration 55.
- **Break-glass admin recovery** (#52): `docker exec -it fwhbridge-app bridge
  recovery status | reset-admin-password`. See docs/admin-recovery.md.
- **Setup wizard "Your login" step and self-service password change** (#54).
  - The step flags guessable passwords such as the compose default admin/admin
    and changes yours in place.
  - `POST /api/auth/password` ends your other logins and keeps this one.
- **docs/security.md**: roles, HTTP/HTTPS settings, how logins end, the audit
  trail, recovery, known gaps and a setup checklist.
- **docs/point-history.md** (#47).

### Changed

- **An open-ended dispatch's log line names the aGate revert timer** (#72):
  `[no time limit; aGate revert timer 7200s, not enforced by the aGate (PICS
  Issue 4)]`. It used to say only `[no time limit]`.
- **Usernames are case-insensitive** (#50). A name differing from an existing
  one only in case is refused.
- **The live bridge runs the code baked into its image** (#48). No `./src` mount
  and no `uvicorn --reload`, so saving a file no longer restarts it.
  `docker-compose.dev.yml` restores hot-reload for development.

### Fixed

- **Writes need the admin role** (#102). Most feature routers checked only for
  a session, so a signed-in **viewer** (and any other role) could reconfigure
  gateways, MQTT and modules, stop gateways, edit and run schedules, and
  restore backups. 61 write routes were open.
  - `require_admin_for_writes` is now on every feature router. Reads stay open
    to any signed-in role. A write route with its own role/capability gate
    keeps it, so `user` and `api` can still use the per-gateway command
    (`control`).
  - Open to any session: login, logout, own password, the disclaimer, and
    connection-outage reports.
  - A regression test walks every route and fails on a new write route without
    a gate.
  - Unchanged: non-admins never see the admin UI (`/` sends them to `/user`),
    and HA ingress users are admin.

- **Command replies report what this call did** (#97). `POST /api/command`
  and `POST /api/gateways/{id}/command` answered from the command handler's
  leftover state. An unknown slug or action therefore came back
  `ok: true, result: "Sent"`, or with an earlier command's text.
  - An unknown slug now gets **400**, listing the known slugs.
  - An unknown `battery_command` action (e.g. "Force Fly") returns `ok: false`.
  - Staging slugs (power, power %, duration, target SoC) return `ok: true`
    with "Staged …", meaning stored, not sent. Changing the power of an active
    dispatch also includes the re-send's result.
  - `CommandHandler.handle_command` returns `{ok, result}` for the call.

- **The primary gateway ignored "Publish to Home Assistant"** (#96). Its
  checkbox in the gateway edit modal did nothing. Off now tombstones its
  discovery topics (the full entity registry plus promoted points, under the
  configured prefixes) and its availability, and drops its samples without the
  "unregistered gateway" warning. On binds it again and re-publishes discovery.
  Live, no restart. With it on, the published topics and payloads are unchanged.
- **MQTT "Enabled" needed a restart** (#96). Saving it now starts the
  publisher, or publishes "offline" availability and stops it. Entities are
  not removed (Unpublish does that). An add-on still waiting for Mosquitto
  starts when the broker appears, reading the saved value. `POST
  /api/mqtt/reconnect` no longer starts a disabled publisher.

- **Setup wizard's Home Assistant step, and tab naming** (#94).
  - It asks "Will you use this bridge with Home Assistant?". Answering No
    (stored as `setup_use_ha`; `POST /api/setup/use-ha`) shows every HA item as
    skipped (–), not failed, and says HA can be connected later. Before, a
    standalone install always finished setup on two red ✗. The HA add-on is
    not asked, because it runs inside HA and the Supervisor sets up both links.
  - Each link says what it is for: the MQTT broker carries entities *into* HA;
    the HA connection lets the bridge *read* HA.
  - The two HA-side checks say why they show "?": "Can't check — no Home
    Assistant connected to the bridge".
  - The "HA Entities" tab is now called **Home Assistant**, to match the other
    repos. The tab id is unchanged.
  - The top bar title read "Settings" on the Schedule, HA and Energy Costs
    tabs; every tab now has its own title.

- **MQTT Topic Prefix / Discovery Prefix settings now apply** (#90, part 1 of
  #87). They were stored but ignored, because every topic, unique_id and device
  identifier was hard-coded `franklinwh` / `homeassistant`.
  - The stored values are now used throughout. The defaults give byte-identical
    output, so nothing changes for existing installs.
  - Incoming commands are acted on only under our own topic prefix.
  - The setup "entities in HA" check counts only our namespace.
  - Changing either prefix is refused (409) and the Settings fields are
    read-only. A change re-creates every HA entity, so it waits for the
    confirmed change flow (#87). The values must be MQTT- and unique_id-safe.

- **Top bar status indicators stay visible on medium windows** (#82). They
  were shown only at 1280 px and wider, so a zoomed-in or medium window
  dropped them silently, including the red Grid dot for an islanded aGate.
  From 768 px they now show as dots with tooltips; the text labels return at
  1280 px.
- **Events History grid-mode intervals** (#78). The 30-day view stretched a
  51 s "PV Clipped" to 2 h and never showed Grid Forming: rows were bucketed
  and `MAX(grid_mode)` compared the labels as text. Intervals are now built
  from the raw transitions, and every bucketed view (charts, the 5-minute
  archive) ranks Grid Forming over PV Clipped over Grid Following.
- **Times are shown in the bridge's timezone, labelled** (#79). The UI used the
  viewing device's zone with no label, so a device 2 h ahead showed the 18:00
  discharge as 20:40.
  - Every time is now in the bridge's zone (the one its schedules and records
    use), from `GET /api/system/timezone`.
  - The zone is named once per view: column headers, chart axis titles and
    range pickers, e.g. "TIME (AEDT)".
  - When the device is in another zone, the top bar, hover titles and chart
    tooltips also give its own time, labelled ("your time 09:56 NZDT").
  - Date-range inputs and "today" (energy flow, history presets, schedule
    dates) use the bridge's calendar too.
- **SunSpec Explorer reset to model 1 and reloaded its catalog every 10 s**
  (#69, #70). The gateway watcher fired on every refresh.
- **Chart modal on a phone** (#65, #68): it now opens full-screen, its controls
  wrap instead of running off the edge, and stray taps no longer close it.
- **Dropdowns are styled throughout the UI** (#66, #68): a themed chevron in
  place of the platform's native look; the dark theme sets `color-scheme`.
- **The aGate's LAN address was removed** from the public docs and test data
  (#72).
- **Battery health (713.Sta) decoded per SunSpec: 0 OK, 1 Warning, 2 Error**
  (#51). The poller had ERROR = 3, and the alarm history used a guessed
  activity enum (OK showed as "Idle"). Older alarm rows are re-labelled when
  read.
- **Point History switch saves when ticked** (#47). It was form state only, so
  recording "never stayed on".
- **INSTALL.md** (#52): `ALLOW_INSECURE_AUTH` allows plain-HTTP login and does
  not bypass it; the shipped compose seeds admin/admin.

## [0.2.3] - 2026-10-08

### Changed

- **Setup wizard follow-ups** (from the first run against a real aGate):
  - **Timezone is a wizard step.** While it's unconfirmed, the wizard asks *Is
    this your timezone?* before the checklist, and says where to change it
    (Home Assistant's time zone, or the container's `TZ`). On a fresh install
    it used to be a separate banner behind the wizard.
  - **One aGate found → already ticked**, so *Next* works straight away.
  - **Search suggests the networks of aGates already set up**, and the hint
    covers aGates reached over Tailscale or a VPN, whose network the bridge's
    host isn't on.
  - **The checklist counts only the current gateways' entities in HA**, and
    reports leftovers from removed gateways separately with where to delete
    them. Leftovers had been making that item pass with nothing current.
  - **The top bar says *No gateway yet*** instead of *never* until an aGate
    or demo gateway exists.

### Fixed

- **Battery control panel** (the Release modal):
  - **WSetPct** shows the scaled value (*100%*) instead of the raw register
    with a % sign (*1000%*). The raw value is on hover.
  - **Watchdog** shows *No limit* for a dispatch without a time limit,
    instead of *0m 0s* in red, which looked expired.
  - A new **Target SoC** row.
- **The control log records which gateway a command was for.** Every
  dispatch was logged as the primary gateway's. Changing the target SoC is now
  logged, and each *command sent* entry includes its time limit and target SoC.
- **Crash recovery now covers every gateway.** The saved active-dispatch state
  table only allowed one row, so any gateway other than the primary failed to
  save its state (logged only as a warning). It's now one row per gateway
  (migration 52).
- Corrected code comments that said the aGate's revert countdown (`RvrtRem`)
  never moves. It counts down; what doesn't happen at 0 is the revert.
- **Settings → Admin → Feature modules no longer says a disabled feature's API is
  blocked.** Turning a module off removes it from the sidebar. Only some modules
  (Schedule, HA Entities, Energy Costs) also gate their API, so the wording
  promised more than Explorer, Sequencer and Logs do.

## [0.2.2] - 2026-10-08

### Changed

- **SunSpec Explorer follows the gateway selector.** Catalog, *Refresh Catalog*
  and on-demand model reads were hard-wired to the first gateway; they now take
  `?gateway_id=` and the Explorer uses the gateway being viewed (the primary
  one for a demo gateway or *Site*). The Dashboard's device bar also updates
  when you switch gateway.
- **SunSpec Explorer and Sequencer are off by default on new installs.** They
  are expert tools, and the Sequencer writes raw registers to a live battery.
  Turn them on in Settings → Admin → Feature modules. Migration 51 keeps them on
  for existing installs, unless you had already switched them off.

### Fixed

- **Dashboard "IP --" and SunSpec Explorer "GATEWAY :502" for a gateway set up in
  the UI.** `GET /api/gateway` and `/api/gateway/test` returned `MODBUS_HOST` from
  the environment, which is empty whenever the aGate was added by the setup
  wizard or in Settings. They now read the gateway's stored settings and take
  `?gateway_id=`. The primary gateway is used when none is named.
- **Settings → Home Assistant → MQTT Broker showed `localhost` / `(anonymous)`
  in the add-on**, under *Configured automatically from Home Assistant's MQTT
  service*. The card read the stored config, but the broker discovered from the
  Supervisor is never stored. `/api/mqtt/status` now reports the broker in use
  (`broker: {host, port, username, source}`), and the card shows that.
- **Unreadable text in light mode.** The near-white text shades written for the
  dark theme's tinted banners (`text-cyan-100`, `text-amber-100/200`,
  `text-red-100/200`, `text-sky-200`) had no light-theme override, so text like
  the *Confirm the bridge's timezone* banner was almost invisible on a light
  background. Each now maps to a dark shade of the same hue in light mode.

## [0.2.1] - 2026-10-07

The first tagged release. Everything below this heading has shipped:
add-on 0.2.0 carried all of it except the Sequencer, logging, cache and
ingress-name fixes, which are new in 0.2.1.

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

- **Under HA ingress the user badge showed "(" and "(ingress)".** It now shows
  the signed-in Home Assistant user's name, from the Supervisor's
  `X-Remote-User-Display-Name` / `-Name` headers, falling back to *Home
  Assistant*. The internal user id stays `ingress`, so the legal notice
  acknowledgement and the audit trail are unaffected.
- **A browser could keep running the previous version's scripts after an update.**
  The HTML pages had no cache header, so Safari reused an old copy, loading
  the old `app.js` under the new templates. Settings went blank and gateway
  names disappeared after the 0.2.0 update. `/`, `/user` and `/login` are
  now sent with `Cache-Control: no-cache`.
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
