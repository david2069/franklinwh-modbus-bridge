# Changelog

What changed in the **FranklinWH Modbus Bridge** add-on, newest first. Home
Assistant shows this file as the add-on's **Changelog** link.

The developer changelog, with every change in detail, is
[CHANGELOG.md](https://github.com/david2069/franklinwh-modbus-bridge/blob/main/CHANGELOG.md)
in the repository.

## 0.2.10 — 2026-10-17

### Fixes
- **The `log_level` option now works.** Set `DEBUG` when diagnosing; it had no
  effect before.

### Docs
- New pages: Developer Guide, Home Assistant setup (instance and notification
  device), and battery dispatch best practice.
- The rest of 0.2.10 fixes the Docker install; nothing else changes for the
  add-on.

## 0.2.9 — 2026-10-16

### Fixes
- **A schedule whose force command was refused showed "ok".** It now shows
  **failed** with the reason (e.g. battery power rating not confirmed), notifies
  you, and retries.
- **Schedules with an empty Power** now run at your battery's full (confirmed)
  rating. Before, they used whatever power was last set by hand.

### Docs
- New documentation site: https://david2069.github.io/franklinwh-modbus-bridge/

## 0.2.8 — 2026-10-16

### Fixes
- **Force charge/discharge on systems with several batteries could run at full
  power regardless of the setting** (#133). Commands are now sized against a
  battery power rating that has been confirmed. Until then they're refused,
  with a message saying how to calibrate.
  - **Single-battery sites that have already run a full-power dispatch** (e.g.
    a daily scheduled discharge) are confirmed automatically from their history.
  - **Others:** Settings → your gateway → **Battery power rating** →
    **Calibrate**, then run a TOU battery dispatch in the FranklinWH app.
- The power sliders are no longer capped at 5000 W; they follow your
  battery's rating.

### Removed
- **Command Power %.** Use Command Power in watts. Schedules saved with a % keep
  working until you edit them.

### Changed
- The sidebar entry is now called **Modbus Bridge**.

## 0.2.7 — 2026-10-16

### Security
- **Only Home Assistant's own sidebar panel (ingress) is trusted without a
  login.** Before, any other add-on on your Home Assistant, and anyone on your
  network once port 8099 was published, could use the bridge as an admin,
  including battery control, with no password. Update now.
- If you published port 8099, it now asks for a bridge login. Use the admin
  password from the add-on's first-start log, or create a user in **Settings →
  Admin** from the sidebar panel.

## 0.2.6 — 2026-10-16

### Fixes
- **Power History no longer drops samples above 15 kW.** A second 15 kW limit,
  in the history recorder, threw away every sample above it, so large arrays
  had gaps at high output. Root cause: a 15 kW "nothing residential is bigger"
  assumption added to filter corrupt readings; solar arrays can be any size.
- **Unpublish now stays unpublished** until you press Republish (it used to
  undo itself within a second), and covers every gateway.
- **Old devices from earlier versions are cleaned up automatically**: stale
  entries on the broker, and empty old "FranklinWH …" devices in Home
  Assistant. Other integrations' devices are never touched.

## 0.2.5 — 2026-10-16

### Fixes
- **Several aGates: each one now appears as its own Home Assistant device.**
  Extra gateways connected after start-up were never published, so only the
  first aGate showed in Home Assistant.
- **Every device is named "FranklinWH Modbus <short serial>"**, including extra
  gateways.
- **Large solar arrays (over 15 kW) now show their real output.** Readings above
  15 kW were replaced with an old value.
- **No more night-time "Rejected corrupted pv_total" warnings.** A small negative
  standby reading is shown as 0 W.

## 0.2.4 — 2026-10-15

### Home Assistant
- **Its own Home Assistant device: "FranklinWH Modbus <short serial>".** It is
  no longer merged with FWHAI's or the Local Bridge's device for the same aGate.
  After updating, delete the old empty device if one is left.
- **Choose the MQTT namespace.** New installs use `fwh_mb`, which keeps this
  bridge's device and entities apart from other FranklinWH integrations on the
  same Home Assistant (FWHAI, the Local Bridge). Existing installs keep
  `franklinwh` until you change it in **Settings → Home Assistant → MQTT
  Broker → Change namespace…**, which removes only the entries this bridge
  published.
- **Each install has its own MQTT client id**, so two integrations on one
  broker no longer knock each other's connection off.
- **Publishing to Home Assistant is opt-in per gateway** and switches on or off
  at once. A demo gateway is not published unless you choose to.
- **Dashboard card:** copy-paste YAML of exactly the entities this bridge
  publishes, with ids read from Home Assistant.
- The setup wizard's Home Assistant step can be skipped, explains the two links,
  and the "HA Entities" tab is now called **Home Assistant**.

### Energy and automations
- **Solar forecast** from Open-Meteo (free, no key) with a Dashboard card and
  `weather.*` / `solar_forecast.*` automation sensors.
- **Wholesale spot price** from AEMO NEM (Australia), or your retailer's price
  from Home Assistant (e.g. Amber), as automation sensors.
- **Dynamic (live) tariffs** in billing, priced from the spot or Home Assistant
  price as energy is used.

### Battery control and safety
- Scheduled dispatches are **verified** on the aGate, retried, re-asserted and
  reported if they don't take effect.
- **Command replies report what the command actually did**; an unknown command
  is refused.
- **Only admins can change settings.** New `api` role for REST-only access,
  with account expiry, sign-in activity and a lockout after repeated failed
  logins.

### Support and diagnostics
- **Support info for a GitHub issue** (Settings → Admin): a redacted summary of
  your setup to preview, copy and paste into the new GitHub issue form.
- SunSpec Explorer names the extension registers identified so far (marked
  unconfirmed). Link-health and extra register recording for diagnostics.

### Fixes
- Times are shown in the bridge's timezone, labelled.
- The Solar Forecast card is readable in Light mode.
- MQTT broker settings saved in Settings are used; **Save & Reconnect** applies them.
- Top bar indicators stay visible on medium windows; dropdowns are styled; the
  chart modal opens full-screen on a phone.

## 0.2.3 — 2026-10-08

### Setup wizard
- **Asks you to confirm the timezone** as one of its steps (it was a separate
  banner behind the wizard), and says where to change it if it's wrong.
- **One aGate found is ticked for you**, so *Next* works straight away.
- **Suggests the network of aGates you've already set up**, and explains what
  to type for an aGate you reach over Tailscale or a VPN.
- **The Home Assistant checklist counts only your current gateways' entities**,
  and tells you about any left over from removed gateways and where to delete them.
- **The top bar says *No gateway yet*** until an aGate or demo gateway exists.

### Battery control
- **The Release panel reads correctly:** WSetPct as *100%* (not *1000%*),
  *No limit* for a dispatch without a time limit (not *0m 0s* in red), and a
  new *Target SoC* row.
- **The control log records the right gateway** for each command, logs target
  SoC changes, and notes each command's time limit and target SoC.
- **Crash recovery covers every gateway**, not just the first one.

### Other
- Settings → Admin → Feature modules: corrected wording (turning a feature off
  removes it from the sidebar).

## 0.2.2 — 2026-10-08

### Fixed
- **The aGate's address now shows** on the Dashboard (it read *IP --*) and in
  the SunSpec Explorer (*GATEWAY :502*) when the aGate was set up with the
  setup wizard or in Settings.
- **Settings → Home Assistant → MQTT Broker shows the broker actually in use**
  (Mosquitto), not *localhost / (anonymous)*.
- **Light mode:** the timezone confirmation and *can't reach the bridge*
  banners were almost unreadable. They're now readable.

### Changed
- **The SunSpec Explorer follows the gateway selector**, so each aGate has its
  own catalog.
- **SunSpec Explorer and Sequencer start switched off on new installs.**
  They're expert tools, and the Sequencer writes directly to the battery's
  registers. Turn them on in Settings → Admin → Feature modules. If you
  already use them, they stay on after updating.

## 0.2.1 — 2026-10-07

### Fixed
- **Sequencer:** a run that took longer than 8 seconds reported *couldn't
  reach the bridge*, even though it carried on and completed. The page now
  waits for as long as the sequence can take, and shows its output.
- **Blank Settings and missing gateway names after an update.** The browser
  could keep using the previous version's scripts. Pages are now always
  re-checked after an update. If you saw this on 0.2.0, reload once.
- **The add-on Log tab now shows the bridge's own messages**, with a date and
  time on every line and the version on the first one. Routine requests are
  no longer logged unless `log_level` is DEBUG.
- **The user badge shows your Home Assistant name** instead of "(ingress)".

### New
- **Connection outages are logged.** When the *can't reach the bridge*
  banner clears, the bridge's log records how long it lasted and the last
  error.

## 0.2.0 — 2026-10-07

### New
- **Setup wizard.** On a fresh install the bridge asks what you want to do:
  connect your aGate, or explore with a demo gateway. It then:
  - searches your network for the aGate, or tests an address you type in;
  - names each aGate from its nameplate, for example *aGate 0091*;
  - waits for the first live reading;
  - ends with a Home Assistant checklist.

  You can run it again from **Settings → Gateways → Run setup again**.
- **Home Assistant entity access sets itself up.** The Home Assistant this
  add-on runs in is connected automatically, with no URL or token to create.
- **MQTT sets itself up, and Mosquitto is no longer required to install.**
  Without a broker the add-on waits and shows an *Install Mosquitto* prompt.
  It connects as soon as Mosquitto is running, without a restart.
- **Demo (mock) gateways simulate controls.** Force charge/discharge, mode and
  reserves change the demo's data, never a real battery. Their entities and
  controls appear in Home Assistant.
- **Settings is split into sections:** Gateways, Home Assistant, Energy &
  Automation, Data & Backup, and Admin.
- **Store page:** this changelog, a Documentation tab, and an icon.

### Fixed
- **No more phantom gateway.** An install with no aGate configured used to poll
  a made-up 192.168.1.100 forever, and you couldn't delete it. It now waits
  for an address.
- **Mock gateways reach Home Assistant**, and the commands HA sends them go to
  that gateway. Previously they always went to the first one.
- **Release** works on demo gateways.
- **Error messages stay on screen** until you dismiss them, and refused
  commands are logged.

### Changed
- **"Default Gateway" is renamed** to *aGate* plus the last four digits of its
  serial. A name you chose yourself is kept. MQTT topics and Home Assistant
  entity IDs don't change.
- Uses franklinwh-modbus 0.9.5.

## 0.1.0

First add-on release.
