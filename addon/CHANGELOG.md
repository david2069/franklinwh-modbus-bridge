# Changelog

What changed in the **FranklinWH Modbus Bridge** add-on, newest first. Home
Assistant shows this file as the add-on's **Changelog** link.

The developer changelog, with every change in detail, is
[CHANGELOG.md](https://github.com/david2069/franklinwh-modbus-bridge/blob/main/CHANGELOG.md)
in the repository.

## 0.2.41 — 2026-10-24

### Fixed
- **Force Discharge works again on aGate firmware V12.** The battery is now
  commanded in watts, so a 5000 W discharge runs. V12 still refuses Force Charge
  over Modbus; the bridge says so.
- **A schedule the aGate refuses is not retried all evening.** You get one
  notification, and the next window tries again.

### Changed
- **Power %** is now that share of your battery's rating, sent in watts (50 % of
  5000 W is 2500 W). Like watts, it needs a battery rating.
- **WSet Mode** shows Pct or Watts, as the aGate reports it.

## 0.2.40 — 2026-10-24

### Fixed
- **Logs:** some lines were filed under the wrong gateway, so the gateway filter
  and the export showed them wrongly.

## 0.2.39 — 2026-10-23

### New
- **Export the app log:** **Export text** and **Export JSON** on the Logs page
  download the log for the filters you've set. Serial numbers and IP addresses
  are masked. Admins only.

## 0.2.38 — 2026-10-23

### Fixed
- **Dialogs and forms:** errors and results now show inside the open dialog or
  form and stay there, instead of a pop-up that vanished. The setup wizard no
  longer moves on when confirming the timezone fails.
- **Bridge heartbeat** no longer reads "unknown" for the first minute after a
  restart.

## 0.2.37 — 2026-10-22

### New
- **Alerts:** the bridge now raises an alert when a battery command is refused,
  when the aGate stops answering, when live data stops arriving, or when one of
  its own background tasks stops.
- **Home Assistant notifications:** every critical alert also appears in HA's
  notifications (the bell). Acknowledging it in the bridge removes it from HA.
- **New device "FranklinWH Modbus Bridge"** with two sensors, **Bridge problem**
  and **Bridge heartbeat**. See the new docs page *Alerts and health sensors*
  for two automations that tell you if the bridge stops.
- The alert pop-up and the notifications screen come in a later release.

## 0.2.36 — 2026-10-21

### New
- **Battery-control writes** at the bottom of **Logs**: every command the
  bridge sent to the aGate, the exact value, the aGate's reply and who sent
  it, with **Download CSV / JSON**.

### Fixes
- The **battery power register 15013** now shows a signed value (e.g. -40 W,
  not 65496 W).
- The **15016** description no longer claims "off-grid".

## 0.2.35 — 2026-10-21

### Fixes
- **HA shows the bridge offline if it stops unexpectedly**, rather than
  showing frozen values as if they were live.
- **A failed battery command says what went wrong in plain words**, and
  whether anything ran, e.g. "Rejected by the aGate (Modbus exception 3:
  illegal data value …). The command did not run; the battery carries on as
  before."

### Docs
- New guide: alerts on your phone, the bridge on your home screen, reaching
  it away from home, and backups off the premises.

## 0.2.34 — 2026-10-21

### Fixes
- **Solar, home load, mode and reserves no longer go missing after a network
  drop.** The bridge now notices within about 30 seconds, reconnects to the
  aGate and logs a warning. Before, they could stay missing until a restart.
- **The library's own warnings** now appear in the bridge's Logs.

### Changes
- **Guide** in the sidebar, and **Discussions** links in the app and the
  docs: ask anything there.

## 0.2.33 — 2026-10-21

### Fixes
- **Active reserve in Emergency Backup** now shows the reserve the aGate
  reports for that mode, not the self-consumption reserve.
- **Events lists** on the Dashboard show register addresses as Base-0
  (e.g. 76, not 40076), labelled as such.

## 0.2.32 — 2026-10-21

### Fixes
- **Works with aGate firmware V12R02B60D00.** The new firmware also answers
  at a second set of Modbus addresses; the bridge now stays on the aGate's
  usual ones, so battery commands and the SunSpec Explorer's addresses are
  as before. Update before restarting the bridge on the new firmware.
- **Firmware version follows an aGate update** without restarting the
  bridge (status, SunSpec export, support bundle).

## 0.2.31 — 2026-10-19

### Changes
- **Power % is back** for Force Charge and Force Discharge: on the
  dashboard (W / % switch), in schedules (W or %) and in Home Assistant
  (Command Power %). A % is sent to the aGate as that exact percentage of
  what it can do, so it works on any system without a battery rating.

### Fixes
- Two example sequences labelled "charge" actually discharged; fixed.

## 0.2.30 — 2026-10-19

### Changes
- **SunSpec Explorer has tabs: Models, FranklinWH extensions and Modbus
  reader.** Each fills the page, so the reader is no longer hidden below the
  extension registers. Export… works from any tab.

## 0.2.29 — 2026-10-19

### Fixes
- **The Modbus reader runs while a battery command is active**, like the
  original reader.

## 0.2.28 — 2026-10-19

### Changes
- **SunSpec Explorer: Modbus reader** (admins). Runs the franklinwh-modbus
  reader against your aGate: read a raw register range, scan models, read a
  point, show only non-zero values, or match raw values against SunSpec
  points. It only reads. A register the aGate doesn't answer shows the Modbus
  error. Polling pauses for the few seconds it runs.

### Fixes
- **The bridge no longer leaves old connections open to the aGate** when it
  reconnects. The aGate serves one Modbus client at a time.

## 0.2.27 — 2026-10-19

### Changes
- **Documentation links open the formatted user guide**
  (david2069.github.io/franklinwh-modbus-bridge) instead of the GitHub
  README: the sidebar's Docs (at the page for the tab you're on), the legal
  notice, and this add-on's page.

## 0.2.26 — 2026-10-19

### Fixes
- **The Sequencer comes with its example sequences.** They were left out of
  the installed package, so the Sequencer started empty.

## 0.2.25 — 2026-10-19

### Fixes
- **Both reserves show what the aGate reports.** Self Reserve shows register
  15508 and TOU Reserve register 15509, as read, on the dashboard, in Home
  Assistant and in schedules. 0.2.19's "unknown" and its note are gone, and
  the SunSpec Explorer's descriptions of these registers are back to the
  originals.

## 0.2.24 — 2026-10-19

### Changes
- **SunSpec Explorer uses Modbus's own terms for addresses.** Base-0 is the
  address sent (and FranklinWH's PICS numbering), Base-1 the register number,
  and **4xxxx** the holding-register reference (40001 = register 1). Exports
  now include the 4xxxx reference too.

## 0.2.23 — 2026-10-19

### Fixes
- **SunSpec Explorer: Base-0 is how FranklinWH's official SunSpec PICS
  numbers registers.** 0.2.21 said Base-1 matched FranklinWH's spreadsheet;
  that was wrong. Base-0 stays the default; Base-1 is now described as the
  numbering some Modbus tools use.

## 0.2.22 — 2026-10-19

### Fixes
- **SunSpec Explorer: put back what 0.2.17–0.2.21 removed.** The extension
  registers' own JSON / CSV buttons, the "SunSpec 4xxxx" address view (with a
  note that the aGate doesn't answer those addresses), and "This model" in
  Export… are back, alongside the new options.

## 0.2.21 — 2026-10-19

### Fixes
- **SunSpec Explorer shows register addresses your aGate actually answers.**
  The default view was the conventional 40001+ numbering, which the aGate
  rejects. It now shows **Base-0** (the address you send) by default, or
  **Base-1** (as in FranklinWH's spreadsheet). Exports carry both, with a note.

## 0.2.20 — 2026-10-19

### Changes
- **The battery rating is the aGate's nameplate.** Force charge and
  discharge use the rating the aGate reports over SunSpec (Model 702) as soon
  as the bridge reads it; calibrating is optional. The bridge still stops
  using a rating if the battery is seen running well above it.
- **SunSpec Explorer → Export… can include the FranklinWH extension
  registers**, in their own section marked as not SunSpec. The extensions
  panel's separate JSON / CSV buttons are gone.

## 0.2.19 — 2026-10-19

### Fixes
- **The top bar no longer hides things on narrower screens.** It wraps onto
  a second line instead, at every width. Before, some widths (an iPad mini)
  lost the Grid / Solar / Load indicators.
- **The reserve of the mode that isn't active shows as unknown**, with no
  slider. The aGate only reports the active mode's reserve over Modbus;
  before, the other one showed a slider in the middle with an empty box, and
  moving it could change the active reserve. Set it in the FranklinWH app.

## 0.2.18 — 2026-10-18

### Changes
- **A phone layout, like the Direct Connect bridge.** On a phone (including
  the Home Assistant app) a bar at the bottom replaces the sidebar; **More**
  holds the other pages, the **Guide** and **Support**. The top bar wraps
  instead of cutting off the page title and hiding the status dots. Tablets
  and desktops are unchanged.

## 0.2.17 — 2026-10-18

### Changes
- **The dashboard's device bar shows the battery SoC** instead of the
  firmware version. Firmware is now under **Info**, and in Support info.
- **SunSpec Explorer: Export… is at the top of the page**, next to Refresh
  Catalog. Export all models, the models the list is showing, or the ones you
  pick.
- **Support info includes the battery's rated charge and discharge power**
  (as the aGate reports it) and the rating the bridge sizes force commands
  against.

### Fixes
- **SunSpec Explorer shows the TOU reserve register (15509)** in every mode;
  it was blank outside TOU. Register 16002 is relabelled: it is not the TOU
  reserve (it matches the backup SoC, `BBBackupSoc`).

## 0.2.16 — 2026-10-17

### Fixes
- **0.2.15's fix is now actually included.** The 0.2.15 add-on was built
  from the 0.2.14 code by mistake, so it didn't contain the change below.
  This release does.

## 0.2.15 — 2026-10-17

### Fixes
- **No more "You have been logged out" inside Home Assistant.** Home
  Assistant signs you in, but when its session for the add-on page lapsed
  (a sleeping laptop, a background tab) the bridge showed its own logout
  message, and its **Log in** button led to "page not found". Now the page
  waits for Home Assistant to renew the session. If that takes more than
  90 seconds, a small banner offers **Reload**. Opening the bridge directly,
  outside Home Assistant, works as before.

## 0.2.14 — 2026-10-17

### Fixes
- **A force with a time limit now ends once, cleanly.** The Modbus library
  also ran its own timer, which ended the command a second time without
  coordinating with the bridge's readings, and without an entry in the
  Activity Log. Only the bridge's timer is used now.

## 0.2.13 — 2026-10-17

### Fixes
- **Connecting to the aGate no longer changes its mode and reserves.** Each
  time the bridge started or reconnected, the Modbus library briefly wrote
  test values to the operating mode (it could switch to Emergency Backup for a
  moment) and both reserves, then put them back. If putting them back failed,
  the aGate kept the test value. That test is now turned off.

## 0.2.12 — 2026-10-17

### Fixes
- **Force Charge and Force Discharge can no longer go out in the wrong
  direction after a reconnect.** If the bridge reconnected to the aGate just as
  a force command was sent, and your calibrated battery rating was higher than
  the aGate's nameplate rating, the command could go out reversed at full rate.
  A single aPower calibrated at its nameplate rating was not affected.

### New
- **SunSpec Explorer → Export…**: export this model, chosen models or all of
  them as JSON or CSV, with a preview. The serial number is redacted unless you
  turn that off.
- **Save as Gist** on the export and on Settings → Admin → Support info.
- Extension registers: an **All / Identified / Unknown** filter.

## 0.2.11 — 2026-10-17

### Fixes
- **Switching off the Control group now stops MQTT commands.** It removed the
  controls from Home Assistant, but the bridge still obeyed commands published
  to its MQTT command topics, so anything that could publish to your broker
  could still command the battery. It now stops listening while Control is
  off, and starts again when you switch it back on.
- With more than one gateway, switching a publishing group off now removes
  that group's entities for every gateway, not only the first.

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
