# Install as a Home Assistant add-on

The add-on is the easiest way if you run **Home Assistant OS** or **Supervised**. It needs the Supervisor; HA Container can't install add-ons, so use [Docker](install-docker.md) there.

## Before you start

- An **MQTT broker** so entities reach HA. Install the **Mosquitto broker** add-on, before or after this one; the bridge finds it by itself.
- Your aGate's **IP address**, with **Modbus TCP enabled** on the aGate (an installer setting).
- **No other Modbus client** polling the same aGate (see [One Modbus client per aGate](../index.md#what-you-need)).

## 1. Add the repository

Home Assistant 2026.10 and later:

1. **Settings → Apps → Install app** (bottom right). That opens the **App store**.
2. **⋮** (top right) → **Repositories**.
3. Press **+ Add** (bottom right), paste
   `https://github.com/david2069/franklinwh-modbus-bridge` and press **Add**.
4. Close the Repositories page. The store now lists **FranklinWH Modbus Bridge**.

!!! note "Older Home Assistant"
    In versions before 2026.10 this is **Settings → Add-ons → Add-on Store → ⋮ →
    Repositories**: paste the URL into the dialog and press **Add**.

!!! warning "The 'Add repository' button"
    The my.home-assistant "Add repository" badge currently opens the store without
    adding anything on HA 2026.10 (#148). Use the steps above.

## 2. Install and start

1. In the App store, open **FranklinWH Modbus Bridge** → **Install**. The first install builds the image on your HA host, which takes a few minutes, longer on a Raspberry Pi.
2. **Configuration** tab (all optional):

    | Option | Meaning |
    |---|---|
    | `gateway_host` | aGate IP, e.g. `192.168.1.50`. Leave it empty to find the aGate with the setup wizard. |
    | `gateway_port` | Modbus TCP port, normally `502`. |
    | `gateway_unit_id` | Modbus unit id, normally `1`. |
    | `poll_interval` | Seconds between polls. `10` is a good default. |
    | `log_level` | `INFO` normally; `DEBUG` when diagnosing. |

3. **Info** tab → **Start**.
4. Turn on **Show in sidebar**. A new add-on has it off, so it won't appear in the sidebar until you do.
5. Open **Modbus Bridge** from the sidebar, or press **Open Web UI**. On a fresh install the [setup wizard](setup-wizard.md) opens.

!!! tip "Sidebar still says 'FranklinWH'?"
    Before 0.2.8 the sidebar entry was called **FranklinWH**. HA keeps the old name
    until the entry is re-created: turn **Show in sidebar** off and on again.

## What the add-on does for you

- **No second login.** The UI is served through HA ingress, and HA has already signed you in.
- **MQTT configures itself** from the Supervisor's Mosquitto service.
- **Timezone** comes from HA (Settings → System → General). The wizard asks you to confirm it.

## Updating

When a new version is released, HA offers **Update** on the add-on's page. Use **Update**, not uninstall and reinstall.

!!! danger "Uninstalling deletes the add-on's data"
    Until #153 is fixed, the bridge keeps its database in the add-on's private
    storage, which Home Assistant deletes on **every** uninstall, even if you choose
    to keep data. Schedules, history, calibration and settings are lost. Update in
    place instead.

!!! info "An update restarts the bridge"
    A restart releases any active battery command and re-runs a schedule whose
    window is open. Update when nothing is running, e.g. not during your evening
    discharge.

## Optional: direct port 8099

The bridge's port 8099 is off by default; the UI works through HA ingress. If you
publish it (add-on **Configuration → Network**), it asks for a **bridge login**
(since 0.2.7). Use the admin password from the add-on's first-start log, or create
a user in **Settings → Admin**.
