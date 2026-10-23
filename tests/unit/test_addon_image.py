"""The prebuilt add-on image workflow builds what the add-on describes (#300).

``.github/workflows/publish-image.yml`` builds ``addon/`` for each arch and pushes
``ghcr.io/david2069/{arch}-franklinwh-modbus-bridge:<version>`` on ``v*`` tags, the
same way franklinwh-direct-connect-bridge publishes its image. Once
``addon/config.yaml`` names that image, the Supervisor pulls ``{image}:{version}``
instead of building, so a workflow that skips an arch or pushes another name is an
update that fails for every user on it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ADDON = ROOT / "addon"
WORKFLOW = ROOT / ".github" / "workflows" / "publish-image.yml"
IMAGE = "ghcr.io/david2069/{arch}-franklinwh-modbus-bridge"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers() -> dict:
    # PyYAML (YAML 1.1) reads the bare key `on:` as the boolean True.
    wf = _workflow()
    return wf.get("on", wf.get(True))


def _build_job() -> dict:
    return _workflow()["jobs"]["build"]


def _step(uses_prefix: str) -> dict:
    steps = [s for s in _build_job()["steps"] if str(s.get("uses", "")).startswith(uses_prefix)]
    assert len(steps) == 1, f"expected one {uses_prefix} step"
    return steps[0]


def test_workflow_exists():
    assert WORKFLOW.is_file()


def test_runs_on_version_tags_and_by_hand():
    on = _triggers()
    assert on["push"]["tags"] == ["v*"]
    assert "branches" not in on["push"], "an image is published from a tag, never a branch"
    assert "workflow_dispatch" in on


def test_builds_every_addon_arch_and_each_has_a_base():
    config_arches = yaml.safe_load((ADDON / "config.yaml").read_text())["arch"]
    built = [entry["arch"] for entry in _build_job()["strategy"]["matrix"]["include"]]
    bases = yaml.safe_load((ADDON / "build.yaml").read_text())["build_from"]
    assert sorted(built) == sorted(config_arches) == ["aarch64", "amd64"]
    assert set(built) <= set(bases)


def test_builds_from_the_addon_directory():
    build = _step("docker/build-push-action")["with"]
    assert build["context"] == "addon"
    assert build["file"] == "addon/Dockerfile"
    assert "BUILD_FROM=" in build["build-args"]


def test_pushes_the_image_name_tagged_with_the_addon_version():
    text = WORKFLOW.read_text()
    assert 'image="ghcr.io/david2069/${{ matrix.arch }}-franklinwh-modbus-bridge"' in text
    assert IMAGE.replace("{arch}", "${{ matrix.arch }}") in text
    build = _step("docker/build-push-action")["with"]
    assert build["tags"] == "${{ steps.meta.outputs.image }}:${{ steps.meta.outputs.version }}"
    assert "addon/config.yaml" in text


def test_pushes_to_ghcr_with_package_write_only():
    assert _workflow()["permissions"] == {"contents": "read", "packages": "write"}
    assert _step("docker/login-action")["with"]["registry"] == "ghcr.io"


def test_a_published_tag_is_never_overwritten():
    text = WORKFLOW.read_text()
    assert "docker manifest inspect" in text and "already exists" in text


def test_addon_names_no_image_but_the_published_one():
    """``image:`` is added in a release after the first image is public (until then the
    Supervisor would pull a tag that doesn't exist). When it is, it must be this name."""
    config = yaml.safe_load((ADDON / "config.yaml").read_text())
    assert "image" not in config or config["image"] == IMAGE
