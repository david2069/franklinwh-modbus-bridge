"""Environment detection -- ha_addon | docker | dev."""

import os
from pathlib import Path
from typing import Literal

Environment = Literal["ha_addon", "docker", "dev"]


def detect_environment() -> Environment:
    """Detect runtime environment from env vars, filesystem markers, and cgroup info."""
    env = os.environ.get("APP_ENV")
    if env in ("ha_addon", "docker", "dev"):
        return env  # type: ignore[return-value]

    ha_indicators = ["SUPERVISOR_TOKEN", "HASSIO_TOKEN", "SUPERVISOR_API"]
    if any(os.environ.get(var) for var in ha_indicators):
        return "ha_addon"

    ha_paths = ["/data/options.json", "/var/run/secrets/hassio"]
    if any(Path(p).exists() for p in ha_paths):
        return "ha_addon"

    if Path("/.dockerenv").exists():
        return "docker"

    try:
        cgroup = Path("/proc/1/cgroup")
        if cgroup.exists():
            content = cgroup.read_text().lower()
            if any(x in content for x in ("docker", "containerd", "kubepods")):
                return "docker"
    except Exception:
        pass

    try:
        mountinfo = Path("/proc/self/mountinfo")
        if mountinfo.exists() and "docker" in mountinfo.read_text().lower():
            return "docker"
    except Exception:
        pass

    return "dev"


def get_data_dir() -> Path:
    """Return the data directory for the current environment."""
    env = detect_environment()
    if env in ("ha_addon", "docker"):
        return Path("/data")
    return Path("./data")
