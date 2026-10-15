"""This bridge's MQTT namespace: which one, and changing it safely (#87).

The namespace is ``mqtt_config.topic_prefix``. Since #91 it drives every state,
command and availability topic, every unique_id and every HA device identifier,
so on a broker shared with other FranklinWH producers (FWHAI, the Local Bridge)
a distinct value is what keeps their entities, devices and command topics apart
(#120: the same ``franklinwh`` namespace merged two producers into one device).

- New installs start on :data:`RECOMMENDED`; existing installs keep what they
  have — nothing moves without the user asking (:func:`choose_initial_namespace`).
- A change on an install that has already published goes through
  :func:`change_namespace`, which removes only the old discovery configs that
  this bridge wrote (:func:`is_ours`), never another producer's.

The discovery prefix (``homeassistant``) is not covered: it stays locked.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import aiosqlite

    from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher

logger = logging.getLogger(__name__)

LEGACY = "franklinwh"
RECOMMENDED = "fwh_mb"


@dataclass(frozen=True)
class NamespacePolicy:
    """The namespaces this bridge may use. Owner-adjustable defaults (#87)."""

    #: Offered by name, in this order. The legacy one is for installs that
    #: already publish under it.
    presets: dict[str, str] = field(
        default_factory=lambda: {
            RECOMMENDED: "Modbus Bridge (recommended)",
            LEGACY: "Legacy — shared with other FranklinWH integrations",
        }
    )
    #: A custom value must match this.
    custom_pattern: str = r"^fwh_[a-z0-9]{2,8}$"
    #: Other producers' namespaces: refused even though some match the pattern.
    reserved: dict[str, str] = field(
        default_factory=lambda: {
            "fwhai": "FWHAI (FranklinWH HA Integrator)",
            "fwh_ai": "FWHAI (FranklinWH HA Integrator)",
            "fwh_lb": "the FranklinWH Local Bridge",
        }
    )


NAMESPACE_POLICY = NamespacePolicy()

#: Every discovery payload this bridge writes has a device ``sw_version`` that
#: starts with this (mqtt_publisher.DeviceInfo.ha_device_block).
OUR_SW_PREFIX = "Modbus Bridge"

#: app_config keys.
CHOSEN_KEY = "mqtt_namespace_chosen"
PUBLISHED_KEY = "mqtt_discovery_first_published_at"

#: The MQTT client id every install used to share (#118).
SHARED_CLIENT_ID = "franklinwh_bridge"


class NamespaceError(Exception):
    """A change that can't be made; ``status`` is the HTTP status to answer."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def validate_namespace(prefix: str) -> str | None:
    """None if ``prefix`` may be used, else why not (a sentence for the user)."""
    p = NAMESPACE_POLICY
    if prefix in p.reserved:
        return f"'{prefix}' is reserved for {p.reserved[prefix]}"
    if prefix in p.presets:
        return None
    if re.fullmatch(p.custom_pattern, prefix or ""):
        return None
    return (
        f"'{prefix}' isn't allowed: choose {', '.join(p.presets)}, or a custom "
        "value of fwh_ plus 2–8 lowercase letters or digits"
    )


def is_ours(payload: bytes | str | None) -> bool:
    """True if a retained discovery config was written by this bridge.

    Decided by the device's ``sw_version`` — the only field that names the
    producer: FWHAI's say "FHAI: v…", and an overlapping slug gives both
    producers the same discovery topic and unique_id (#120).
    """
    if not payload:
        return False
    try:
        data = json.loads(payload)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    device = data.get("device") or data.get("dev") or {}
    if not isinstance(device, dict):
        return False
    sw = device.get("sw_version") or device.get("sw") or ""
    return isinstance(sw, str) and sw.startswith(OUR_SW_PREFIX)


# ── which namespace, at first start ──────────────────────────────────────────


async def _could_have_published(db: aiosqlite.Connection) -> bool:
    """Any sign this install has ever put an entity in Home Assistant.

    A gateway with an address, a serial or a past connection, or a mock, is what
    gets published; the default gateway row with no address cannot publish.
    Read before startup creates the default gateway from the environment, so a
    fresh install that sets MODBUS_HOST still counts as fresh.
    """
    from franklinwh_bridge.store.db import get_app_config

    if await get_app_config(db, PUBLISHED_KEY):
        return True
    async with db.execute(
        "SELECT 1 FROM gateways WHERE COALESCE(mock, 0) = 1"
        " OR TRIM(COALESCE(host, '')) <> '' OR COALESCE(serial, '') <> ''"
        " OR last_connected_at IS NOT NULL LIMIT 1"
    ) as cur:
        return await cur.fetchone() is not None


async def choose_initial_namespace(db: aiosqlite.Connection) -> dict[str, Any]:
    """Decide the namespace once per install, and record the decision.

    - Already decided (``mqtt_namespace_chosen`` set): nothing changes.
    - A stored prefix other than the legacy default was chosen by someone: kept.
    - Anything that could have been published (see ``_could_have_published``):
      the legacy ``franklinwh`` is kept, so existing HA entities stay put.
    - Otherwise a fresh install: :data:`RECOMMENDED`.
    """
    from franklinwh_bridge.store.db import (
        get_app_config,
        get_mqtt_config,
        set_app_config,
        set_mqtt_config,
    )

    raw = await get_app_config(db, CHOSEN_KEY)
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            return {"prefix": raw, "reason": "recorded"}

    current = (await get_mqtt_config(db)).get("topic_prefix") or LEGACY
    if current != LEGACY:
        choice = {"prefix": current, "reason": "kept_custom"}
    elif await _could_have_published(db):
        choice = {"prefix": LEGACY, "reason": "existing_install"}
    else:
        await set_mqtt_config(db, {"topic_prefix": RECOMMENDED})
        choice = {"prefix": RECOMMENDED, "reason": "new_install"}
    choice["at"] = time.time()
    await set_app_config(db, CHOSEN_KEY, json.dumps(choice))
    logger.info("MQTT namespace: %s (%s)", choice["prefix"], choice["reason"].replace("_", " "))
    return choice


async def ensure_client_id(db: aiosqlite.Connection) -> str:
    """Give this install its own MQTT client id (#118).

    Every install used to connect as ``franklinwh_bridge``; a broker keeps one
    connection per client id, so two bridges on one broker knocked each other
    off in a loop. Replaces only that shared default — a value someone set is
    left alone.
    """
    from franklinwh_bridge.store.db import get_mqtt_config, set_mqtt_config

    current = (await get_mqtt_config(db)).get("client_id") or ""
    if current and current != SHARED_CLIENT_ID:
        return current
    new = f"{SHARED_CLIENT_ID}_{secrets.token_hex(4)}"
    await set_mqtt_config(db, {"client_id": new})
    logger.info("MQTT client id set to %s (unique to this install, #118)", new)
    return new


async def mark_published(db: aiosqlite.Connection) -> None:
    """Remember that discovery has been published at least once."""
    from franklinwh_bridge.store.db import get_app_config, set_app_config

    if not await get_app_config(db, PUBLISHED_KEY):
        await set_app_config(db, PUBLISHED_KEY, str(time.time()))


async def has_published(db: aiosqlite.Connection, publisher: MqttPublisher | None) -> bool:
    """Has this install ever published discovery? Then a change needs confirming."""
    from franklinwh_bridge.store.db import get_app_config

    if await get_app_config(db, PUBLISHED_KEY):
        return True
    return bool(
        publisher is not None
        and publisher.state.discovery_published
        and publisher._has_discoverable()
    )


async def namespace_info(db: aiosqlite.Connection, publisher: MqttPublisher | None) -> dict:
    """What the UI needs to show and offer the namespace."""
    from franklinwh_bridge.store.db import get_app_config, get_mqtt_config

    cfg = await get_mqtt_config(db)
    chosen: Any = await get_app_config(db, CHOSEN_KEY)
    try:
        chosen = json.loads(chosen) if chosen else None
    except ValueError:
        chosen = {"prefix": chosen}
    p = NAMESPACE_POLICY
    return {
        "topic_prefix": publisher.topic_prefix if publisher is not None else cfg["topic_prefix"],
        "discovery_prefix": cfg["discovery_prefix"],
        "recommended": RECOMMENDED,
        "legacy": LEGACY,
        "presets": [{"value": k, "label": v} for k, v in p.presets.items()],
        "custom_pattern": p.custom_pattern,
        "reserved": sorted(p.reserved),
        "published": await has_published(db, publisher),
        "chosen": chosen,
    }


# ── changing it ──────────────────────────────────────────────────────────────


async def published_short_ids(db: aiosqlite.Connection, publisher: MqttPublisher) -> set[str]:
    """Every device short_id this bridge publishes or has published.

    The publisher's bound devices, plus every gateway on file — a gateway that
    is offline right now still has retained entities in HA.
    """
    from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo
    from franklinwh_bridge.store.db import get_gateways

    sids: set[str] = set(publisher.bound_short_ids())
    for row in await get_gateways(db):
        gw_id = row.get("id") or ""
        serial = row.get("serial") or ""
        if gw_id == publisher.gateway_id and not serial:
            continue  # the default device's id comes from its serial
        sid = DeviceInfo(serial=serial, gateway_id=gw_id).short_id
        if sid:
            sids.add(sid)
    return sids


async def change_namespace(
    db: aiosqlite.Connection,
    publisher: MqttPublisher,
    new_prefix: str,
    *,
    confirm: bool,
    actor: str = "",
    window_s: float = 2.0,
) -> dict[str, Any]:
    """Move this bridge to ``new_prefix``, removing only its own old entities.

    1. Validate; unchanged is a no-op.
    2. If anything was ever published (``confirm`` required): stop publishing,
       read the old namespace's retained discovery configs and empty only those
       this bridge wrote. Another producer's are left; the old availability and
       state topics are left too (they may be shared, and a stale retained state
       is harmless). If the broker can't be reached nothing changes.
    3. Save, switch the publisher (topics, unique_ids, device identifiers,
       command subscription) and republish under the new namespace.
    4. Audit: a WARNING plus a persisted event.
    """
    from franklinwh_bridge.store.db import (
        log_startup_event,
        set_app_config,
        set_mqtt_config,
    )

    old = publisher.topic_prefix
    new_prefix = (new_prefix or "").strip()
    if new_prefix == old:
        return {"status": "unchanged", "topic_prefix": old}
    problem = validate_namespace(new_prefix)
    if problem:
        raise NamespaceError(422, problem)

    published = await has_published(db, publisher)
    if published and not confirm:
        raise NamespaceError(
            409,
            "This bridge has already published to Home Assistant: changing the "
            "namespace creates new entities and a new device. Confirm to proceed.",
        )

    # Stopped for the switch either way: nothing may be published under the old
    # namespace after it has been cleaned, and the command subscription moves.
    was_running = publisher.is_running
    if was_running:
        await publisher.stop()
    counts = {"removed_ours": 0, "left_foreign": 0, "not_found": 0}
    if published:
        sids = await published_short_ids(db, publisher)
        try:
            counts = await publisher.tombstone_own_discovery(old, sids, window_s=window_s)
        except Exception as exc:
            if was_running:
                await publisher.start()
            logger.error("Namespace change %s → %s abandoned: %s", old, new_prefix, exc)
            raise NamespaceError(
                503,
                f"Couldn't reach the MQTT broker to remove the old entities ({exc}); "
                "nothing was changed",
            ) from exc

    await set_mqtt_config(db, {"topic_prefix": new_prefix})
    await set_app_config(
        db,
        CHOSEN_KEY,
        json.dumps(
            {
                "prefix": new_prefix,
                "reason": "changed",
                "from": old,
                "at": time.time(),
                "by": actor or None,
            }
        ),
    )
    publisher.switch_namespace(new_prefix)
    if was_running:
        await publisher.start()

    detail = f"{old} → {new_prefix} by {actor or 'unknown'}: " + (
        f"removed {counts['removed_ours']} of this bridge's old entities, left "
        f"{counts['left_foreign']} belonging to other producers, "
        f"{counts['not_found']} not on the broker"
        if published
        else "nothing had been published, so nothing was removed"
    )
    logger.warning("MQTT namespace changed %s", detail)
    await log_startup_event(db, "mqtt_namespace_change", detail)
    return {
        "status": "changed",
        "from": old,
        "topic_prefix": new_prefix,
        "scanned": published,
        **counts,
    }
