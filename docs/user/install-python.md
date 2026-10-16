# Install with Python (no container)

For running the bridge straight from a Python virtual environment: development, or a machine where you don't want Docker. For a bridge that controls your battery every day, the [add-on](install-addon.md) or [Docker](install-docker.md) is easier to keep running.

## Before you start

- **Python 3.11 or later.** On macOS, `python3` is often 3.9 from Apple; use Homebrew's `python3.12` (or similar). An older Python fails with *"editable mode currently requires a setuptools-based build"*, which doesn't mention the version.
- Git.
- Port **8099** free on the machine.
- An MQTT broker if you want Home Assistant entities. The bridge runs without one.

## 1. Install

```bash
git clone https://github.com/david2069/franklinwh-modbus-bridge.git
cd franklinwh-modbus-bridge
python3.12 -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
```

This installs everything, including the `franklinwh-modbus` library, from PyPI. Don't install a different version of the library by hand: the bridge needs the version `pyproject.toml` asks for.

## 2. Start it

Run it from the repository folder, because the bridge keeps its data in `./data` under the folder you start it from.

```bash
export ALLOW_INSECURE_AUTH=1   # sign in over plain http://; see below
bridge run
```

It listens on port **8099** on every interface (`bridge run --port 8100 --host 127.0.0.1` to change that).

## 3. Sign in

Open `http://localhost:8099`.

- The first start creates the user **admin** with a generated password and prints it once:
  `Seeded admin user 'admin' with a GENERATED password: …`. To choose your own, set `ADMIN_PASSWORD` before the **first** start. It is ignored once a user exists.
- Without `ALLOW_INSECURE_AUTH=1` the sign-in page refuses plain HTTP: *"login requires HTTPS — set ALLOW_INSECURE_AUTH=1 for a LAN-only setup"*. It doesn't bypass the login. Behind TLS, leave it unset and set `SESSION_COOKIE_SECURE=1`.
- Lost the password? `bridge recovery reset-admin-password --allow-host`, run from the same folder. See [admin recovery](../admin-recovery.md).

Then the [setup wizard](setup-wizard.md) opens.

## Check it works

```bash
bridge status                              # environment dev, data dir, schema version
curl -s http://localhost:8099/api/status   # "status":"ok"
```

## Settings you set yourself

| What | How |
|---|---|
| aGate | The [setup wizard](setup-wizard.md), or `MODBUS_HOST` before starting. |
| MQTT broker | **Settings → Home Assistant → MQTT broker**. Until one is set the log repeats `MQTT error: [Errno 61] Connection refused` (it tries `localhost:1883`); that's harmless. |
| Timezone | The machine's own clock. The bridge records it on first start and asks you to confirm it. |
| Log level | `logging: {level: DEBUG}` in a `config.yaml` in the start folder. `LOG_LEVEL` is not read yet (known issue #181). |

## Updating

```bash
git pull
pip install -e ".[dev]"
```

Stop the bridge (Ctrl+C) and start it again. Migrations run at start and are forward-only, so take a backup first (**Settings → Data & Backup → Create Backup**).

## Uninstalling

Stop the bridge, then delete the folder. That deletes `./data` too: the database, backups and settings. Before you do, remove the bridge's entities from Home Assistant: see [Fixing, moving and removing entities](home-assistant-entities.md).

## Running it as a service

Not tested yet. If you run it under systemd or launchd, start it from the repository folder (`WorkingDirectory=`) so it finds `./data`, and stop it with SIGTERM so it releases any active battery command cleanly.
