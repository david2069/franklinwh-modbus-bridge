"""The README badge row points at real workflows and facts."""

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _head() -> str:
    return (ROOT / "README.md").read_text().split("\n## ", 1)[0]


def test_the_badges_point_at_real_workflows_and_facts():
    head = _head()
    badges = re.findall(r"\[!\[[^\]]*\]\((https://img\.shields\.io/[^)]+)\)\]", head)
    assert len(badges) == 8, badges
    wf = set(re.findall(r"actions/workflows/([\w.-]+\.yml)", head))
    assert wf == {p.name for p in (ROOT / ".github/workflows").glob("*.yml")}
    py = re.search(r'requires-python\s*=\s*">=([\d.]+)"', (ROOT / "pyproject.toml").read_text())[1]
    assert f"python-{py}%2B" in head
    assert "license-MIT" in head and "MIT License" in (ROOT / "LICENSE").read_text()
    # The last GitHub Release is 0.2.14: a release badge would show a stale version.
    assert "github/v/release" not in head


def test_the_installs_badge_counts_this_add_on():
    slug = re.search(r'^slug:\s*"?([\w-]+)', (ROOT / "addon/config.yaml").read_text(), re.M)[1]
    m = re.search(
        r"analytics\.home-assistant\.io%2Faddons\.json&query=%24%5B%27(\w+)%27%5D\.total", _head()
    )
    assert m and m[1].split("_", 1)[1] == slug  # <repository hash>_<slug>
