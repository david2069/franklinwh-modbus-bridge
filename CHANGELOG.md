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

### Fixed

- **Discovery now goes out on every MQTT connection** (#412), as the bridge's own discovery
  already did. A gateway added after start had its discovery sent just before the reconnect
  that sets the last will; some brokers drop that (seen on amqtt, not on Mosquitto), and it
  wasn't sent again.

## [0.2.49] — 2026-10-31

### Added

- **Status notices in Home Assistant (owner, 2026-10-30; modelled on the Energipays
  bridge):** a persistent notification when the bridge starts (version, gateways polling
  and how often, any not polling), when it shuts down, and when a gateway is paused
  (Stop) or polls again (Start, or Restart of a paused one). One attempt each, logged
  either way. On by default; **Settings → Home Assistant → Status notices** switches
  them off (`status_notices` in `GET/PUT /api/alerts/settings`, whose fields are now
  each optional).

### Changed

- **Logs → Battery-control writes has a Gateway column** (owner, 2026-10-30): which aGate
  each write went to, by gateway name with its aGate serial below (the ID on hover). The
  downloads already carried `gateway_id` and still carry no serial.
- **`/api/mqtt/topics` names its gateway** (`?gateway=`; `400` without one), like the
  other routes (#296 item 43). Its flat `topics` are that gateway's, empty if it isn't
  publishing; it used to fall back to the first gateway.
- **`/api/status` no longer describes only the gateway with ID `default`:** its poller
  component lists every gateway by ID, and `?gateway=` adds one as the flat fields. It
  still answers with no gateway named, since it is the add-on's health check.
- **Settings → Site & Gateways:** a gateway the bridge has refused (a different aGate
  answered, or its serial is another gateway's) shows a red **serial refused** label in
  the table, with the reason on hover. It used to show only in the pop-up.
- The REST API reference (`docs/dev/api-reference.md`, `openapi.json`) is regenerated
  for this version.
- The docs' top-bar gateway selector screenshot shows the current selector (All gateways…, each gateway with its state).

### Fixed

- **Removing a gateway left its device and entities in Home Assistant until the bridge
  restarted** (#151): the delete never synced MQTT, unlike add and edit. It now removes
  them at once, as switching Publish off does (config tombstoned, availability offline;
  retained state stays on the broker). Found in 0.2.49 release testing, where the
  Settings page then asked for the deleted gateway's topics and got 404.
- **A page left on a removed gateway kept asking for it** (404 every 10 s) when no other
  gateway was polling, or when the user had picked it. The page now moves to the default
  gateway as soon as the one on view is gone; removing it in Settings does so at once.
- **AEMO price fetch failures logged no reason** when the error had no text (a
  timeout): the log now names the error.
- **Log export over-masked the demo gateway:** its `mock` host and `MOCK-DEMO` serial
  were masked, so every "mock" and "MOCK" in the export became a placeholder. A demo
  gateway identifies no one and is no longer masked; real serials and addresses still are.

## [0.2.48] — 2026-10-30

### Added

- **A designated default gateway (#296 item 43):** **Make default** in Edit Gateway marks
  one gateway the default (migration 63, `POST /api/gateways/{id}/default`, logged as an
  admin action). It's listed first, marked **default** in the gateway table, and the page
  opens on it; the Sequencer starts on it. A designation only: the gateway with ID
  `default` keeps its Home Assistant device and topics. Removing the default gateway makes
  the next one the default.

### Changed

- **Every API request names its gateway (#296 item 43):** routes that used the gateway
  with ID `default` when none was named now answer `400` with *"This request names no
  gateway: add …"*, logged with the user. They take the gateway as `?gateway=`,
  `?gateway_id=` or a `"gateway_id"` body field, by ID or serial (an unknown one `404`, a
  malformed one `422`). This covers `/api/points`, `/api/points/{id}`, `/api/command`,
  `/api/battery/limits`, `/api/sensors`, `/api/scheduler/evaluate`, the energy, tariff,
  solar and point-history routes, the gateway address and test, the SunSpec model,
  catalog and export routes, `/api/sequence/execute` and `/api/reader/run`. The web pages
  send the active gateway. The user page uses the default gateway, or a polling one when
  the default isn't polling, as the main page does; it used to read and command
  `default` only. **REST clients that call these routes without a gateway must add
  one.**
- **Logs has two sub-tabs**, **App log** and **Battery-control writes**, each with its
  own link (`?tab=logs&section=app|writes`). The writes table used to sit below the log.
  Writes stay admin only (owner).

### Fixed

- **Removing the gateway with ID `default`** stopped it and cleared its samples before
  answering 403; it's now refused first, with the reason (it owns the primary Home
  Assistant device; removing it waits for a later release).
- **The Sequencer listed the default gateway twice** ("Primary aGate" and its name).

## [0.2.47] — 2026-10-30

### Fixed

- **Schedule conditions: the column grip can be seen and caught (PR #400, owner 2026-10-30):** the
  grip that widens the sensor column was a faint 2px line with a 10px hit area. It's now a
  bordered **⋮** handle with a 24px hit area beside the field, highlighted on hover and
  while dragging.
- **Schedule conditions: the sensor drop list can be resized (PR #400):** a **◢** corner drags it
  wider and taller, never below its default size or past the dialog; the size is
  remembered in this browser. Applies to the sensor and Lookup lists in entry and exit
  conditions and in groups.

### Changed

- **Mode and reserve writes (15507–15509) are documented as a known defect with no
  known workaround (PR #398):** the "Hardware ignored write" message, the sample sequences'
  step notes, the scheduling test plan and design, `vendor-issues.md` and the
  write-plane handoff no longer present a "SPAN unlock" as a fix. Tests that need
  these writes to take effect are marked known to fail. Earlier changelog entries are
  left as written.
- **`franklinwh-modbus` capped below 0.11 (`>=0.10.1,<0.11`) in `pyproject.toml` and
  `requirements.txt` (PR #399):** 0.11.0 changes how Power % and control modes are sent, and the
  bridge hasn't been tested against it. A pip install no longer pulls it; the Docker
  images already pin v0.10.1.

## [0.2.46] — 2026-10-29

### Added

- **A gateway is locked to its aGate's serial (PR #394, #296 item 42):** the first serial
  reported is kept; a different serial at the address (read twice) or one another
  gateway holds is refused: no polling, no commands, no new connection, an ERROR in
  the log, and `serial_refused` in the gateway list. Start-up stops before it reads
  control state or sends a Release to that aGate.
- **Gateway ID or serial in `/api/gateways/{gw_id}/…` (PR #394, #296 item 42):** a serial is
  rewritten to its gateway's ID before routing. After sign-in, an unknown reference
  gets 404 "No gateway has the ID or serial '…'" and a malformed one 422 with the
  reason; each is logged with the user. A viewer's write still gets 403 first.

### Changed

- **"Site (All Gateways)" replaced by a read-only All gateways overview (PR #395, #296 item 43):**
  the gateway picker's **All gateways…** opens a modal: counts (Registered, Polling,
  Unavailable, Stopped), one row per gateway (status, grid, mode and command, SoC, battery,
  grid, solar and load power, last poll) and a site total; clicking a row makes that gateway
  active. The Site view sent commands and Release to `default`; the page now always names
  the active gateway (`api/gateways/{id}/points` and `/command`, never `api/points` or
  `api/command`). New schedule entries target the active gateway.

### Fixed

- **A gateway not set up reads Connected again (PR #396):** after #395 a fresh install
  showed red Disconnected beside "No gateway yet". The setup wizard and docs no longer
  name the removed Site view.

## [0.2.45] — 2026-10-29

### Added

- **Stop/Start polling in the Edit Gateway dialog (PR #390):** the result shows
  in the dialog and stays there.

### Changed

- **Run now is followed like a scheduled run (PR #392):** the Monitor shows
  "Run now — running …"; the entry's exit conditions end it and the battery is
  checked to be moving; ■ Stop now shows outside the window too and releases it;
  if the entry's window opens while it runs, the window takes it over. However
  it ends is logged once: Stop, a Release, its time limit or another command
  replacing it. With no Duration it still runs until released or the aGate's
  own timeout.
- **Scheduler releases name the entry (PR #392):** window end, Stop and exit
  conditions send the Release as `schedule:<id>`, not `manual`.

### Fixed

- **Release from the top bar shows in the entry's Activity Log (PR #392):** a
  `released` row, "released by <user> — <result>", when the Release ends a
  schedule's command.
- **A Run now the aGate rejects is logged `failed` (PR #392)**, not `executed`.
- **A stopped gateway's points aren't requested (PR #391):** the page and the
  MQTT settings skip gateways that aren't polling, so no 404s.

## [0.2.44] — 2026-10-28

### Added

- **Every top-bar item can be hidden (PR #383):** Topbar Preferences gains a
  Left section (sidebar button, page title, gateway picker) and Right now lists
  Connection, Data quality, Clock and User with Release, SunSpec, Theme and
  Refresh. Only the gear stays. Right items now really reorder (the arrows had
  no effect). Saved preferences keep their choices.
- **Top-bar icons with live values (PR #384):** Grid, Solar, Load and Battery
  show an icon (drawn for this project) and the value (kW, or SoC %), with hover
  text saying what each is. New store getter `batteryLevel`.
- **Condition sensor column can be dragged wider (PR #386):** one shared width
  for every condition sensor field; only wider than the 13rem default, only as
  far as the row has room; remembered per browser. The field shows the full
  name, cutting the start when it doesn't fit.
- **Colour rule documented:** `docs/user/ui/colours.md`.

### Changed

- **One clock, bridge time only (PR #383):** the "your time …" line is gone from
  the top bar, hover titles, chart tooltips and the `/user` footer.
- **Colour rule (PRs #384, #385):** red = critical only, amber = warning,
  orange = energy drawn, green = active, grey = inactive.
  - Battery SoC: red at or below 5%, amber at or below the active reserve,
    otherwise green (top bar, Dashboard ring, `/user` ring; was 80/30/15).
  - Grid importing: orange (was red). Battery discharging: orange (was amber).
    Solar producing: green (was the solar amber).
  - Grid not connected and an active command (Release): amber (were red).
    Disconnected stays red.
- **Friendly names (PR #386):** HA Action entities are listed by friendly name
  (id in the hover; same-name entities keep their ids). Explain, the entry's
  condition summary, Monitor lines and the Activity Log show names instead of
  ids; stored text keeps the ids.
- **Docs:** the top-bar page brought up to date; three screenshots redone.

### Fixed

- **CHANGELOG 0.2.43 (PR #387):** the Power % line cites PR #366, documented in
  PR #364 (#364 is a PR, not an issue).

## [0.2.43] — 2026-10-28

### Added

- **Scheduler Monitor (PR #381):** after each scheduler tick, one line per
  enabled entry goes to an in-memory buffer (last 500, lost on restart): idle,
  blocked (failing condition with its live value), holding (seconds left),
  ready, running (exit conditions not yet met), skipped (which entry holds the
  gateway), deferred, retrying or done. Route: `GET /api/scheduler/monitor`
  (`entry_id`, `since`). The Schedule tab has a collapsed **Monitor** panel,
  polled every 5 s while open, with an entry filter, Pause, Clear and Copy. The
  Activity Log is unchanged.
- **Explain an entry (PR #379):** **⋯ → Explain / details** describes an entry in
  plain words at Min, Medium or Full (WHEN / IF / DO / UNTIL / ON / HOLD), with
  the same wording as the Direct Connect bridge. The level is remembered per
  browser.

### Changed

- **Dashboard dropdowns open upwards (PR #377)** when there's no room below, and
  are capped to the space there is, so iPad menus such as Events History show
  every option.
- **Schedule dialogs size to Default, Medium or Full (PR #378)** and drag to any
  size in between; the choice is remembered per browser. The notification
  message box grows as you type. The Sequencer header is on two lines, so long
  names wrap.
- **Schedule follow-ups (PR #380):** the notification message box is no longer
  2 px tall after switching an HA action to Notify; dialog heights use the
  visible screen height (`dvh`), so Full fits on iPhone and iPad Safari; Explain
  reads "when it starts and when it ends".

## [0.2.42] — 2026-10-27

### Added

- **Sequencer import (#294, PR #374):** an **Import** dialog takes a sequence
  from the GitHub samples of the latest `vX.Y.Z` tag (each marked new, changed
  or same as yours), an https link (10 s, 256 KB cap; GitHub page links become
  raw links) or pasted text. It previews steps and register writes, then saves
  under a chosen name, asking before replacing. Nothing runs on import. Each save
  is logged. Routes: `GET /api/sequence-import/samples`,
  `POST /api/sequence-import/preview`, `POST /api/sequence-import/save`
  (control capability). The existing sequence routes now check names (letters,
  digits, `_`, `-`, up to 64) on get, save, delete and rename.
- **Admin actions (PR #375):** a poll interval change applies live, without a
  gateway restart; below 5 s, Settings asks first. Gateway stop and restart ask
  first, naming a running schedule entry or battery command
  (`GET /api/lifecycle/check`). Poll interval changes, gateway start, stop and
  restart, and bridge restarts are each a WARNING app-log line and an
  `ADMIN_ACTION` alert naming the user. **Restart bridge** (Settings → Admin,
  `POST /api/bridge/restart`, admin) works on Docker only; it is disabled on
  the add-on, where HA restarts it.
- **"This bridge's entities in Home Assistant" (#368, PR #370):** a table on the
  Home Assistant page listing every published entity with its HA entity id,
  where HA shows it, and a note for other ids, entities missing from HA or
  disabled in HA. Route: `GET /api/ha/bridge-entities`.
- **Publishing groups ask and log (#296 item 39, PR #371):** switching a group
  asks first, naming the group and its entity count. Every change that adds or
  removes HA entities writes one app-log line naming the group, the change and
  the user.

### Changed

- **Grid connection and inverter state are normal sensors (#296 item 38,
  PR #371):** no longer diagnostic, so HA shows them with the other sensors and
  the dashboard card includes them. Unique ids and entity ids are unchanged.
- **Power % workaround logged on every use (PR #366; documented in PR #364):** one INFO line per
  command names the percentage, the watts sent through WSet and the approval.
  No behaviour change.
- **Small screens (PR #373):** the Explorer and Sequencer lists fold to a strip
  (on their own under 768 px once something is picked); Sequencer live-point
  tiles are two per row and cut long values off within the tile; the
  Preferences panel stays on screen under 640 px.
- **Add-on image workflow (#300, PR #362):** a prebuilt image is built for
  GHCR on `v*` tags. The add-on still builds locally until the image is public
  and named in `addon/config.yaml`.

## [0.2.41] — 2026-10-24

### Changed

- **franklinwh-modbus 0.10.1 (#323, #255):** the add-on moves from 0.9.5. Force
  charge and discharge are now sent in watts (WSetMod=1 + WSet in one write; the
  enable isn't cycled), so a 5000 W discharge runs on aGate firmware
  V12R02B60D00, which refuses WSetPct above 10 %. A value the aGate refuses comes
  back as "Rejected by the aGate (…)" at once, with no reconnect or resend. Also
  from 0.9.6/0.10.0: `connect()` no longer writes the mode and reserves, an
  over-limit request is clamped in the direction asked, and partial reads are
  reported. V12 still refuses charging (negative WSet).
- **Power % is a share of the trusted rating, sent in watts.** It used to go to
  the aGate as WSetPct unchanged, which the library no longer does. 50 % of
  5000 W is 2500 W. Like watts, a % now needs a trusted rating, and the
  overshoot guard covers it.

### Fixed

- **A scheduled dispatch the aGate refuses isn't retried all window (#323).** On
  2026-10-23 a refused 18:00 discharge was re-sent 58 times in an hour. A
  "Rejected by the aGate" result now stops that window's attempts, with one
  notification saying so; the next window tries again. Lost links and timeouts
  still retry with back-off.
- **"Dispatch this bridge did not send" names the real setpoint.** It read
  WSetPct only, so a watts dispatch showed "WSetPct=0.0%". It now reports WSetMod
  and WSet or WSetPct, and a change underneath our own command is judged in
  watts when the aGate holds watts.
- **WSet Mode read the SunSpec values the wrong way round.** It showed 0 as
  "Off" and 1 as "Pct"; SunSpec 704 defines 0 as W_MAX_PCT and 1 as WATTS. It
  now shows **Pct** or **Watts**, as reported.

## [0.2.40] — 2026-10-24

### Fixed

- **Log rows named the wrong gateway:** a row's gateway was taken from
  everything before the first colon after "Gateway ", so "Gateway demo started
  (MOCK): …" was filed under "demo started (MOCK)". It's now taken only from
  "Gateway <id>:", with the id a single word, and the mock start line names its
  gateway explicitly. Affects the Logs tab's gateway filter and the export.

## [0.2.39] — 2026-10-23

### Added

- **App log export (#320):** **Export text** and **Export JSON** on the Logs tab
  download the entries the current filters select (search, level, gateway,
  source, time span), oldest first, up to 100,000 lines, newest kept past that.
  Serials (and their 8-character short ids) and IP addresses are masked; loopback
  stays. Admin only. `GET /api/diagnostics/logs?format=text|json` with the same
  filters as `GET /api/logs`.

## [0.2.38] — 2026-10-23

### Fixed

- **Results inside dialogs and forms (#351, PR #355):** while a dialog or an
  inline add/edit form is open, the result of an action in it shows inside it
  (`role=status`) and stays until the next action; a toast only after it
  closes. Covers release control, chart and SunSpec export, the schedule
  editor and import, edit and remove gateway, the MQTT namespace, the setup
  wizard's timezone step, new sequence, and the user, gateway, HA instance,
  service and group forms. A failed timezone confirmation in the setup wizard
  no longer reports "Timezone confirmed: undefined" and moves on; a failed
  calibration and a failed new sequence now show their error.
- **Bridge heartbeat after a restart (#324, PR #354):** a beat counts only once
  it is sent, so the sensor no longer reads "unknown" for about 60 s after a
  start.

## [0.2.37] — 2026-10-22

### Added

- **Alert triggers (#324, PR #349):** COMMAND_REFUSED and WRITE_IGNORED are
  raised when a command fails; the message says whether the aGate or the
  bridge refused it. A new AlertMonitor (every 15 s) opens and resolves
  GATEWAY_UNREACHABLE (5/2 min), LIVE_DATA_MISSING (2/1 min) and
  DATA_MISSING_PARTIAL (15500-15509, 2/1 min). Conditions still open at a
  restart are reloaded so they can resolve.
- **Worker health (#324 §11, PR #349):** the pollers, the scheduler, the
  health checker, log writing, the monitor and delivery beat each pass.
  WORKER_STALLED is raised after max(4 × cadence, 30 s) without a beat, or
  when a task crashed. `GET /api/health/workers`. Detection only; no restart.
- **Critical alerts to Home Assistant (#324 §6, PR #350):** a persistent
  notification `fwh_mb_alert_<id>`, retried with back-off, dismissed when
  acknowledged in the bridge. Can be switched off (on by default); a skip is
  recorded with its reason. `GET/PUT /api/alerts/settings`.
- **Bridge device (#324 v1.2, PR #352):** "Bridge problem" (binary_sensor, no
  availability topic, open alerts as attributes) and "Bridge heartbeat"
  (every 60 s), both retained and diagnostic. They are removed by Unpublish
  and by a namespace change. Docs: `user/alerts.md`.

## [0.2.36] — 2026-10-21

### Added

- **Modbus write audit trail (#342 part 1):** every SunSpec write the bridge
  makes is recorded with time (ms), register, raw values, the aGate's reply
  (ok or the exception) and who asked (user, schedule, sequencer). One log
  line per write; kept 90 days / 200k rows; Logs → "Battery-control writes"
  with CSV/JSON download (works under HA ingress);
  `GET /api/diagnostics/modbus-writes` (admin).
- **Alert record and API (#324, shared spec with the DC bridge):** schema 61,
  `GET /api/alerts`, `POST /api/alerts/{id}/ack`, summary in `/api/status`.
  Nothing raises alerts yet.

### Fixed

- **15013 (BatteryPowerRaw)** is decoded as signed int16: it showed 65496 W for
  -40 W. Negative = charging, as M714 DCW (one site's readings).
- **15016 (StatusRaw)** description: 1 seen during two aGate service restarts
  on 2026-10-21 (V12; meaning unconfirmed).
- **Docs:** the HA setup page says up front that steps 1–2 don't apply to the
  HA app install.

## [0.2.35] — 2026-10-21

### Fixed

- **MQTT last will (#325):** a crashed or cut-off bridge left the retained
  availability "online", so HA showed frozen values as live. The broker now
  marks the gateway "offline". It uses the gateway's existing availability
  topic (D1), so it is set when exactly one gateway is announced.
- **Failed commands in plain words (#323):** the result names what the Modbus
  exception means and says the command did not run, or that the previous
  command stopped. A % command is logged as e.g. `50%`, not `0W`. Sent HA
  notifications are logged too.

### Added

- **Docs:** phone notifications (Companion app), the bridge on the home
  screen, remote access (Home Assistant Link recommended; Tailscale and
  Cloudflare Tunnel compared), off-premises backups, and "back up first" in
  the add-on install guide (#327).

## [0.2.34] — 2026-10-21

### Fixed

- **Live data silently missing after a network drop (#317):** a failed
  library-internal reconnect left the library without its device while its
  cached SunSpec models kept reading. Only 15500–15509 (solar, home, mode,
  reserves) stopped, with no warning, for 3.5 h on a live aGate. The poller now
  forces a full reconnect when the library's device is gone, or when the block
  is missing for 3 polls after it was seen, and logs a warning. Library
  defect: franklinwh-modbus#89.
- **Library warnings in the bridge logs (#318):** `franklinwh_modbus` WARNING
  and above now reach the add-on log and the Logs tab.

### Added

- **Guide in the sidebar**, and **Discussions** links in the sidebar footer, the
  phone menu and the docs (nav, footer, front page).

## [0.2.33] — 2026-10-21

### Fixed

- **Active reserve in Emergency Backup (#311):** the bridge showed the
  self-consumption reserve (15508). In EB it now shows 16001 as the aGate
  reports it; SC (15508) and TOU (15509) are unchanged and still
  cross-checked against 16001.
- **Events registers (#314):** the Dashboard's events lists showed
  40076/40078/41039/41044, which are neither Base-0 addresses nor 4xxxx
  references. They now show the Base-0 addresses (76, 78, 1039, 1044) and the
  column is labelled "REGISTER (Base-0)".

## [0.2.32] — 2026-10-21

### Fixed

- **aGate firmware V12R02B60D00 (#308):** the aGate now mirrors its SunSpec map
  at PDU 40000, and pysunspec2's scan probes 40000 first, ignoring the
  library's base 0. The bridge now probes base 0 first, so reads and writes
  stay at the aGate's own addresses and the Explorer/export addresses don't
  shift by 40000. Library defect: franklinwh-modbus#84.
- **Firmware after an aGate update (#307):** the nameplate is re-read after a
  reconnect, so status, export header and support bundle show the new
  firmware without a bridge restart; the change is logged.

## [0.2.31] — 2026-10-19

### Added

- **Power % restored (#305):** passed through as WSetPct (no rating
  conversion, the cause of #133); dashboard, schedules, HA entity.

### Fixed

- Sample sequences `basic_charge_release` and `conflict_test_160` used +WSetPct
  (discharge) for a charge.

## [0.2.30] — 2026-10-19

### Changed

- **Explorer sub-tabs** (Models / FranklinWH extensions / Modbus reader),
  `?section=` in the URL.

## [0.2.29] — 2026-10-19

### Fixed

- **Reader: no block while a battery command is active** (owner; not in the
  original reader).

## [0.2.28] — 2026-10-19

### Added

- **Modbus reader in the SunSpec Explorer (#294).** The franklinwh-modbus
  reader (vendored, MIT) via `POST /api/reader/run`, admin, read-only; the
  bridge hands it the aGate's connection under the Modbus lock and takes it
  back after.

### Fixed

- **Socket leak on disconnect/reconnect (#64).** The library's disconnect()
  never closed the TCP socket (pysunspec2's close() is a no-op for TCP
  devices); `close_sockets_on_disconnect()` closes it.

## [0.2.27] — 2026-10-19

### Changed

- **Docs links go to the github.io site** (sidebar Docs per tab,
  `disclaimer.DOCS_URL`, add-on `url`, addon/DOCS.md).

## [0.2.26] — 2026-10-19

### Fixed

- **Example sequences ship in the package (#287).** package-data now
  includes `sequences/*.json` (29 files; the wheel had none).

## [0.2.25] — 2026-10-19

### Fixed

- **15508/15509 reported as read (#285).** The #34 per-mode blanking, the
  0.2.19 "unknown" note and the rewritten Explorer descriptions are removed
  (owner: report what the register reports).

## [0.2.24] — 2026-10-19

### Changed

- **Modbus address terms; 4xxxx in the export (#273).** `holding_register_4x`
  (CSV "Holding register (4xxxx)"); the `addressing` note uses Modbus terms
  (PDU address, register number, holding-register reference). Explorer
  "SunSpec 4xxxx" renamed "4xxxx".

## [0.2.23] — 2026-10-19

### Fixed

- **Base-0 is FranklinWH's PICS numbering (#273, #279).** Labels, the export
  `addressing` note and docs said Base-1; the library's PICS cross-reference
  records 0-based PDU addresses (ControllerHb 1092).

## [0.2.22] — 2026-10-19

### Fixed

- **Restored features removed without being asked (#277):** extensions panel
  JSON/CSV, the SunSpec 4xxxx address view, the export's This model scope.

## [0.2.21] — 2026-10-19

### Fixed

- **Register addresses: base-0 (default) and base-1, no 40001+ (#273).** The
  aGate serves SunSpec at base 0 and answers 4xxxx with exception 2. Export:
  `address_base0` / `address_base1` (CSV: two columns) and an `addressing`
  note.

## [0.2.20] — 2026-10-19

### Changed

- **M702 nameplate is the battery rating (#269).** Trusted as reported
  (`m702_nameplate`); calibrated and observed-confirmed still preferred; no
  M702 rating → refused (never 5000 W assumed); #133 guard unchanged. Docs
  corrected: the aGate does report a nameplate rating.
- **Export… includes the FranklinWH extensions (#228)**, marked not SunSpec
  (JSON `franklinwh_extensions` block with a note; CSV rows `ext`). The
  extensions panel's JSON/CSV buttons are removed. 15508/15509 descriptions
  say both read the active reserve on V10R01B04D00.

## [0.2.19] — 2026-10-19

### Fixed

- **One top-bar rule: wrap, never hide (#262 follow-up).** Per-item
  breakpoints removed; 700–767 px had neither the phone layout nor the
  status dots.
- **Unknown reserve shows no slider (#256).** Dashboard and Controls show the
  inactive mode's reserve as unknown, with no write; 15508 = 15509 = the
  active reserve on V10R01B04D00 (owner reading). Other write paths: #266.

## [0.2.18] — 2026-10-18

### Added

- **Phone layout (#262).** At ≤699 px: bottom tab bar (max(4, width/64)
  slots, last is More), More sheet (overflow tabs, Guide to the published
  docs, Support info), wrapping top bar and device bar. Desktop unchanged.
  The console gate gains a phone-width pass.

## [0.2.17] — 2026-10-18

### Changed

- **Device bar: SoC instead of firmware (#258).** Firmware moved to Info
  (Diagnostics) and stays in Support info. Support info gains each gateway's
  `battery_rating` per direction: M702 nameplate, dispatch rating, source,
  trusted.
- **Explorer Export… in the top bar (#228).** Scopes: all models (default),
  displayed models (the tree after its filters), or chosen models.

### Fixed

- **Raw 15509 visible (#256).** The poller keeps `vreg_15508`/`vreg_15509`
  as read, so the Explorer shows 15509 in any mode (#34's gating unchanged
  pending franklinwh-modbus#63). 16002 relabelled BBBackupSoc, not the TOU
  reserve.

## [0.2.16] — 2026-10-17

### Fixed

- **The add-on builds the release it's named for (#253, S2).** 0.2.15 moved
  the version but not `addon/Dockerfile`'s `BRIDGE_REF`, so the 0.2.15 add-on
  installed the 0.2.14 code and #247's fix didn't ship. `BRIDGE_REF` is now
  v0.2.16; `test_addon_release` fails if it doesn't match the version, and
  `test_smoke` no longer pins a literal version.

## [0.2.15] — 2026-10-17

### Fixed

- **No bridge logout modal under Home Assistant ingress (#247, S2).** Any 401
  raised the "You have been logged out" modal, whose "Log in" went to a
  bridge login page ingress doesn't serve ("page not found"). Pages served
  through ingress are marked (`data-ha-ingress`); there a 401 shows no modal
  and never leads to `/login`. The page keeps polling while HA renews its
  session, and after 90 s of 401s a banner offers Reload (of the HA page).
  The bridge logs a request that carries an ingress header but isn't trusted
  as ingress, so the two 401 sources can be told apart.

### Tests

- **Route checks work with FastAPI 0.137 (#248).** `app.routes` now holds
  included routers, so the dashboard-YAML auth test failed and the admin
  write-gate test (#102) checked no route at all. Both now work by request;
  every write route is called as a viewer and must be refused.

## [0.2.14] — 2026-10-17

### Fixed

- **A timed force is released once, by the bridge, under the Modbus lock
  (#242, S2).** The bridge passed the duration to franklinwh-modbus, whose
  own timer released the command from another thread without the gateway's
  Modbus lock (its writes could interleave with a poll) and with no audit,
  on top of the bridge's watchdog release. The library timer is no longer
  used.

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
