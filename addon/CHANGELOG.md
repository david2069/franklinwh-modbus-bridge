# Changelog

What changed in the **FranklinWH Modbus Bridge** add-on, newest first. Home
Assistant shows this file as the add-on's **Changelog** link.

The developer changelog, with every change in detail, is
[CHANGELOG.md](https://github.com/david2069/franklinwh-modbus-bridge/blob/main/CHANGELOG.md)
in the repository.

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
