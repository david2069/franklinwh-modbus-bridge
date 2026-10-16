# Install with Docker

For running the bridge without Home Assistant's Supervisor: a NAS, a small server, or next to HA Container.

## Run it

```bash
git clone https://github.com/david2069/franklinwh-modbus-bridge.git
cd franklinwh-modbus-bridge
cp .env.example .env        # set MODBUS_HOST, MQTT_HOST, TZ, ADMIN_PASSWORD …
docker compose up -d --build
```

Open `http://<host>:8100` and sign in.

- The first admin comes from `ADMIN_USERNAME` / `ADMIN_PASSWORD` in `.env`. If `ADMIN_PASSWORD` isn't set, a password is generated and printed to the log (`docker compose logs | grep -i password`). Change it under **Settings → Admin**.
- Locked out? See the admin recovery runbook (`docs/admin-recovery.md` in the repository).

## Settings you set yourself

| What | Where | Note |
|---|---|---|
| Timezone | `TZ` in `.env` (e.g. `Australia/Sydney`) | Schedules and tariff windows run on this clock. The bridge asks you to confirm it. |
| MQTT broker | **Settings → Home Assistant → MQTT broker** | No Supervisor to ask, so enter host, port and credentials. |
| aGate | `MODBUS_HOST` in `.env`, or the [setup wizard](setup-wizard.md) | One Modbus client per aGate. |

`ALLOW_INSECURE_AUTH=1` (on in the shipped compose file) lets you sign in over plain
HTTP on your LAN. It doesn't bypass the login. Behind TLS, remove it and set
`SESSION_COOKIE_SECURE=1`.

## Updating

```bash
git pull
docker compose up -d --build
```

Data lives in the `app-data` volume and survives updates. Migrations are forward-only, so take a backup first (**Settings → Data & Backup → Create Backup**).

!!! info "A rebuild restarts the bridge"
    It releases any active battery command and re-runs a schedule whose window is
    open. Deploy when nothing is running.
