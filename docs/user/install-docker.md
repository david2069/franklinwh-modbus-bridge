# Install with Docker

For running the bridge without Home Assistant's Supervisor: a NAS, a small server, or next to HA Container.

## Before you start

- **Docker with Compose v2** (`docker compose version` works). For Podman, see [Podman](#podman) below.
- **Git**, to fetch the source. The image is built on your machine; there is no published image.
- About **3 GB** of disk for the first start (see [What it starts](#what-it-starts)), and a few minutes for the first build. Builds on amd64 and arm64 (e.g. Raspberry Pi 4/5, Apple silicon).
- Free host ports **8100** (web UI) and **1886** (the bundled MQTT broker).
- Your aGate's **IP address**, with **Modbus TCP enabled**, and **no other Modbus client** polling it. Or none: you can try a demo gateway first.

## 1. Get the source and make `.env`

```bash
git clone https://github.com/david2069/franklinwh-modbus-bridge.git
cd franklinwh-modbus-bridge
cp .env.example .env
```

Edit `.env` **before the first start**. These are read once, on first start:

| Setting | Set it to | Why |
|---|---|---|
| `TZ` | Your zone, e.g. `Europe/London` | Schedules and tariff windows run on this clock. If unset, the bridge runs on `Australia/Sydney` and records it; changing it later raises a *TIMEZONE CHANGED* error until you confirm (#176). |
| `ADMIN_PASSWORD` | A password of your own | If unset, the first admin is **admin / admin** (#177). |
| `ADMIN_USERNAME` | Optional | Defaults to `admin`. |

And these whenever they apply:

| Setting | Set it to |
|---|---|
| `MQTT_HOST` | `mosquitto` for the broker this compose file starts, or your own broker's address. **Not** `localhost`: inside the container that is the bridge itself. |
| `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` | Your broker's, if it isn't the bundled one. |
| `MODBUS_HOST` | Your aGate's IP, to connect straight away. Leave it empty to use the [setup wizard](setup-wizard.md). |
| `LAN_SUBNET` | e.g. `192.0.2.0/24`, if the wizard should search a LAN and you open the UI through `localhost` or a proxy. |

!!! warning "MQTT is set in `.env`, not in the UI"
    The shipped compose file always passes `MQTT_HOST` to the bridge, so
    **Settings → Home Assistant → MQTT broker → Edit** refuses a new host:
    *"Set by MQTT_HOST in the bridge's environment; change it there and restart"*.
    Change `.env` and run `docker compose up -d`.

## 2. Build and start

```bash
docker compose up -d --build
```

## 3. Sign in

Open `http://<host>:8100` and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` (**admin / admin** if you didn't set them). Change the password at once under **Settings → Admin → Users**.

You'll see, in order: the **Unofficial software** notice (tick and **Continue**), then the [setup wizard](setup-wizard.md). Use the wizard's timezone step, or the **Confirm the bridge's timezone** banner, to confirm `TZ`.

`ALLOW_INSECURE_AUTH=1` (on in the shipped compose file) lets you sign in over plain HTTP on your LAN. It doesn't bypass the login. Behind TLS, set `ALLOW_INSECURE_AUTH=0` and `SESSION_COOKIE_SECURE=1` in `.env`.

Locked out? See [admin recovery](../admin-recovery.md).

## Check it works

```bash
docker compose ps                                 # fwhbridge-app and fwhbridge-mosquitto Up
curl -s http://localhost:8100/api/status          # "status":"ok","environment":"docker"
docker compose logs app | grep -E "Bridge started|MQTT connected"
```

You want `Bridge started (env=docker, v…)` and `MQTT connected to mosquitto:1883`. A repeating `MQTT error: [Errno 111] Connection refused` means `MQTT_HOST` is wrong (usually `localhost`).

## What it starts

| Container | Port | What |
|---|---|---|
| `fwhbridge-app` | 8100 → 8099 | The bridge. Data in the `app-data` volume. |
| `fwhbridge-mosquitto` | 1886 → 1883 | An MQTT broker, anonymous access, for the bridge and HA. |
| `fwhbridge-homeassistant` | 18125 → 8123 | A **separate, empty Home Assistant** (2.5 GB image), not your own HA (#180). |

If you already run Home Assistant, you don't need the last one. Start only the bridge and the broker:

```bash
docker compose up -d --build app
```

To point your own HA at the bundled broker, add the MQTT integration with host `<docker host>` and port `1886`. To connect the bridge to your HA's entities, use **Settings → Home Assistant → + Add Home Assistant**.

## Updating

```bash
git pull
docker compose up -d --build
```

Data lives in the `app-data` volume and survives updates. Migrations are forward-only, so take a backup first (**Settings → Data & Backup → Create Backup**).

!!! info "A rebuild restarts the bridge"
    It releases any active battery command and re-runs a schedule whose window is
    open. Deploy when nothing is running.

## Uninstalling

1. Remove the bridge's entities from Home Assistant first: see [Fixing, moving and removing entities](home-assistant-entities.md). Once the bridge is gone, nothing clears its retained MQTT topics.
2. Stop and remove the containers and **all their data**:

    ```bash
    docker compose down -v
    ```

    Without `-v` the volumes (database, broker data) stay, ready for a later `up`.
3. Remove the images if you want the disk back: `docker image ls` shows `franklinwh-modbus-bridge-app` (or `_app`), `eclipse-mosquitto` and `home-assistant`.
4. Delete the `franklinwh-modbus-bridge` folder.

## Podman

Tested with Podman 6 (`podman machine` on macOS, arm64) and `podman-compose` as the compose provider. It works with these differences:

- `podman compose` needs a provider: `docker-compose` or `podman-compose` on the `PATH`.
- Use the commands above with `podman` in place of `docker`, including for [admin recovery](../admin-recovery.md).
- Podman builds OCI images, which drop the image's `HEALTHCHECK`, so `ps` shows no health for the bridge.
- **Admin recovery refuses** with *"not inside a container"*, because Podman has no `/.dockerenv` (#179). Add `--allow-host`; under compose the database is the right one (`Database: /data/bridge.db`).
- The wizard may propose Podman's own network (`10.89.0.0/24`) as your LAN (#183). Use **Enter address**, or set `LAN_SUBNET`.
- Always run the image through the compose file. A plain `podman run` without `APP_ENV=docker` stores the database outside the `/data` volume, and it's lost when the container is removed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `MQTT error: [Errno 111] Connection refused` in the log | `MQTT_HOST` in `.env` is `localhost`. Set it to `mosquitto` (bundled broker) or your broker, then `docker compose up -d`. |
| *TIMEZONE CHANGED* error in the log | `TZ` changed since first start. Confirm the new zone in the banner, or set `TZ` back. |
| Can't change the broker in Settings | Expected: set `MQTT_*` in `.env`. |
| *login requires HTTPS* | `ALLOW_INSECURE_AUTH` is off and you're on `http://`. Use HTTPS, or set it to `1` on a trusted LAN. |
| `log_level`/`LOG_LEVEL=DEBUG` changes nothing | Known issue #181: `LOG_LEVEL` isn't read yet. |
