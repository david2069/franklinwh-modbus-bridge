# Admin recovery (break-glass)

Use this when **no admin can log in**: forgotten password, the only admin
disabled or demoted, or the account deleted.

The bridge and your battery schedules keep running while you are locked out.
Only the web UI and REST API are unavailable.

## 1. Check what the bridge has

```bash
docker exec -it fwhbridge-app bridge recovery status
```

It prints the database it is reading, then every account:

```
Database:     /data/bridge.db
Schema:       v54
In container: yes
Accounts:     1
  admin                viewer  DISABLED  last login never
No enabled admin can log in. Fix: bridge recovery reset-admin-password
```

Check the first three lines. `Database` must be `/data/bridge.db`, and
`In container` must say `yes`. Anything else means you are looking at a
different copy from the one the bridge uses.

## 2. Reset

```bash
docker exec -it fwhbridge-app bridge recovery reset-admin-password
```

It shows the status again, asks for confirmation, then makes the account a
working, enabled admin:

| Situation | Result |
|---|---|
| Password forgotten | Password reset |
| Account demoted to user/viewer | Promoted back to admin |
| Account disabled | Re-enabled |
| Account deleted, or no accounts at all | Created as admin |

- **New password:** generated and printed **once** in your terminal. To choose
  one instead, add `--password 'at-least-8-chars'`. Nothing is emailed, and
  nothing leaves the machine.
- **Another account:** use `--username NAME` (default `admin`). The name is
  matched ignoring case.
- **No restart needed.** Log in at once, then change the password under
  **Settings → Admin → Users**.

## What it records

- **Logs tab:** a WARNING, "Auth: break-glass recovery: … (user=admin)".
- **Login audit** (once the login record from #50 is deployed): a
  `break_glass_reset` event. The account's open logins are ended, and those
  pages show "Your password was changed by an administrator" (ended by
  `recovery`).

## Safety rails

- **Inside the container only.** It refuses on the host unless you pass
  `--allow-host`, which is for a bridge installed without Docker. FWHAI's tool
  once reported success while editing a host-side copy of its database, and
  the operator stayed locked out.
- **Never creates a database.** If none exists at the expected path, it stops
  and says so.
- **No master password and no "turn login off" switch.** On this bridge an
  open UI lets anyone on the network command the battery. Resetting a
  password gets you in without that exposure.

## HA add-on

Under Home Assistant ingress you are signed in through Home Assistant, and the
bridge's own login is not used, so this lockout does not arise there. Running
the command inside an add-on container has not been tested yet.
