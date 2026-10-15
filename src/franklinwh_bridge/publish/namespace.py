"""This bridge's MQTT namespace: which one, and changing it safely (#87).

The namespace is ``mqtt_config.topic_prefix``. Since #91 it drives every state,
command and availability topic, every unique_id and every HA device identifier,
so on a broker shared with other FranklinWH producers (FWHAI, the Local Bridge)
a distinct value is what keeps their entities, devices and command topics apart
(#120: the same ``franklinwh`` namespace merged two producers into one device).

- A new install publishes nothing to Home Assistant until the user has chosen
  the naming (the setup wizard's "Home Assistant naming" step, or the
  ``MQTT_NAMESPACE`` environment variable): the choice is *pending* and the
  publisher holds. Existing installs keep what they have, with nothing pending —
  nothing moves without the user asking (:func:`choose_initial_namespace`).
- A change on an install that has already published goes through
  :func:`change_namespace`, which removes only the old discovery configs that
  this bridge wrote (:func:`is_ours`), never another producer's.

The discovery prefix (``homeassistant``) is not covered: it stays locked.
"""

from __future__ import annotations

import json
import logging
import os
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
#: Set on a fresh install until the naming is chosen; while set (and nothing is
#: chosen) the publisher holds — no discovery, state, availability or commands.
PENDING_KEY = "mqtt_namespace_pending"

#: Headless choice for an install nobody opens the UI on (Docker): read at
#: startup on a fresh install, and counts as chosen. Ignored once a namespace
#: is in use — a change goes through Settings.
NAMESPACE_ENV = "MQTT_NAMESPACE"

#: The user documentation for this choice, linked from the UI.
DOCS_URL = (
    "https://github.com/david2069/franklinwh-modbus-bridge/blob/main/docs/home-assistant-naming.md"
)

#: Entities shown in the preview, and probed for a previous install.
PREVIEW_SLUGS = ("battery_soc", "grid_power_kw", "operating_mode")

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


async def is_pending(db: aiosqlite.Connection) -> bool:
    """True while a fresh install waits for the user to choose the naming."""
    from franklinwh_bridge.store.db import get_app_config

    return bool(await get_app_config(db, PENDING_KEY)) and not await get_app_config(db, CHOSEN_KEY)


async def _record_choice(
    db: aiosqlite.Connection, prefix: str, reason: str, actor: str = ""
) -> dict[str, Any]:
    from franklinwh_bridge.store.db import set_app_config, set_mqtt_config

    await set_mqtt_config(db, {"topic_prefix": prefix})
    choice: dict[str, Any] = {"prefix": prefix, "reason": reason, "at": time.time()}
    if actor:
        choice["by"] = actor
    await set_app_config(db, CHOSEN_KEY, json.dumps(choice))
    await set_app_config(db, PENDING_KEY, "")
    return choice


async def choose_initial_namespace(
    db: aiosqlite.Connection, env_value: str | None = None
) -> dict[str, Any]:
    """Decide the namespace once per install, and record the decision.

    - Already decided (``mqtt_namespace_chosen`` set): nothing changes, and
      ``MQTT_NAMESPACE`` is ignored (a change goes through Settings).
    - A stored prefix other than the legacy default was chosen by someone: kept.
    - Anything that could have been published (see ``_could_have_published``):
      the legacy ``franklinwh`` is kept, so existing HA entities stay put.
    - Otherwise a fresh install: ``MQTT_NAMESPACE`` (``env_value``, read from
      the environment when not given) if set and allowed — that counts as the
      user's choice. Without it the choice is *pending* (``"pending": True``):
      the stored prefix is :data:`RECOMMENDED` for the preview, and nothing may
      be published until the user chooses (:func:`choose_namespace`).
    """
    from franklinwh_bridge.store.db import (
        get_app_config,
        get_mqtt_config,
        set_app_config,
        set_mqtt_config,
    )

    if env_value is None:
        env_value = os.environ.get(NAMESPACE_ENV, "")
    env_value = (env_value or "").strip()

    raw = await get_app_config(db, CHOSEN_KEY)
    if raw:
        try:
            choice = json.loads(raw)
        except ValueError:
            choice = {"prefix": raw, "reason": "recorded"}
        if env_value and env_value != choice.get("prefix"):
            logger.warning(
                "%s=%s ignored: this install already publishes as '%s'. Change it in "
                "Settings → Home Assistant → MQTT Broker → Change namespace…",
                NAMESPACE_ENV,
                env_value,
                choice.get("prefix"),
            )
        return choice

    if not await get_app_config(db, PENDING_KEY):
        current = (await get_mqtt_config(db)).get("topic_prefix") or LEGACY
        if current != LEGACY or await _could_have_published(db):
            reason = "kept_custom" if current != LEGACY else "existing_install"
            choice = {"prefix": current, "reason": reason, "at": time.time()}
            await set_app_config(db, CHOSEN_KEY, json.dumps(choice))
            logger.info("MQTT namespace: %s (%s)", current, reason.replace("_", " "))
            return choice
        # A fresh install: nothing is published until the naming is chosen.
        await set_mqtt_config(db, {"topic_prefix": RECOMMENDED})
        await set_app_config(db, PENDING_KEY, str(time.time()))

    if env_value:
        problem = validate_namespace(env_value)
        if problem is None:
            choice = await _record_choice(db, env_value, "env")
            logger.info("MQTT namespace: %s (chosen with %s)", env_value, NAMESPACE_ENV)
            return choice
        logger.error("%s=%s not used: %s", NAMESPACE_ENV, env_value, problem)

    prefix = (await get_mqtt_config(db)).get("topic_prefix") or RECOMMENDED
    logger.warning(
        "Home Assistant publishing is waiting for you to choose a naming: open the "
        "bridge's setup (Home Assistant naming), or set %s=%s and restart",
        NAMESPACE_ENV,
        RECOMMENDED,
    )
    return {"prefix": prefix, "reason": "pending", "pending": True}


async def choose_namespace(
    db: aiosqlite.Connection,
    publisher: MqttPublisher | None,
    prefix: str,
    *,
    acknowledged: bool,
    actor: str = "",
) -> dict[str, Any]:
    """The user's explicit choice of naming (the wizard step).

    Needs ``acknowledged`` (the user understands it is permanent). On a fresh
    install it records the choice and releases the publisher, which then
    publishes for the first time, under it. On an install that isn't pending
    and hasn't published it simply switches (:func:`change_namespace`); once
    something is published it refuses (409): that change removes old entities
    and belongs to Settings → Change namespace….
    """
    from franklinwh_bridge.store.db import log_startup_event

    prefix = (prefix or "").strip()
    if not acknowledged:
        raise NamespaceError(422, "Tick 'I understand this is permanent' to choose the naming")
    problem = validate_namespace(prefix)
    if problem:
        raise NamespaceError(422, problem)

    if not await is_pending(db):
        if await has_published(db, publisher):
            raise NamespaceError(
                409,
                "This bridge has already published to Home Assistant, so its naming is "
                "set. To change it (new entities and a new device), use Settings → Home "
                "Assistant → MQTT Broker → Change namespace….",
            )
        if publisher is None:
            await _record_choice(db, prefix, "chosen", actor)
            return {"status": "chosen", "topic_prefix": prefix}
        return await change_namespace(db, publisher, prefix, confirm=False, actor=actor)

    restart = False
    if publisher is not None and publisher.topic_prefix != prefix:
        restart = publisher.is_running
        if restart:
            await publisher.stop()
        publisher.switch_namespace(prefix)
    await _record_choice(db, prefix, "chosen", actor)
    if publisher is not None:
        publisher.release()
        if restart:
            await publisher.start()
    detail = f"{prefix} chosen by {actor or 'unknown'}; Home Assistant publishing starts"
    logger.warning("MQTT namespace %s", detail)
    await log_startup_event(db, "mqtt_namespace_chosen", detail)
    return {"status": "chosen", "topic_prefix": prefix}


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
        # A fresh install's choice still to be made: nothing is published yet.
        "pending": await is_pending(db),
        "env_var": NAMESPACE_ENV,
        "docs_url": DOCS_URL,
    }


# ── the preview, and a previous install on the broker ────────────────────────


def preview_namespace(
    prefix: str,
    gateways: list[dict[str, Any]],
    discovery_prefix: str = "homeassistant",
) -> list[dict[str, Any]]:
    """What Home Assistant gets for each gateway under ``prefix``.

    Built with the same code that publishes (``DeviceInfo``, ``EntityDef``), so
    the preview can't drift from what is sent. A default gateway whose serial
    isn't known yet (not connected) is shown with placeholders.
    """
    from franklinwh_bridge.publish.entities import get_entity_by_slug
    from franklinwh_bridge.publish.mqtt_publisher import (
        DeviceInfo,
        _slug,
    )

    # Stand-ins for a serial not read yet, and what the user is shown instead.
    full_ph, short_ph = "PHSERIALXX000000", "XX000000"

    out: list[dict[str, Any]] = []
    for row in gateways:
        gw_id = row.get("id") or row.get("gateway_id") or "default"
        mock = bool(row.get("mock"))
        serial = row.get("serial") or ""
        # The serial is read from the aGate when it connects; until then the
        # name (and, for the first gateway, the ids) show a placeholder.
        known = bool(serial) or mock
        info = DeviceInfo(
            serial=serial if known else full_ph,
            gateway_id=gw_id,
            # How ha_device_block recognises a mock ("… (mock)" name).
            firmware="MOCK" if mock else "",
        )
        sid = info.short_id

        def show(text: str | None, _known: bool = known) -> str | None:
            if not text or _known:
                return text
            text = text.replace(full_ph, "<serial>")
            return text.replace(short_ph, "<last 8 of serial>").replace(
                short_ph.lower(), "<last 8 of serial>"
            )

        # The device name rule of 0.2.5: the short serial for every gateway
        # (a mock keeps its id + "(mock)"); ids and topics use ``sid``, which
        # for an added gateway is its gateway id.
        block = info.ha_device_block("", prefix)
        device_name = block["name"]
        entities = []
        for slug in PREVIEW_SLUGS:
            ent = get_entity_by_slug(slug)
            if ent is None or (ent.is_control and row.get("mock")):
                continue  # a demo has no controls in HA (GatewayDevice.rebuild_entities)
            # HA makes the entity id from the device and entity names, not from
            # the namespace; the user can rename it in HA.
            entity_id = f"{ent.ha_type}.{_slug(device_name + ' ' + ent.name)}"
            entities.append(
                {
                    "slug": slug,
                    "name": ent.name,
                    "ha_type": ent.ha_type,
                    "unique_id": show(ent.unique_id(sid, prefix)),
                    "entity_id": show(entity_id),
                    "state_topic": show(ent.state_topic(sid, prefix)),
                    "command_topic": show(ent.command_topic(sid, prefix)),
                    "discovery_topic": show(ent.discovery_topic(sid, prefix, discovery_prefix)),
                }
            )
        out.append(
            {
                "gateway_id": gw_id,
                "name": row.get("name") or gw_id,
                "mock": bool(row.get("mock")),
                "publish_to_ha": bool(row.get("publish_to_ha", 1)),
                "short_id": show(sid),
                "serial_known": known,
                "device_name": show(device_name),
                "device_identifier": show(block["identifiers"][0]),
                "topic_root": show(f"{prefix}/{sid}/"),
                "availability_topic": show(f"{prefix}/{sid}/availability"),
                "entities": entities,
            }
        )
    return out


async def detect_previous_install(
    db: aiosqlite.Connection,
    publisher: MqttPublisher,
    *,
    window_s: float = 1.5,
) -> dict[str, Any]:
    """Look on the broker for this bridge's entities from an earlier install.

    Read-only. Checks a few named discovery topics (:data:`PREVIEW_SLUGS`) per
    known device and per candidate namespace — the presets plus the stored one —
    rather than subscribing to all of ``homeassistant/#``. A retained config
    counts only if this bridge wrote it (:func:`is_ours`); another producer's
    under the same name is reported separately, as a reason not to pick it.
    """
    from franklinwh_bridge.publish.entities import get_entity_by_slug

    sids = sorted(await published_short_ids(db, publisher))
    prefixes = list(NAMESPACE_POLICY.presets)
    if publisher.topic_prefix not in prefixes:
        prefixes.append(publisher.topic_prefix)
    result: dict[str, Any] = {
        "checked": False,
        "found": False,
        "prefix": None,
        "ours": {},
        "foreign": {},
        "short_ids": sids,
        "error": None,
    }
    if not sids:
        result["error"] = "No aGate serial known yet, so there is nothing to look for"
        return result
    by_topic: dict[str, str] = {}
    for prefix in prefixes:
        for sid in sids:
            for slug in PREVIEW_SLUGS:
                ent = get_entity_by_slug(slug)
                if ent is not None:
                    by_topic[ent.discovery_topic(sid, prefix, publisher.discovery_prefix)] = prefix
    try:
        retained = await publisher.probe_retained(list(by_topic), window_s=window_s)
    except Exception as exc:
        result["error"] = f"Couldn't read the MQTT broker ({exc})"
        return result
    result["checked"] = True
    for topic, payload in retained.items():
        prefix = by_topic.get(topic)
        if prefix is None:
            continue
        bucket = result["ours"] if is_ours(payload) else result["foreign"]
        bucket[prefix] = bucket.get(prefix, 0) + 1
    if result["ours"]:
        # The namespace with most of our entities; ties go to the policy order.
        result["prefix"] = max(prefixes, key=lambda p: result["ours"].get(p, 0))
        result["found"] = True
    return result


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
    # While a fresh install's choice is pending this only moves the preselected
    # value: the choice itself — with its acknowledgement — is choose_namespace's.
    if not await is_pending(db):
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
