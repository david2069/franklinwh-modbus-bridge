# Build & Dependency Policy

How `franklinwh-modbus-bridge` pins, builds against, and stays in sync with the
`franklinwh-modbus` library — and how that ties into PyPI releases.

---

## 1. Library sourcing — two modes

The bridge depends on `franklinwh-modbus`. It is sourced one of two ways:

| Mode | When | How |
|---|---|---|
| **Git ref (pinned)** | The bridge needs library changes that are **not yet released** to PyPI (the normal case during co-development). | `Dockerfile` installs `…@git+https://github.com/david2069/franklinwh-modbus.git@${FWM_REF}` where `FWM_REF` is an **immutable commit SHA or tag**. |
| **PyPI version (pinned)** | The needed changes are in a **published PyPI release**. | `pyproject.toml` / `requirements.txt` pin `franklinwh-modbus==X.Y.Z`, and the Dockerfile installs from PyPI. |

**Rule: always pin an immutable ref — never an unpinned branch.** A branch
(`develop`) is non-reproducible and, because Docker caches the `RUN pip install`
layer by instruction text, a plain `docker compose build` would silently reuse a
stale library and never re-clone.

### Current state (2026-06)
- The bridge is in **git-ref mode**, pinned to `FWM_REF=eb698258…` (`develop`
  HEAD) — it carries the `Union` import fix + the `uint32`/enum/`address`
  sequencer features + the standby handshake, **none of which are on PyPI yet**.
- PyPI's latest (`0.9.2`) is clean and imports fine, but **predates those
  features** — so we cannot use PyPI mode until a new release ships them.

---

## 2. The `FWM_REF` mechanism

```dockerfile
ARG FWM_REF=<commit-sha-or-tag>
RUN pip install --no-cache-dir pyserial \
    "franklinwh-modbus @ git+https://github.com/david2069/franklinwh-modbus.git@${FWM_REF}"
```

- **Reproducible** — the same `FWM_REF` always installs the same code.
- **Cache-busting** — `FWM_REF` is part of the `RUN` command, so bumping it
  invalidates the layer; an upgrade is picked up by a plain
  `docker compose build` **without** `--no-cache`.
- **Upgrade** — edit `FWM_REF` in the `Dockerfile` (tracked, one line), or for a
  one-off: `docker compose build --build-arg FWM_REF=<tag-or-sha> app`.
- **Prefer a release tag** (`v0.9.3`) over a raw SHA once a release is cut —
  self-documenting and tied to a published artifact.

---

## 3. Python version constraint

**The deployment target is Python 3.12** (the container base, and CI's matrix).
Validate the library on 3.12, not just the dev machine's interpreter.

> Why this matters: Python 3.14 evaluates annotations lazily (PEP 649), so a
> missing-import bug in a type annotation (e.g. the `Union` defect) passes
> locally on 3.14 but fails at import on ≤3.13. **Always smoke-test
> `import franklinwh_modbus` on 3.12 before bumping `FWM_REF`.**

---

## 4. Staying in sync with PyPI

The library publishes via **PyPI Trusted Publishing (OIDC)** from
`.github/workflows/publish.yml`, triggered by a `v*.*.*` tag.

### Release → bridge-bump flow
1. **Library repo:** bump the version, then push a tag `vX.Y.Z`.
2. CI (`publish.yml`) builds on 3.12 and publishes to PyPI via the trusted
   publisher.
3. **Bridge repo:** switch to PyPI mode (or set `FWM_REF=vX.Y.Z`) and pin
   `franklinwh-modbus==X.Y.Z`. Rebuild, smoke-test on 3.12.

### "Are we in sync with PyPI?" — checklist
- [ ] The features the bridge relies on exist in a **published** PyPI release
      (`curl -s https://pypi.org/pypi/franklinwh-modbus/json | jq .releases`).
- [ ] The bridge's pin (`FWM_REF` or `==X.Y.Z`) points at that release.
- [ ] `import franklinwh_modbus` succeeds on **Python 3.12**.
- [ ] The installed code actually contains the features (don't trust
      `__version__` — see §6).

---

## 5. Trusted-publisher re-enrolment (currently lost)

PyPI Trusted Publishing was lost and must be re-enrolled before any new release
can be published. It is a **PyPI web action** (not a repo change):

1. Sign in to **PyPI** → project **`franklinwh-modbus`** → **Settings → Publishing**.
2. **Add a trusted publisher** (GitHub Actions) with **exactly**:
   - **Owner:** `david2069`
   - **Repository:** `franklinwh-modbus`
   - **Workflow name:** `publish.yml`
   - **Environment:** `pypi`  *(the prod job; the optional TestPyPI job uses
     environment `testpypi` on test.pypi.org and must be re-enrolled there too
     if used)*
3. Confirm the GitHub repo has the matching **environments** (`pypi`,
   optionally `testpypi`) under Settings → Environments.
4. Re-run `publish.yml` (push a `v*` tag or use the manual `workflow_dispatch`)
   and confirm the upload succeeds.

Until this is done, releases can only be sourced via **git ref** (current mode).

---

## 6. Open library-side issues to fix before the next release

These live in `franklinwh-modbus` (handle in that repo's own session):

- **Version metadata is inconsistent and stale.** `pyproject.toml` says `0.9.2`
  while `src/franklinwh_modbus/__init__.py` `__version__` says `0.9.0`; the
  published PyPI `0.9.2` also reports `__version__ == '0.9.0'`. `develop`
  additionally carries unreleased features beyond `0.9.2`. **Bump both to the
  next version (e.g. `0.9.3`) and keep them in sync** (ideally derive
  `__version__` from package metadata so it can't drift). Until fixed,
  `__version__` cannot be used to tell what's installed.
- **The next release must include the `Union` fix** (commit `789ee19`) so PyPI
  installs work on Python ≤ 3.13.

---

## 7. TL;DR
- Pin the library to an **immutable ref** (`FWM_REF` SHA/tag) — never a branch.
- Bumping `FWM_REF` busts the cache; no `--no-cache` needed.
- Smoke-test `import franklinwh_modbus` on **Python 3.12** before bumping.
- To use **PyPI** instead of git: re-enrol the trusted publisher, fix the
  version metadata, cut a `vX.Y.Z` release, then pin `==X.Y.Z`.
