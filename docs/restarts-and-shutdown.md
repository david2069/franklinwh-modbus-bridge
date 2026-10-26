# Restarts and shutdown — operating practice

A bridge restart is never free. Every restart, however it is caused:

- **releases any active battery command** (force charge/discharge stops), and
- **re-evaluates schedules on boot**, so an entry whose window is open fires again.

Treat a restart of a bridge that controls a real aGate like a change to the
equipment: announce it, time it, and leave a reason in the record.

## What causes a restart

| Cause | Typical trigger | Announced? |
|---|---|---|
| Code reload | Any edit under `src/` **only** when running with `docker-compose.dev.yml` (source bind-mounted, `uvicorn --reload`) | No — **one restart per saved file** |
| Container stop/restart | `docker compose restart`, `up -d` after a rebuild, Docker Desktop update | No |
| Host reboot / power loss | macOS update, sleep policy, outage | No (power loss: no shutdown at all) |
| Crash | Unhandled error in the process | No shutdown record |
| HA add-on | Add-on update or restart from the Supervisor | No |

None of these carries a reason today. See *Audit trail* below.

## Before restarting (or editing source on) a live bridge

1. **Check nothing is running.** Controls tab shows no active command; the
   Schedule tab shows no entry inside its window.
2. **Avoid the busy window.** On the reference site keep 17:30–20:00 clear
   (the 18:00 force discharge runs then).
3. **Tell the owner first** if it is not your bridge — including an agent
   session about to edit Python files under `src/`.
4. **Batch edits.** With the dev overlay's `--reload`, every save is a restart; three files saved
   is three restarts. Prefer working on a branch/worktree and syncing once.
5. **Afterwards**, check the logs for `failed to start` and confirm the
   schedule engine started with the expected number of entries.

## Live bridges should not hot-reload

`docker-compose.yml` runs the code baked into the image: no `./src` mount and
no `--reload` (since 2026-10-12). Only a deliberate rebuild and restart changes
the running code:

```bash
docker compose up -d --build     # one restart; then check the logs
docker logs --since 2m fwhbridge-app | grep -E "Connected to aGate|failed to start"
```

For hot-reload development, layer the dev overlay — against a mock gateway,
never the live bridge:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

## Audit trail — what is recorded today

| Record | Where | Contents |
|---|---|---|
| `shutdown` / `startup` | `startup_log` table | version, environment — **no reason** |
| `shutdown` per gateway | `control_log` table | the command released, or "no active command", with a hardware snapshot |
| `startup_hw_snapshot` | `control_log` | WSet state read on boot |
| "Bridge shutting down — releasing control…" | Logs tab (`logs` table) | the INFO line |
| "Admin action: …" and an `ADMIN_ACTION` alert | Logs tab, `alerts` table | a gateway start, stop or restart, a bridge restart from Settings (Docker), or a poll interval change, with the user |

Known gaps (tracked in #45):

- no **reason** — a reload, a Docker stop, a reboot and an operator restart look identical;
- an **unclean start** (a `startup` with no `shutdown` before it = crash or power loss) is not flagged;
- `startup_log` is **not exposed** in the UI or REST API;
- a restart is not linked to the **command it cut short** or the **schedule it re-fired**.

To review restarts until those land, query the database:

```sql
SELECT datetime(ts, 'unixepoch', 'localtime'), event, detail
  FROM startup_log
 WHERE event IN ('startup', 'shutdown')
 ORDER BY ts DESC LIMIT 50;
```

A `startup` row not preceded by a `shutdown` row means the previous run ended
without a graceful shutdown.
