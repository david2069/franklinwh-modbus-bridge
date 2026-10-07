# Changelog

What changed in the **FranklinWH Modbus Bridge** add-on, newest first. Home
Assistant shows this file as the add-on's **Changelog** link.

The developer changelog, with every change in detail, is
[CHANGELOG.md](https://github.com/david2069/franklinwh-modbus-bridge/blob/main/CHANGELOG.md)
in the repository.

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
