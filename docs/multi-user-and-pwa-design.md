# Modularisation, Multi-User, Roles & PWA — Design

**Status:** **PRIORITISED (owner, 2026-08-12)** — above the unified action pipeline /
RateRudder / pricing polish. Scoped 2026-08-08, extended 2026-08-12 with a
**modularisation layer** (below) and a re-sequenced roadmap. Not built yet.

**One-line goal:** make every feature an **optional, capability-gated module**
(admin can turn scheduling / HA Entities / RateRudder / etc. on or off), add real
user accounts + roles (admin / user / viewer) with a simplified, mobile-first,
view-only household dashboard, and deliver both dashboards as installable PWAs —
with vetted auth libraries behind mandatory TLS, without breaking open
deployments or double-authenticating under Home Assistant ingress.

**Since first scoped (2026-08-08), a lot shipped** — HA Entities/multi-HA,
Scheduler v2 (dwell, HA actions v1/v2 + guard, Value|Lookup, verification),
persistent logs — so the schema is now **v31** (the users table below becomes
migration **v32**, superseding the "v27" references further down). The
modularisation layer (§M) is **Phase 0** and ships first, standalone, before auth.

---

## M. Modularisation — capability-gated module registry (Phase 0, ships FIRST)

**Decisions (owner, 2026-08-12):** gating is **BOTH** a global admin enable-flag
AND a role capability; build the registry **standalone first** (before auth), so
"turn features off" ships immediately and roles slot in at Phase 2 with no rework.

Every feature is a **module**: `{ id, label, icon, routers[], required_capability,
enabled (global) }`. A module's nav item + routes are available **iff** `enabled`
(admin's global switch) **AND** the principal holds `required_capability`. Before
auth (Phase 0) there is a single implicit **admin** principal with all
capabilities, so only the global `enabled` flags apply — delivering feature
on/off now; roles slot in at Phase 2 unchanged.

**Module inventory (initial):**

| Module | id | capability | default |
|---|---|---|---|
| Dashboard | `dashboard` | `view` | on (core, not disableable) |
| Settings + Gateways | `settings` | `settings` | on (admin) |
| Automations (Scheduler v2 incl. HA actions + future unified pipeline) | `automations` | `automations` | on |
| HA Entities (multi-HA) | `ha_entities` | `ha_entities` | on |
| Sequencer | `sequencer` | `sequencer` | on |
| SunSpec Explorer | `explorer` | `explorer` | on |
| Logs | `logs` | `logs` | on |
| Terminal (backlog) | `terminal` | `terminal` | off |
| RateRudder (backlog) | `raterudder` | `raterudder` | off |
| Pricing (backlog) | `pricing` | `pricing` | off |

- **Store:** module enabled-flags in `app_config` (JSON) — `get_app_config`/
  `set_app_config` already exist; **no new table** for Phase 0. The capability map
  lives in code (single source, shared with §3 roles).
- **Backend:** `api/modules.py` — `GET /api/modules` (each module + `enabled` +
  whether the caller may access) and `PATCH /api/modules/{id}` (admin toggles
  `enabled`). A dependency `require_module(id)` = `enabled AND capability`, applied
  to each feature router in `main.py` (composes with the later `require_role`).
  Pre-auth, `require_module` checks only `enabled`.
- **Frontend:** `$store.app` loads `/api/modules` (and later `/api/auth/me`) into
  `modules` + `caps`, with a `canSee(id)` helper. Sidebar nav buttons and tab
  `x-show` gate on `canSee('automations')` etc. An admin **Settings → Modules**
  panel toggles the enabled flags.
- **The unified action pipeline is NOT its own module** — it lives inside
  `automations`; disabling that module hides the whole builder incl. the pipeline.
  **RateRudder / pricing** are their own off-by-default modules. This is exactly
  how the "some users won't want scheduling / HA / RateRudder" requirement is met.

This registry is the substrate roles plug into: role→capabilities sets `caps`;
module `enabled` is orthogonal.

---

## 1. Current state (verified in-repo)

- **Auth: none.** All routes are open on `0.0.0.0:8100`. `AdminSettings.username/
  password` (`config/settings.py`) are **dead code** — referenced nowhere. No
  users / roles / sessions tables, no middleware, no `secret_key`, no CORS/CSRF.
- **UI:** one Alpine.js SPA — every tab inlined into `templates/index.html` and
  toggled by `$store.app.activeTab` (no router); served by `api/ui.py` `GET /`.
  Assets vendored locally under `static/` (tailwind/alpine/chartjs + per-tab JS +
  `css/design-system.css`). Desktop-oriented; the sidebar does not auto-collapse;
  only one responsive media query exists.
- **PWA:** nothing — no manifest, service worker, icons, or `theme-color`. `<head>`
  has charset + viewport + `<base href="{{ base_path }}/">` (HA ingress) only.
- **Foundations (friendly):** DB migration pattern is clean (`store/db.py`,
  `MIGRATIONS` dict, `CURRENT_SCHEMA_VERSION` currently **26** → next is **27**);
  follow the `services` CRUD template + a v8-style post-migration hook to seed
  data that needs Python (password hashing). `pyproject.toml` already has
  `python-multipart` (login forms) but **no auth libraries**. `main.py` registers
  **no middleware** — the slot for `SessionMiddleware` is right after app creation.
- **HA ingress:** under `env == ha_addon`, Supervisor already authenticates the
  user (`api/ui.py` reads `X-Ingress-Path` only). App-level auth must **bypass**
  there and enforce only for `docker` / `dev`.

## 2. Locked decisions (owner, 2026-08-08)

1. **Non-admin users are VIEW-ONLY today** — simplified dashboard, no control.
2. **Three roles: admin / user / viewer**, implemented via a **role→capability
   map** so `user` can be granted control later with a one-line map change (no
   migration). Today: `viewer` = `user` = `[view_dashboard]`; `admin` = all.
3. **Auth rollout = seed an admin + ENFORCE** (from `ADMIN_USERNAME/PASSWORD` env,
   else generate a random password and log it once) — secure by default for
   docker/dev; no lockout. Bypass under HA ingress.
4. **PWA: the `/user` simplified dashboard is the PRIMARY installable PWA**; the
   admin dashboard is installable but secondary.
5. **Vetted libraries, NOT a custom Security Module.** (Explicit lesson from the
   sibling FWHAI project, which hand-rolled its own — treated as a mistake.) Use
   Starlette **`SessionMiddleware`** (itsdangerous-signed cookies) + **`argon2-cffi`**
   (argon2id) for hashing. `fastapi-users` / Authlib considered but overkill for
   3 fixed roles / no self-registration — adopt only if password-reset / OAuth /
   many-users appear.
6. **TLS is mandatory, terminated at the EDGE — the app never does TLS itself.**
   See §5.

## 3. Auth architecture

- **Primitives (no bespoke crypto):** `argon2-cffi` for `password_hash`;
  `SessionMiddleware` for signed session cookies; a generated `secret_key`
  persisted in the `app_config` key/value table when not provided via env.
- **`api/auth.py`** (`auth_router`, included before `ui_router`):
  `POST /api/auth/login` (form or JSON → verify → set session cookie),
  `POST /api/auth/logout`, `GET /api/auth/me`.
- **Dependencies:** `get_current_user(request)` (reads `request.app.state.db` +
  session — matches the existing "handlers pull `app.state.db` directly" pattern;
  there is no DI layer today) and `require_role(*roles)`. Applied to routers in
  `main.py`. A **role→capability** map (`viewer/user = {view}`,
  `admin = {view, control, schedules, settings, users}`) is the single place
  permissions live.
- **Ingress bypass:** when `detect_environment() == "ha_addon"`, `get_current_user`
  returns a synthetic admin (trust Supervisor) — no double auth.
- **Seeding:** migration **v27** post-hook creates the first admin (env creds or a
  generated password logged once). No open window for docker/dev.
- **Hardening:** cookies `HttpOnly` + `SameSite=Lax` (+ `Secure` when HTTPS, see
  §5); same-origin CORS; login rate-limit; CSRF handled by `SameSite=Lax` plus a
  custom-header requirement on cookie-authenticated state-changing requests.

## 4. Transport security (TLS) — §6 decision detail

The app stays a **plain-HTTP backend**; TLS terminates at the edge:

| Deployment | TLS + auth |
|---|---|
| **HA addon** | Free — HA ingress provides TLS **and** authenticates the user; app bypasses its own auth. Likely the majority path. |
| **Docker / LAN** (the PWA case) | Front with **Caddy** (auto Let's Encrypt with a domain, or its internal CA for LAN). Ship a `docker-compose` "with-tls" profile. |
| **Remote access** | **Tailscale** (`tailscale serve`) — TLS + identity, no public exposure. |

App-level enforcement so "mandatory" is real:
- Run uvicorn with `--proxy-headers`; honor **`X-Forwarded-Proto`**; set the
  session cookie **`Secure`** flag when the request is HTTPS; emit **HSTS** when
  TLS is on; trust proxy headers **only from the known proxy**.
- **Refuse to accept a login over plain HTTP** unless `ALLOW_INSECURE_AUTH=1`
  (a deliberate LAN-only override). This makes HTTPS the default requirement
  without hard-blocking an intentional LAN setup.

## 5. Simplified user dashboard

- New route `GET /user` → `templates/user.html`, its **own mobile-first shell**
  (not the admin sidebar/topbar chrome), reusing `/static` + the existing JSON
  APIs (`api/points`, `api/health`, `api/gateways`).
- Content: SOC ring, power flow (solar/grid/load/battery), operating mode, today's
  energy, next-fire/schedule status, connectivity badge. **No** SunSpec explorer,
  sequencer, raw points, or settings.
- Role-gated: `user`/`viewer` land here on login; `admin` may view it too. Login
  redirects by role (`admin` → `/`, `user`/`viewer` → `/user`).
- Admin gains a **Users** management tab (CRUD users, set roles, reset password) —
  admin capability only.

## 6. PWA

- **Assets:** `static/icons/` (192 + 512 + maskable + apple-touch) generated from
  one source icon.
- **`/user` shell head:** `<link rel="manifest">`, `theme-color` (from the dark
  shell), `apple-touch-icon`, `apple-mobile-web-app-capable`.
- **Served routes** (not just `/static`, so scope covers the app root):
  `GET /user.webmanifest` (name, `start_url` respecting `X-Ingress-Path`,
  `display: standalone`, scope, icons) and `GET /sw.js` at **root scope**.
- **Service worker:** precache the app shell + vendored static; **network-first**
  for `/api/*`; offline app-shell fallback. Register in `user_app.js`.
- **Ingress caveat:** manifest `start_url`/`scope` and SW registration must use the
  `base_path` from `X-Ingress-Path`. Admin manifest is a later, optional add.

## 7. Data model

Migration **v27** (`store/db.py`), bump `CURRENT_SCHEMA_VERSION` to 27:

```sql
CREATE TABLE users (
    id            TEXT PRIMARY KEY,          -- user_<uuid8>
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,             -- argon2id
    role          TEXT NOT NULL DEFAULT 'viewer'
                    CHECK (role IN ('admin','user','viewer')),
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL DEFAULT 0,
    updated_at    REAL NOT NULL DEFAULT 0,
    last_login_at REAL
);
```

CRUD (`_USER_FIELDS` allowlist + `create/get/get_by_username/update/delete_user`)
mirrors the compact `services` template. Sessions ride signed cookies (no table)
unless server-side revocation is later required, in which case add a `sessions`
table. `secret_key` lives in `app_config` (generated) or `SECURITY_SECRET_KEY` env.

## 8. Phasing (re-sequenced 2026-08-12 — modules first)

0. **Phase 0 — Module registry + capability gating** (§M, ships FIRST, standalone,
   no auth): module inventory + capability map; `app_config` enabled-flags;
   `api/modules.py` (`GET /api/modules`, `PATCH /api/modules/{id}`);
   `require_module(id)` on each feature router; `$store.app` `modules`/`caps` +
   `canSee()` gating the sidebar/tabs; admin **Settings → Modules** toggle panel.
   Delivers "turn features off" immediately; single implicit admin until Phase 1.
1. **Phase 1 — Auth core** (was A1; largest / highest risk): deps (`argon2-cffi`,
   `itsdangerous`); migration **v32** + user CRUD + admin seed; `secret_key`;
   `api/auth.py` + `get_current_user`/`require_role`; `main.py`
   `SessionMiddleware` + `--proxy-headers`/forwarded-proto + HSTS + router gating
   (compose with `require_module`) + ingress bypass + `ALLOW_INSECURE_AUTH` guard;
   `ui.py` `/login`. Ships enforcing (docker/dev), seeded (no lockout).
2. **Phase 2 — Roles + dashboards** (was A2): role→capability map wired to the
   module caps; admin Users tab; `GET /user` mobile-first shell + trimmed
   `user_app.js` showing only permitted modules; role-based login redirect.
3. **Phase 3 — PWA** (was C): icons + manifest + service worker for `/user`
   (ingress-aware), registration, offline shell. Admin manifest optional later.

Then (unchanged priority order, AFTER the above): unified action pipeline,
RateRudder, pricing — each a gated module per §M. Each phase is independently
shippable; rebuild + Playwright-verify per the deploy workflow.

## 9. Risks & back-compat

- **Greenfield security** — keep to libraries (decision #5); do not build a custom
  Security Module.
- **No TLS in-repo** — enforced at the edge (decision #6); app makes HTTPS
  mandatory via forwarded-proto + the `ALLOW_INSECURE_AUTH` override.
- **Lockout** — avoided by seeding an admin at migration time.
- **HA addon** deployments unaffected (auth bypassed under ingress).
- **Existing tests** hit routes unauthenticated — add an authenticated
  test-client fixture (or a test bypass) so the suite keeps passing once gating is
  on.

## 10. Verification

- **Unit:** password hash/verify, `require_role` gating matrix, user CRUD, admin
  seed, `ALLOW_INSECURE_AUTH`/forwarded-proto logic.
- **Integration:** login → gated route returns 401 unauth / 200 with role; ingress
  bypass path; logout invalidates.
- **Browser (Playwright, per project harness):** login page, mobile `/user`
  dashboard, PWA installability (manifest + SW registration), console-clean.
- Rebuild + deploy; confirm container serves the committed code.

## 11. Open questions

- Reconcile against **FWHAI's custom Security Module** (read-only) before A1 — to
  reach parity or deliberately avoid its pitfalls. (Owner deferred this read for
  now.)
- Password-reset / account-recovery flow (out of scope for view-only v1?).
- Whether `viewer` vs `user` should differ at all in v1 (identical today; the
  distinction is reserved for the capability map).
- Multi-tenant / per-user data ownership — **out of scope**; today `gateway_id`
  is a device concept, not a person.
