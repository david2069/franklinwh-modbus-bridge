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

## [0.2.13] — 2026-10-17

S1 hotfix: connecting to the aGate wrote its operating mode and reserves.

### Fixed

- **Connecting no longer writes to the aGate (#239, S1).** franklinwh-modbus
  0.9.5 runs a "writability test" inside `connect()`: it writes test values to
  the operating mode (15507: Emergency Backup when the aGate is in
  Self-Consumption) and both reserves (15508/15509), then writes the originals
  back. The bridge connects at every start and reconnect, so the mode and
  reserves were rewritten each time, and left wrong if the restore failed.
  The bridge now turns that test off on the controller it creates.

## [0.2.12] — 2026-10-17

A battery-direction fix, plus the SunSpec Explorer export.

### Fixed

- **Force command direction after a reconnect (#230, S2).** The bridge gave
  franklinwh-modbus its trusted battery rating before taking the Modbus lock.
  A poller reconnect in that gap reset the library's rating to the nameplate
  (or 5000 W), and franklinwh-modbus 0.9.5 clamps an over-limit request in
  the opposite direction (library #28), so a Force Charge could be sent as a
  full-rate discharge when the trusted rating was higher than the nameplate.
  The rating is now set under the lock, immediately before the send.

### Added

- **SunSpec Explorer export (#228).** **Export…** opens a window: this model,
  chosen models or all of them, as JSON or CSV, with a preview. **Redact serial
  number** is on by default; it replaces the serial in model 1's `SN` and
  anywhere else it appears. The gateway's IP address and name are never
  included. Built by the new `GET /api/models/export`. It replaces the
  per-model SunSpec JSON/CSV buttons, which exported the serial unredacted.
- **Save as Gist (#228)** on the export and on Settings → Admin → Support
  info: copies the text and opens GitHub's new Gist page, where you choose a
  secret (unlisted) or public Gist. The bridge stores no GitHub token and
  sends nothing to GitHub.
- **Extension registers: All / Identified / Unknown filter (#228)**, to show
  only the named registers or only the unnamed ones.

## [0.2.11] — 2026-10-17

S1 hotfix: MQTT battery commands while the Control group is off.

### Fixed

- **MQTT commands while the Control group is off (#204, S1).** Switching
  Control off removed the control entities from Home Assistant, but the bridge
  stayed subscribed to `<prefix>/<id>/control/+/set` and obeyed any message
  there, so anything that could publish to the broker could still command the
  battery. It now unsubscribes while Control is off, refuses a command whose
  group is off, and subscribes again when Control is switched back on.
- **Publishing groups and a second gateway (#204).** A registered gateway kept
  the group settings it had when it registered, so switching a group off left
  that gateway's entities in Home Assistant. Every gateway now follows the
  groups.

## [0.2.10] — 2026-10-17

Packaging: the Docker install (milestone R1b). Found by an end-to-end test of
every install method on 0.2.9.

### Fixed

- **`.env.example` pointed MQTT at `localhost`**, so a compose install never
  connected to its broker (#175).
- **Compose preset the timezone to Australia/Sydney.** `TZ` is now required:
  compose refuses to start until `.env` sets it (#176). A stopgap until each
  gateway has its own timezone (#162).
- **Compose seeded admin/admin.** The first admin now gets a generated password,
  printed once in the log. An empty `ADMIN_PASSWORD` is treated as unset instead
  of creating an admin with no password (#177).
- **The image trusted `X-Forwarded-*` from any client.** The Dockerfile runs
  uvicorn with `--no-proxy-headers`, as the add-on has since 0.2.7 (#178).
- **Podman wasn't recognised as a container** (`/run/.containerenv`), so a plain
  `podman run` kept its database outside `/data` and admin recovery refused
  (#179).
- **Compose started a full Home Assistant.** It now starts only with
  `--profile ha` (#180).
- **`LOG_LEVEL` and the add-on's `log_level` option were ignored** (#181). Env
  vars now take precedence over `config.yaml` for every setting, as documented.

### Documentation

- Install guides rewritten after the end-to-end test (#199): a Python page,
  Podman notes, check-it-works and uninstall steps.
- Full disclaimer on the docs home page (#203).
- Developer Guide, Home Assistant setup guide, and battery dispatch best
  practice (#215).

## [0.2.9] — 2026-10-16

S1 hotfix: scheduled dispatch (#164). Same day as 0.2.8, as an S1 hotfix.

### Fixed

- **A refused force command was audited "ok — dispatched"** (0.2.8 regression).
  When a force command isn't sent (battery rating not trusted, unknown action,
  error), the command handler now records a failed result with the reason, so a
  schedule audits **failed**, notifies, and retries at 15/30/60 s. Before, the
  previous command's "ok" was read.
- **Manual values leaked into scheduled runs.** A force entry now always sets
  its own power, duration and target SoC, using 0 when left empty: full trusted
  rating, no time limit, no target. Before, an entry with empty Power ran at the
  last manually set watts, and a leftover manual time limit or target could end
  it early.

### Documentation

- New user documentation site (MkDocs Material, #173): install, setup wizard,
  every dashboard and setting, the full scheduler reference, and the utility,
  tariff and location guides.

## [0.2.8] — 2026-10-16

S1 hotfix: battery control (#133, #134). Same day as 0.2.7, as an S1 hotfix.

### Fixed

- **Force charge/discharge could run far above the requested power on sites
  with several aPowers** (#133). The aGate is commanded with a percentage
  (`WSetPct`) of the battery's rating. That rating came from Model 702, or was
  assumed to be 5000 W when unread (#134). A 5 kW request on a 20 kW site went
  out as 100 %. Force commands are now sized only against a **trusted** rating
  and are refused until there is one:
  - **calibrated:** the peak recorded while the owner runs a TOU battery
    dispatch in the official app;
  - **Model 702 confirmed:** the battery has been seen at 80–115 % of it. On
    first start this is seeded from recorded history, so a site that already
    discharges at full power is confirmed at once.

  If the battery is later seen well above the rating, or a running command
  overshoots it, the command is released and the rating must be recalibrated
  (`gateway/battery_rating.py`, migration 60).
- **Power sliders** were capped at a hard-coded 5000 W. They and the Max
  buttons now follow the trusted rating, and the limits endpoints return
  `null` instead of an assumed 5000 W.
- **Clean-up removes a stale entity's retained state topic** together with its
  discovery config.

### Removed

- **Command Power %** (UI, HA entity, REST, schedule form and
  `params.power_pct`). It was a percentage of an assumed rating, not of the
  battery. Existing % schedule entries still run, sized against the trusted
  rating, until edited. The HA entity is removed by the start-up clean-up.

### Added

- **Battery power rating** in Settings → gateway edit: status per direction, and
  **Calibrate** (start, then run the dispatch in the app, then Finish). API:
  `GET /api/gateways/{id}/battery-rating`,
  `POST /api/gateways/{id}/battery-rating/calibration`.

### Changed

- The add-on's Home Assistant sidebar entry reads **Modbus Bridge** (#144).

## [0.2.7] — 2026-10-16

Security hotfix. Nothing else is in this release.

### Security

- **Add-on: only Home Assistant ingress is trusted without a login**
  (GHSA-hhm2-xvvc-f6ww). In add-on mode every request to port 8099 was
  treated as the ingress admin, so any other add-on on the Supervisor network,
  and anyone once port 8099 was published, had the full admin API with no
  credentials, battery control included. A request now counts as ingress only
  when it comes from the Supervisor's ingress proxy (`172.30.32.2`) with the
  `X-Ingress-Path` header; everything else logs in with a bridge account.
  `addon/run.sh` no longer trusts forwarded headers (`--no-proxy-headers`), so
  no client can rewrite its address, and sets `ALLOW_INSECURE_AUTH=1` so a
  published port 8099 can log in over plain HTTP, as in Docker
  (`api/auth.py`, `tests/unit/test_addon_ingress_trust.py`).

## [0.2.6] — 2026-10-16

### Added

- **Old leftovers are removed automatically.** About 90 s after start the
  bridge removes its own stale discovery configs from the broker (ours by
  `sw_version`, including the v0.1.0 form, for our gateways, and no longer
  published). It also removes empty old devices from every connected Home
  Assistant: devices with exactly a pre-0.2.4 identifier of ours and **no**
  entities. Another integration's configs and a merged device still holding its
  entities are never touched (`publish/cleanup.py`).

### Fixed

- **Power History dropped every sample above 15 kW** (#122). A second 15 kW
  limit in the history recorder (`store/metrics.py`) rejected the whole row
  whenever solar, grid or battery power exceeded 15 kW. 0.2.5 had removed only
  the poller's copy.
  - **Root cause of both limits:** commit 2caf8a7 (2026-05-28) fixed real 0xFFFF
    corruption (three summed "no value" registers reading as 196,605 W), but
    also capped all power at 15 kW. It assumed nothing residential exceeds that
    ("3× aGate stacking"), which conflates the battery system with the
    customer's own PV array, and was never checked against a real site. It was
    duplicated in two places. Effect on a 33 kW (DC) site: wrong (stale) solar
    in Home Assistant, and gaps in Power History at high output.
  - Both guards now reject only physically absurd values (> 150 kW, the summed
    sentinel), not real readings.
- **Unpublish undid itself within about a second** (#131). The run loop
  republished discovery straight away. Unpublish now pauses publishing until
  **Republish** (persisted across restarts), and removes every gateway's
  entities, not only the primary's. Settings shows "Unpublished — Republish to
  restore".

## [0.2.5] — 2026-10-16

### Fixed

- **Several gateways: every gateway gets its own Home Assistant device** (#128).
  Extra gateways were registered with MQTT once at startup, before a real aGate
  had been read, and never again. A user with three gateways saw one device,
  and the other gateways' readings were dropped. The bridge now re-checks every
  15 s and registers each gateway as soon as its serial is known, and re-binds
  it after a reconnect or restart.
- **Device names use the short serial for every gateway:**
  `FranklinWH Modbus <last 8 of serial>`. Extra gateways were named by their
  gateway id (e.g. `…shed_gw`). A demo/mock gateway is named
  `FranklinWH Modbus <id> (mock)`.
- **Large solar arrays: real readings were thrown away above 15 kW** (#122). A
  33 kW (DC) site had its midday solar replaced by a stale value every poll.
  The fixed 15 kW limit is gone; only physically absurd values (e.g. three
  summed 0xFFFF registers) are rejected.
- **No more night-time "Rejected corrupted pv_total=65531" warnings** (#122).
  Small negative PV readings (an inverter's standby draw, sent as unsigned
  16-bit, e.g. 65531 = −5 W) are decoded and shown as 0 W.

## [0.2.4] — 2026-10-15

### Changed

- **One Home Assistant device per integration** (owner decision; docs/ha-device-identity.md).
  The bridge's device is now named `FranklinWH Modbus <short serial>` with
  identifier `<prefix>_modbus_<SERIAL>`, so Home Assistant never merges it with
  FWHAI's or the Local Bridge's device for the same aGate (#120). Entities
  keep their unique_ids and move to the new device; delete the empty old one.

### Added

- **Support info for a GitHub issue.** A redacted summary of this install to
  paste into a bug report, aligned with the Local Bridge's support bundle.
  - `GET /api/support/info` (admin only) returns the bundle as JSON (`info`)
    and as ready-to-paste Markdown (`markdown`): bridge version, git commit
    when known, environment, Python/OS/arch, library versions, DB schema,
    install date, uptime, UTC offset and whether the timezone was confirmed;
    per gateway (as "gateway 1", "gateway 2", …) model, firmware, device type,
    wiring, battery ports, home-load register, poll interval, health, last
    poll age, poller counts and a 24 h link-stats summary; MQTT (enabled,
    connected, where the broker settings come from, namespace, entity count);
    HA instance counts; modules, schedules, point history, solar forecast
    (configured + array, never the location), NEM region, dynamic tariff,
    users by role; and the last 24 h of warnings/errors by source with the
    last 20 messages.
  - Never included: serials (full or short id), IPs, host names and ports of
    the aGate or broker, MAC addresses, location, usernames, emails, HA URLs
    and tokens, passwords, MQTT credentials and client id, gateway/site/device
    names, session ids. Structured fields come from an allowlist; log and
    error text goes through a new scrubber (`support/scrub.py`) that replaces
    every identifying value on file, then anything that looks like an IP, URL,
    host, MAC, email, serial, token, coordinate or home-directory path.
  - The install date is recorded once on first run (`app_config.install_date`);
    an older install gets the date its database was created.
  - Settings → Admin → **Support info for a GitHub issue**: Generate → preview
    → Copy (clipboard, else select + copy, else "Press Ctrl/Cmd+C") /
    Download .md / Open a new issue on GitHub. A "Support info" link next to
    "Issues" in the sidebar footer leads there (admins).
  - GitHub issue forms: `bug_report.yml` (with a Support info box and
    personal-details / not-FranklinWH-support checks), `feature_request.yml`,
    and `config.yml` with a "Not FranklinWH support" contact link.

- **Choose and change the MQTT namespace** (#87, part B). The namespace
  (`mqtt_config.topic_prefix`) prefixes every topic, unique_id and HA device
  identifier. Its own namespace keeps this bridge apart from FWHAI and the
  Local Bridge on a shared broker (#120).
  - New installs start on **`fwh_mb`**. "New" means no gateway with an
    address, serial, past connection or mock, and nothing ever published.
    Existing installs keep `franklinwh`. The decision is made once and stored
    in `app_config.mqtt_namespace_chosen`.
  - Allowed: `fwh_mb` (recommended), `franklinwh` (legacy), or a custom
    `^fwh_[a-z0-9]{2,8}$`. `fwhai`, `fwh_ai` and `fwh_lb` are reserved for the
    other producers. The policy is one constant: `publish/namespace.py`
    `NAMESPACE_POLICY`.
  - `GET /api/mqtt/namespace` returns the current namespace and the choices.
    `POST /api/mqtt/namespace` `{topic_prefix, confirm}` (admin) changes it.
    Before anything has been published it just switches. After, `confirm` is
    required, and the change:
    1. reads the old namespace's retained discovery configs;
    2. empties only those whose device `sw_version` starts with
       "Modbus Bridge" (another producer's are left, as are the old
       availability and state topics);
    3. switches topics, unique_ids, device identifiers and the command
       subscription live, and republishes.

    It reports `removed_ours` / `left_foreign` / `not_found`, and logs a
    WARNING and an audit event. If the broker can't be reached, nothing
    changes.
  - Settings → Home Assistant → MQTT Broker: **Change namespace…** opens a
    dialog with the choices, the consequences and the counts afterwards.
  - The setup wizard's Home Assistant step shows the namespace, and lets you
    choose it while nothing has been published.
  - `PATCH /api/mqtt/config` still refuses a prefix change (409), and now
    points to the new endpoint. The discovery prefix stays fixed.

- **`GET /api/energy/battery-hours`** (#116): per-hour battery charge and
  discharge kWh for a local day (default today), integrated from the sampled
  `battery_w` and split by sign, so an hour that charged and then discharged
  reports both. Each hour has a `state` (`charge` / `discharge` / `idle`, idle
  under 50 Wh) or `null` when it has no samples (the rest of today, an outage).
  An estimate from sampled power, not a meter. Read-only.

- **Dynamic (live) tariffs in billing** (#89, part B).
  - A service's plan can be Static or **Dynamic (live)** (Settings → Energy &
    Automation → service → Energy rates). Dynamic prices energy at the AEMO
    NEM spot for a chosen region (sell = buy = spot) or at Home Assistant
    buy / feed-in price entities (feed-in falls back to buy). The editor shows
    the live price. Stored in the service's `pricing` JSON as
    `kind: "dynamic"` + `dynamic: {source: "nem", region} | {source: "ha",
    buy_entity, feedin_entity}`, the Local Bridge's shape; the static rates
    stay on file.
  - Accrual: each poll's grid import / export counter delta is priced at the
    live rate. No live price (or a NEM price over 10 min old) → that energy is
    recorded as unpriced kWh, never priced at an old or zero rate. A slice
    over 1 h is not priced. Demand, export bonus, export charge and standing
    charges still apply on top.
  - `tariff.buy_rate` / `tariff.sell_rate` show the live $/kWh, and
    `tariff.spot_price` / `tariff.feed_in_price` follow the plan's source,
    while the plan is dynamic. Static plans are unchanged.
  - Billing history (migration 59) records how each period was priced
    (`static`, `dynamic` or `mixed`, with a label such as "Wholesale (live) ·
    AEMO NEM NSW1") and its unpriced kWh; the Energy Costs tab shows both.
  - `GET /api/tariff/dynamic` (the active plan's live price, or a preview for
    the editor); `POST /api/tariff/validate-rates` validates a dynamic
    provider. Writes stay admin-only.
- **Wholesale spot price from AEMO NEM, as automation sensors** (#89, part A).
  - Settings → Gateways → "Wholesale spot price": pick a NEM region (NSW1,
    QLD1, SA1, TAS1, VIC1; Off by default, and Off fetches nothing). It shows
    the live 5-minute spot price in c/kWh, its settlement time in the bridge's
    zone, and a stale marker when AEMO stopped answering. Australia only.
  - New condition sensors `tariff.spot_price` (buy) and `tariff.feed_in_price`
    (sell) in c/kWh, the Local Bridge's names and units. NEM gives sell = buy =
    spot. An optional Home Assistant price entity per side overrides NEM ("HA
    wins"); a `$`/AUD value is scaled to cents. No data, or a price over 30 min
    old, reads None, so conditions fail closed.
  - `GET /api/tariff/spot?region=` (the Local Bridge's route) and `GET/PUT
    /api/tariff/spot/settings` (admin writes). Source: AEMO's public
    `ELEC_NEM_SUMMARY`, no key; cached 240 s, 12 s timeout, never raises.
  - `tariff.buy_rate` / `tariff.sell_rate` are unchanged ($/kWh from the static
    rate model). A dynamic tariff kind in billing is part B.

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

- **SunSpec Explorer names the extension registers we've identified.** They
  are labelled **unconfirmed**, from one site's recorded readings and the
  library notes, not FranklinWH documentation: 15013 (battery power at ~1 W,
  sign while charging not established), 15016 (status code), 15035 (SoC ×10),
  16001/16002 (reserve mirrors). Values stay raw.

- **Solar Forecast card aligned with the Local Bridge's** (#116). Tinted day
  panels (amber border today, blue tomorrow) headed "THU, 15 OCT", a big kWh
  figure and "Peak X kW @ HH:MM"; actual-so-far kWh and % with a progress bar
  and "vs forecast to now"; where today's solar went ("→home →batt →grid kWh
  est.", the Energy Flow card's reconstructed arcs) and the battery SoC; a
  battery charging / discharging / idle band under the hourly bars with a
  legend; gradient bars with the current hour emphasised and a NOW line; and a
  footer saying what the bars and times mean. Times stay in the bridge's zone,
  labelled, with 24-hour ticks.

- **An open-ended dispatch's log line names the aGate revert timer** (#72):
  `[no time limit; aGate revert timer 7200s, not enforced by the aGate (PICS
  Issue 4)]`. It used to say only `[no time limit]`.
- **Usernames are case-insensitive** (#50). A name differing from an existing
  one only in case is refused.
- **The live bridge runs the code baked into its image** (#48). No `./src` mount
  and no `uvicorn --reload`, so saving a file no longer restarts it.
  `docker-compose.dev.yml` restores hot-reload for development.

### Fixed

- **Each install has its own MQTT client id** (#118). Every install connected
  as `franklinwh_bridge`. A broker keeps one connection per client id, so two
  bridges on one broker would knock each other off in a loop. At first start
  the shared default is replaced with `franklinwh_bridge_<8 hex>`, saved and
  logged. A value someone set is kept. Settings → MQTT Broker shows it. If
  the connection drops 3 times in 2 minutes, a WARNING names the client id.
  Whether the reconnect loop seen on the add-on was this collision is not
  established.

- **Solar Forecast card unreadable in Light mode** (#116). The card hard-coded
  dark slates; the Light theme overrides some of them but not the panels'
  `bg-slate-800/60` or the weather chip's `bg-slate-700/60`, so the day panels
  rendered as dark grey boxes with unreadable text. It now uses the theme
  tokens (new `.sf-*` rules in `design-system.css`) and reads in both Light
  and Dark.

- **The HA dashboard card lists only the entities this bridge publishes.**
  With "Include diagnostics" on, it also listed every other entity whose
  unique_id started with our prefix. On a Home Assistant that also runs FWHAI,
  whose defaults use the same device identifier and unique_id prefix (so HA
  merges both onto one device), that gave 155 entities, most of them FWHAI's.
  The card now takes exactly what this bridge currently publishes over MQTT and
  looks those ids up in HA's registry. The modal says "N of M published
  entities found". Entities whose unique_id both bridges use can't be told
  apart until each bridge has its own prefix (#87).

- **No real location as an example.** The Location & Solar latitude and
  longitude fields showed a real place as their placeholder; they now say
  "latitude" / "longitude". The tests use an obviously made-up point.

- **Spot price interval, and HA conditions you can tell apart** (#110).
  - Wholesale spot price: "settled 18:25" at 18:22 read as a future time.
    AEMO's settlement time is the end of the 5-minute interval, so it now
    reads "for 18:20–18:25 AEDT".
  - Schedule editor: a long sensor name keeps its end in the condition picker
    ("… Express - Home General Price" / "… Home Feed In Price" instead of two
    identical "HA Live: Amber Express - H…"). The full name and id show on hover.
  - The "live:" value is rounded and carries the sensor's unit
    ("0.1339 $/kWh", not "0.1339303"). Raw HA prices are $/kWh while
    `tariff.*` prices are c/kWh.

- **A broker saved in Settings is now used, and Save & Reconnect applies it**
  (#103). At startup the environment's MQTT host always replaced the saved one.
  Even an unset `MQTT_HOST` did, through its `localhost` default. So a broker
  saved in Settings was never used, not even after a restart, and Reconnect
  restarted against the old broker.
  - One rule per field (host, port, username, password): an `MQTT_*` variable
    that is set and not empty wins; then the value saved in Settings; then, as
    an add-on, the Supervisor's broker; then `localhost:1883`. It lives in
    `config/mqtt_broker.py` and is used at startup and on
    `POST /api/mqtt/reconnect`.
  - `GET /api/mqtt/config` adds `sources` and `env_vars`. The edit form starts
    from the broker in use, and a field the environment sets is read-only,
    with the variable named. `PATCH` refuses to change one (409).
  - No live change: docker-compose sets `MQTT_HOST`, which still wins.

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
