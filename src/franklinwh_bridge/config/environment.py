"""Environment detection -- ha_addon | docker | dev."""

import ipaddress
import os
import socket
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


# ── LAN subnets to search for an aGate (setup wizard) ────────────────────────
#
# Where the LAN is depends on how the bridge runs, so it is decided here — the
# one place the bridge branches on add-on / docker / dev.
#
# * ha_addon: the container sits on HA's internal network; its own interfaces
#   say nothing about the LAN. The Supervisor knows the host's interfaces.
# * docker (bridge networking): the container's interfaces are Docker's. The
#   address the browser used to reach the bridge is the Docker host's LAN
#   address (the published port lands there), so its /24 is the best guess;
#   then LAN_SUBNET; then the container's own interface, which is the LAN only
#   with host networking.
# * dev / bare metal: this machine's own interface.

#: A scan covers at most one /24 per subnet; wider prefixes are narrowed to the
#: /24 that contains the address.
SCAN_PREFIX = 24

#: Docker's default address pools. A container interface in these is Docker's
#: own bridge network, not the LAN.
_DOCKER_POOLS = (ipaddress.ip_network("172.16.0.0/12"),)


def lan_subnet_for(address: str, prefix: int = SCAN_PREFIX) -> str | None:
    """The scan subnet (/24) containing ``address``, if it is a private IPv4 LAN address.

    Loopback, link-local, Tailscale (100.64.0.0/10, not "private") and public
    addresses give None: none of them identifies a LAN to search.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None
    if ip.version != 4 or ip.is_loopback or ip.is_link_local or not ip.is_private:
        return None
    return str(ipaddress.ip_network(f"{ip}/{max(prefix, SCAN_PREFIX)}", strict=False))


def _primary_ipv4() -> str | None:
    """This host's outbound IPv4 address (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: routing lookup only
            return s.getsockname()[0]
    except OSError:
        return None


def _host_from_header(host_header: str | None) -> str | None:
    """The address part of a Host header, resolved if it is a name."""
    if not host_header:
        return None
    host = host_header.strip()
    if host.startswith("["):  # IPv6 literal — not scannable
        return None
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if host in ("localhost", ""):
        return None
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


async def _supervisor_subnets() -> list[str]:
    """IPv4 subnets of the HA host's interfaces, from the Supervisor."""
    from franklinwh_bridge.config.supervisor import supervisor_network_info

    info = await supervisor_network_info()
    out: list[str] = []
    for iface in (info or {}).get("interfaces", []):
        if not iface.get("enabled", True) or not iface.get("connected", True):
            continue
        for addr in ((iface.get("ipv4") or {}).get("address") or []):
            ip, _, plen = str(addr).partition("/")
            subnet = lan_subnet_for(ip, int(plen) if plen.isdigit() else SCAN_PREFIX)
            if subnet and subnet not in out:
                out.append(subnet)
    return out


async def candidate_subnets(host_header: str | None = None) -> list[dict]:
    """Subnets to offer for an aGate search, each ``{subnet, source}``.

    ``host_header`` is the Host the browser used to reach the bridge. Empty
    list means no LAN could be identified — the wizard then offers "Enter
    address" first.
    """
    env = detect_environment()
    found: list[dict] = []

    def add(subnet: str | None, source: str) -> None:
        if subnet and all(f["subnet"] != subnet for f in found):
            found.append({"subnet": subnet, "source": source})

    if env == "ha_addon":
        for subnet in await _supervisor_subnets():
            add(subnet, "supervisor")
        return found

    if env == "docker":
        browser_ip = _host_from_header(host_header)
        add(lan_subnet_for(browser_ip) if browser_ip else None, "browser_address")
        for raw in (os.environ.get("LAN_SUBNET") or "").split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                net = ipaddress.ip_network(raw, strict=False)
            except ValueError:
                continue
            add(lan_subnet_for(str(net.network_address), net.prefixlen), "lan_subnet_env")
        own = _primary_ipv4()
        if own and not any(ipaddress.ip_address(own) in pool for pool in _DOCKER_POOLS):
            add(lan_subnet_for(own), "host_network")
        return found

    own = _primary_ipv4()
    add(lan_subnet_for(own) if own else None, "this_machine")
    return found
