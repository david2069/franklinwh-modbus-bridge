"""Remove this bridge's own stale Home Assistant leftovers, automatically.

Older versions left things behind that users had to delete by hand:

- **Retained discovery configs** on the broker for entities the bridge no longer
  publishes (an entity that changed type, a renamed or removed one, or a config
  from a much older release). Each keeps an old device alive in HA.
- **Empty old devices** in Home Assistant after 0.2.4 moved the entities to the
  new per-integration identifier (``<prefix>_modbus_<SERIAL>``). The entities
  moved; the old device was left with nothing in it.

Only ever our own, decided conservatively:

- broker: the config's node id is ``<prefix>_<short_id>_…`` for one of OUR
  gateways, it is not currently published, and its device says it came from this
  bridge (``sw_version`` "Modbus Bridge …", or the v0.1.0 form "… (bridge: v…)").
  Another producer's config (FWHAI "FHAI: …") on an overlapping node is left.
- Home Assistant: the device carries exactly one of OUR old identifiers
  (``<prefix>_<SERIAL>`` or ``<prefix>_<gateway id>_<SERIAL>``) and has NO
  entities. A merged device still holding another integration's entities is
  never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from typing import Any

import aiomqtt

from franklinwh_bridge.publish.namespace import is_ours

logger = logging.getLogger(__name__)

#: How long to collect retained discovery configs after subscribing.
_COLLECT_S = 3.0
#: v0.1.0 wrote sw_version as "<aGate firmware> (bridge: v0.1.0)".
_LEGACY_SW = re.compile(r"\(bridge: v\d")


def _ours_or_legacy(payload: bytes | str | None) -> bool:
    if is_ours(payload):
        return True
    try:
        dev = (json.loads(payload or b"{}") or {}).get("device") or {}
    except (ValueError, TypeError):
        return False
    sw = dev.get("sw_version") or dev.get("sw") or ""
    return isinstance(sw, str) and bool(_LEGACY_SW.search(sw))


def stale_topics(
    retained: dict[str, bytes],
    prefix: str,
    short_ids: set[str],
    current: set[str],
) -> list[str]:
    """Pure: which retained discovery topics are our own stale leftovers."""
    heads = tuple(f"{prefix}_{sid}_" for sid in short_ids)
    out = []
    for topic, payload in retained.items():
        parts = topic.split("/")
        if len(parts) < 4 or not payload or topic in current:
            continue
        if parts[2].startswith(heads) and _ours_or_legacy(payload):
            out.append(topic)
    return sorted(out)


async def sweep_broker(publisher: Any) -> int:
    """Tombstone our own stale retained discovery configs. Returns the count."""
    if publisher.discovery_paused:
        return 0
    short_ids, current = publisher.published_discovery()
    if not short_ids:
        return 0
    retained: dict[str, bytes] = {}
    try:
        async with aiomqtt.Client(
            hostname=publisher._host,
            port=publisher._port,
            username=publisher._username,
            password=publisher._password,
            identifier=f"{publisher._client_id}_sweep",
        ) as client:
            await client.subscribe(f"{publisher.discovery_prefix}/+/+/config")

            async def collect() -> None:
                async for msg in client.messages:
                    if msg.retain:
                        retained[str(msg.topic)] = bytes(msg.payload or b"")

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(collect(), timeout=_COLLECT_S)
            stale = stale_topics(retained, publisher.topic_prefix, short_ids, current)
            for topic in stale:
                await client.publish(topic, b"", retain=True)
    except Exception as exc:
        logger.warning("Stale discovery sweep skipped: %s", exc)
        return 0
    if stale:
        logger.warning(
            "Removed %d stale discovery config(s) this bridge left on the broker: %s",
            len(stale),
            ", ".join(stale[:10]) + (" …" if len(stale) > 10 else ""),
        )
    return len(stale)


def old_identifiers(prefix: str, devices: list[tuple[str, str]]) -> set[str]:
    """Pre-0.2.4 identifiers for (gateway_id, serial) pairs."""
    from franklinwh_bridge.publish.mqtt_publisher import _slug

    out = set()
    for gw_id, serial in devices:
        if not serial:
            continue
        if gw_id and gw_id != "default":
            out.add(_slug(f"{prefix}_{gw_id}_{serial}"))
        else:
            out.add(f"{prefix}_{serial}")
    return out


async def remove_empty_old_devices(ha_instance: Any, identifiers: set[str]) -> int:
    """Delete our old, now-empty devices from Home Assistant. Returns the count."""
    if not identifiers or ha_instance is None or not getattr(ha_instance, "connected", False):
        return 0
    try:
        devices = await ha_instance.ws_call({"type": "config/device_registry/list"})
        entities = await ha_instance.ws_call({"type": "config/entity_registry/list"})
    except Exception as exc:
        logger.info("Old-device clean-up skipped (registry not readable): %s", exc)
        return 0
    in_use = {e.get("device_id") for e in entities or []}
    removed = 0
    for dev in devices or []:
        idents = {p[1] for p in dev.get("identifiers") or [] if len(p) == 2 and p[0] == "mqtt"}
        if not idents or not idents <= identifiers or dev.get("id") in in_use:
            continue
        for entry_id in dev.get("config_entries") or []:
            try:
                await ha_instance.ws_call(
                    {
                        "type": "config/device_registry/remove_config_entry",
                        "device_id": dev["id"],
                        "config_entry_id": entry_id,
                    }
                )
                removed += 1
                logger.warning(
                    "Removed empty old Home Assistant device %r (%s) left by an "
                    "earlier version of this bridge",
                    dev.get("name_by_user") or dev.get("name"),
                    ", ".join(sorted(idents)),
                )
                break
            except Exception as exc:
                logger.info("Could not remove old device %s: %s", dev.get("id"), exc)
    return removed
