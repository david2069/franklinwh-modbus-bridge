# Security guide

How the bridge controls who can see and command your battery, what it records,
and what to change when you set it up. Written for the person running the
bridge.

> Anyone with a working **admin** or **user** login can force your battery to
> charge or discharge. Treat these logins like the FranklinWH app login.

## 1. Accounts and roles

| Role | Sees | Can command the battery | Manages users |
|---|---|---|---|
| **admin** | Everything (full UI) | Yes | Yes |
| **user** | The mobile dashboard (`/user`) | Yes (charge/discharge) | No |
| **viewer** | The mobile dashboard, read-only | No | No |

- Manage accounts under **Settings → Admin → Users**.
- **Usernames ignore case:** "Admin" and "admin" are the same account, and the
  bridge refuses a new name that differs from an existing one only in case.
- **The last enabled admin can't be deleted, disabled or demoted**, so the UI
  can't lock itself out. If it happens anyway, see §6.
- **Passwords** are stored as argon2id hashes, never in plain text.

### The first admin

It is created on the first start from `ADMIN_USERNAME` / `ADMIN_PASSWORD`.

- **The shipped `docker-compose.yml` defaults these to `admin` / `admin`.**
  Either set them in `.env` before the first start, or change the password
  straight after you sign in.
- With `ADMIN_PASSWORD` unset, a random password is generated and printed to the
  log once: `docker compose logs | grep -i password`.

## 2. Logging in over HTTP or HTTPS

| Setting | Effect |
|---|---|
| *(default)* | Login is refused over plain HTTP; it requires HTTPS. |
| `ALLOW_INSECURE_AUTH=1` | Allows login over plain HTTP. **This does not bypass the login.** The shipped compose sets it for LAN use. |
| `SESSION_COOKIE_SECURE=1` | The login cookie is only sent over HTTPS. Set it when the bridge sits behind TLS. |

Plain HTTP on a home LAN means the password and login cookie cross the network
unencrypted. For access from outside the home, put the bridge behind TLS (for
example Caddy or `tailscale serve`), remove `ALLOW_INSECURE_AUTH` and set
`SESSION_COOKIE_SECURE=1`. Don't forward its port to the internet.

## 3. How a login works and ends

- **The login cookie:**
  - holds a signed record of your login;
  - lasts **14 days from your last use**, renewed on every visit.
- **The signing key:**
  - comes from `SECURITY_SECRET_KEY` if set, otherwise from
    `/data/.session_secret`, created on first start and kept across restarts;
  - if it is changed or deleted, every login ends.
- **The login record:** each login has one, holding who, when, the IP,
  the browser and when it was last active. A login only works while its record
  is open.

When a login ends, the page shows **"You have been logged out"** with the
reason. If you reopen the bridge later, the login page shows the same reason.

| Reason shown | Cause |
|---|---|
| You logged out. | The Log out menu |
| Your account was disabled / deleted by an administrator. | Settings → Admin → Users |
| Your password was changed… | You changed it on another device, an admin reset it, or break-glass recovery did |
| An administrator ended this login. | An admin revoked it (see §4) |
| Your login expired after 14 days without use. | Cookie lifetime |
| This browser stopped sending its login… | Cleared site data, private browsing ended, or the browser dropped the cookie |
| This browser has no saved login for this address… | A different address (IP vs hostname, VPN), or a new browser or device |

The bridge, its schedules and battery commands keep running when a page is
logged out. Only that page loses access.

## 4. Audit trail

These are recorded with the time, IP and browser:

- every login;
- every failed login, with why: no such user, wrong password or account disabled;
- every logout;
- every login ended, with the reason and who ended it;
- every break-glass reset.

They also appear in the **Logs** tab, where failed logins are WARNINGs.

| REST (admin only) | |
|---|---|
| `GET /api/auth/events?limit=200` | The audit trail, newest first |
| `GET /api/auth/sessions?active=true` | Who is logged in now |
| `POST /api/auth/sessions/{id}/revoke` | End one login immediately |

There is no Users-page screen for these yet. Use the API, or read the Logs tab.

## 5. Home Assistant add-on

Under HA ingress, Home Assistant has already authenticated you and the bridge
does not ask again. Everyone who can open the add-on panel in HA is an admin of
the bridge, so control access through HA's own users.

## 6. Locked out

See [admin-recovery.md](admin-recovery.md):

```bash
docker exec -it fwhbridge-app bridge recovery status
docker exec -it fwhbridge-app bridge recovery reset-admin-password
```

There is no master password and no switch that turns login off.

## 7. Known gaps

These are not implemented yet, although the design (multi-user-and-pwa-design.md §3–4) planned them:

- **No limit on login attempts.** Failed logins are recorded, not throttled.
- **No CSRF header check.** Cookie writes rely on `SameSite=Lax` only.
- **No HSTS header** when served over TLS.
- **Self-service password change has no Settings page yet.** It is in the
  setup wizard's "Your login" step and at `POST /api/auth/password` (#54).
  Otherwise an admin changes it under Settings → Admin → Users.
- **No UI for sessions and the audit trail** (REST only).
- **Audit rows have no retention limit.** They are small, but they grow.

## Setup checklist

1. Set `ADMIN_PASSWORD` in `.env` before the first start, or change `admin` /
   `admin` immediately after. The setup wizard's "Your login" step flags a
   default password and changes it in place.
2. Give each person their own account with the lowest role that works
   (viewer, then user, then admin).
3. Keep the bridge off the internet; use TLS or Tailscale for remote access.
4. Note where [admin-recovery.md](admin-recovery.md) is, before you need it.
