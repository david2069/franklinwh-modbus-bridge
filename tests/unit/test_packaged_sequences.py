"""The example sequences ship in the installed package (owner, 2026-10-19).

pyproject's package-data listed only static/ and templates/, so the wheel the
add-on and Docker install had no sequences/*.json and the Sequencer started
empty: _get_sequences_dir() seeds the data dir from a directory that wasn't
there.
"""

from __future__ import annotations

import tomllib
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "src" / "franklinwh_bridge"


def test_every_bundled_sequence_is_package_data():
    globs = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"][
        "package-data"]["franklinwh_bridge"]
    files = [p.relative_to(PKG).as_posix() for p in (PKG / "sequences").glob("*.json")]
    assert len(files) >= 10
    missing = [f for f in files if not any(fnmatch(f, g) for g in globs)]
    assert missing == []
