"""The add-on's version, the package version and the add-on changelog agree.

Home Assistant shows "Update available" only when ``addon/config.yaml``'s
version goes up, and shows ``addon/CHANGELOG.md`` behind the Changelog link.
The add-on sat at 0.1.0 while months of changes shipped, so nobody was ever
offered them. This keeps a release from bumping one and forgetting the rest.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

import franklinwh_bridge

ROOT = Path(__file__).resolve().parents[2]
ADDON = ROOT / "addon"


def _addon_version() -> str:
    return str(yaml.safe_load((ADDON / "config.yaml").read_text())["version"])


def test_addon_and_package_versions_match():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert _addon_version() == pyproject["project"]["version"] == franklinwh_bridge.__version__


def test_addon_changelog_has_the_current_version_first():
    text = (ADDON / "CHANGELOG.md").read_text()
    versions = re.findall(r"^## (\d+\.\d+\.\d+)", text, flags=re.M)
    assert versions, "addon/CHANGELOG.md has no '## x.y.z' entries"
    assert versions[0] == _addon_version()


def test_store_page_files_exist():
    for name in ("DOCS.md", "CHANGELOG.md", "icon.png", "logo.png"):
        assert (ADDON / name).is_file(), f"addon/{name} missing"
    assert (ADDON / "icon.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
