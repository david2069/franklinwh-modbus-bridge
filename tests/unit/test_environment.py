"""Tests for environment detection."""

import os
from unittest.mock import patch

from franklinwh_bridge.config.environment import detect_environment, get_data_dir


def test_detect_dev_on_bare_metal():
    with patch.dict(os.environ, {}, clear=True):
        assert detect_environment() == "dev"


def test_detect_from_app_env_var():
    for env in ("ha_addon", "docker", "dev"):
        with patch.dict(os.environ, {"APP_ENV": env}, clear=True):
            assert detect_environment() == env


def test_detect_ha_addon_from_supervisor_token():
    with patch.dict(os.environ, {"SUPERVISOR_TOKEN": "abc123"}, clear=True):
        assert detect_environment() == "ha_addon"


def test_detect_ha_addon_from_hassio_token():
    with patch.dict(os.environ, {"HASSIO_TOKEN": "abc123"}, clear=True):
        assert detect_environment() == "ha_addon"


def test_detect_ha_addon_from_options_json(tmp_path):
    options = tmp_path / "options.json"
    options.write_text("{}")
    with patch.dict(os.environ, {}, clear=True), patch(
        "franklinwh_bridge.config.environment.Path"
    ) as mock_path:

        class FakePath:
            def __init__(self, p):
                self._p = str(p)

            def exists(self):
                return self._p == "/data/options.json"

            def read_text(self):
                return ""

        mock_path.side_effect = FakePath
        assert detect_environment() == "ha_addon"


def test_data_dir_dev():
    with patch("franklinwh_bridge.config.environment.detect_environment", return_value="dev"):
        assert str(get_data_dir()) == "data"


def test_data_dir_docker():
    with patch("franklinwh_bridge.config.environment.detect_environment", return_value="docker"):
        assert str(get_data_dir()) == "/data"


def test_data_dir_ha_addon():
    with patch(
        "franklinwh_bridge.config.environment.detect_environment", return_value="ha_addon"
    ):
        assert str(get_data_dir()) == "/data"


def test_podman_container_detected(monkeypatch):
    """Podman writes /run/.containerenv, not /.dockerenv (#179)."""
    from franklinwh_bridge.config import environment

    monkeypatch.setattr(environment, "_CONTAINER_MARKERS", ("/nonexistent", __file__))
    with patch.dict(os.environ, {}, clear=True):
        assert environment.in_container()
        assert detect_environment() == "docker"
        assert str(get_data_dir()) == "/data"


def _host_files(files: dict[str, str]):
    """Path stand-in: only `files` exist, with that text."""

    class FakePath:
        def __init__(self, p):
            self._p = str(p)

        def exists(self):
            return self._p in files

        def read_text(self):
            return files[self._p]

    return FakePath


def test_pip_install_on_a_host_running_containers_is_dev():
    """#413: a host running Docker containers lists their overlay mounts in its
    own /proc/self/mountinfo. A pip-installed bridge took that for "inside
    Docker", chose /data and failed to start (PermissionError)."""
    host = {
        "/proc/1/cgroup": "0::/init.scope\n",
        "/proc/self/mountinfo": "1234 29 0:52 / /var/lib/docker/overlay2/abc/merged rw "
                                "shared:300 - overlay overlay rw\n",
    }
    with patch.dict(os.environ, {}, clear=True), patch(
        "franklinwh_bridge.config.environment.Path", side_effect=_host_files(host)
    ):
        assert detect_environment() == "dev"


def test_docker_still_detected_from_pid1_cgroup():
    """cgroup v1 containers without /.dockerenv still name docker in PID 1's cgroup."""
    container = {"/proc/1/cgroup": "12:pids:/docker/0123abcd\n"}
    with patch.dict(os.environ, {}, clear=True), patch(
        "franklinwh_bridge.config.environment.Path", side_effect=_host_files(container)
    ):
        assert detect_environment() == "docker"
