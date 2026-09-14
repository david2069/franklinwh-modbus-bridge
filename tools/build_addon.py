#!/usr/bin/env python3
"""Assemble a self-contained Home Assistant add-on folder.

Why this exists
---------------
The Supervisor builds an add-on with the **add-on directory** as the Docker
build context, so a Dockerfile there cannot ``COPY`` anything from the
repository root (``..`` is forbidden in COPY). Two obvious alternatives are
closed off here:

* **pip install from git** — the bridge repo is private, and the Supervisor's
  build has no credentials.
* **Manifest at the repo root** — ``settings.py`` already reads ``./config.yaml``
  as the *bridge's own* configuration, so an add-on manifest of the same name
  would be parsed as app config and break local dev runs.

So we assemble: copy the manifest and the source it needs into one directory
that is a valid add-on on its own. Copy that directory to your HA host's
``/addons/`` share and it installs as a local add-on.

Usage:
    python tools/build_addon.py [--out dist/addon] [--verify]

``--verify`` runs ``docker build`` on the result, which is the only way to know
the add-on actually builds rather than merely looking right.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (source, destination-relative-to-addon-root). Destinations are flat because
# the Dockerfile runs with the assembled folder as its context.
ITEMS: list[tuple[str, str]] = [
    ("addon/config.yaml", "config.yaml"),
    ("addon/Dockerfile", "Dockerfile"),
    ("addon/build.yaml", "build.yaml"),
    ("addon/run.sh", "run.sh"),
    ("constraints.txt", "constraints.txt"),
    ("pyproject.toml", "pyproject.toml"),
    ("src", "src"),
    ("README.md", "README.md"),
]

# Never ship build noise or a developer's local database into an add-on image.
EXCLUDE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.pyo", ".DS_Store", "*.db", "*.db-wal", "*.db-shm",
)


def assemble(out: Path) -> Path:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for src_rel, dst_rel in ITEMS:
        src = REPO / src_rel
        dst = out / dst_rel
        if not src.exists():
            raise SystemExit(f"missing required file: {src_rel}")
        if src.is_dir():
            shutil.copytree(src, dst, ignore=EXCLUDE)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        print(f"  {src_rel} -> {dst_rel}")

    (out / "run.sh").chmod(0o755)
    return out


def verify(out: Path) -> int:
    """Actually build it. An assembled folder that looks right but fails to
    build is worse than no add-on, because the failure surfaces on the user's
    HA box instead of here."""
    print("\nBuilding (this compiles a few wheels on Alpine; takes a while)…")
    proc = subprocess.run(
        [
            "docker", "build",
            "--build-arg",
            "BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.19",
            "-t", "fwhbridge-addon:verify",
            str(out),
        ],
        cwd=REPO,
    )
    if proc.returncode != 0:
        print("\nBUILD FAILED — the add-on would not install on HA either.")
        return proc.returncode
    print("\nBuild OK — this folder installs as a local add-on.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="dist/addon")
    ap.add_argument("--verify", action="store_true", help="docker build the result")
    args = ap.parse_args()

    out = (REPO / args.out).resolve()
    print(f"Assembling add-on in {out.relative_to(REPO)}/")
    assemble(out)

    if args.verify:
        return verify(out)

    print(
        "\nDone. Copy this folder to your HA host's /addons share, e.g.\n"
        f"  scp -r {out.relative_to(REPO)} root@homeassistant:/addons/franklinwh_modbus_bridge\n"
        "then Settings -> Add-ons -> Add-on Store -> ⋮ -> Check for updates.\n"
        "Re-run with --verify to docker build it first."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
