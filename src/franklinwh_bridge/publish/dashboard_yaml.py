"""Home Assistant dashboard card YAML for this bridge's entities (#86).

Entity ids come from Home Assistant's own registries, never from a prediction:
HA appends ``_2`` on a collision and users rename entities, so a card built from
guessed ids shows "Entity not available" rows.

Selecting *our* rows takes the device registry as well as the entity registry.
Other producers (FWHAI, the Local Bridge) publish ``franklinwh_<id>_<slug>``
unique_ids on the same ``mqtt`` platform, so platform + unique_id prefix alone
would sweep their entities in. A row is ours only when its device carries an
``["mqtt", <identifier>]`` this publisher currently announces, and its
unique_id starts ``<topic_prefix>_<short_id>_`` for that device.

Output is plain core ``entities`` cards (no custom cards, no theme line), the
same shape the FWHAI integration ships:

- ``grouped``: a ``vertical-stack`` with one ``entities`` card per group, titled
  "Modbus Bridge — <Group>". With more than one gateway device each title also
  names the device: "Modbus Bridge — <Device> — <Group>".
- ``single``: one ``entities`` card, "Modbus Bridge", every id.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from franklinwh_bridge.publish.entities import BRIDGE_ENTITIES, EntityDef

TITLE = "Modbus Bridge"
DIAGNOSTICS = "Diagnostics"
#: Card order. Diagnostics is last and only emitted when asked for.
GROUPS = ["Power & Energy", "Battery", "Controls", "Status", DIAGNOSTICS]
LAYOUTS = ("grouped", "single")


class DashboardError(Exception):
    """A card can't be built; ``status`` is the HTTP code the route returns."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class OurDevice:
    """A gateway device this publisher announces to HA right now."""

    identifier: str
    short_id: str
    name: str
    #: Slug order for this device: BRIDGE_ENTITIES first, then anything else it
    #: publishes (promoted catalog points).
    defs: list[EntityDef] = field(default_factory=list)


def group_for(entity: EntityDef | None) -> str:
    """Card group for an EntityDef; an unknown slug lands in Diagnostics."""
    if entity is None or entity.entity_category in ("diagnostic", "config"):
        return DIAGNOSTICS
    # state_group "control" holds active_reserve_pct, a read-only sensor that
    # belongs next to the reserve numbers it reports (FWHAI groups it the same).
    if entity.is_control or entity.state_group == "control":
        return "Controls"
    if entity.state_group in ("power", "energy", "solar"):
        return "Power & Energy"
    if entity.state_group in ("battery", "capacity"):
        return "Battery"
    return "Status"


def _ordered_defs(published: list[EntityDef] | None) -> list[EntityDef]:
    out = list(BRIDGE_ENTITIES)
    seen = {e.slug for e in out}
    for e in published or []:
        if e.slug not in seen:
            out.append(e)
            seen.add(e.slug)
    return out


def our_devices(publisher: Any) -> list[OurDevice]:
    """Every device the publisher announces, keyed by its HA identifier."""
    prefix = publisher.topic_prefix
    out: list[OurDevice] = []
    seen: set[str] = set()

    def add(info: Any, published: list[EntityDef] | None) -> None:
        if info is None:
            return
        block = info.ha_device_block(prefix=prefix)
        for ident in block.get("identifiers") or []:
            if ident in seen:
                continue
            seen.add(ident)
            out.append(
                OurDevice(
                    identifier=ident,
                    short_id=info.short_id,
                    name=block.get("name") or info.short_id,
                    defs=_ordered_defs(published),
                )
            )

    add(publisher.device_info, list(publisher.entities or []))
    for dev in getattr(publisher, "_devices", {}).values():
        add(dev.device_info, list(dev.entities or []))
    return out


@dataclass
class DeviceCards:
    device: OurDevice
    ha_device_id: str
    name: str
    #: group title -> [entity_id, ...], in GROUPS order, empty groups dropped.
    groups: list[tuple[str, list[str]]]

    @property
    def entity_ids(self) -> list[str]:
        return [eid for _, ids in self.groups for eid in ids]


def select_ours(
    device_rows: list[dict],
    entity_rows: list[dict],
    ours: list[OurDevice],
    topic_prefix: str,
    include_diagnostics: bool = False,
) -> list[DeviceCards]:
    """Match HA's registry rows to our devices; group each device's entities."""
    by_ident = {d.identifier: d for d in ours}
    matched: dict[str, tuple[OurDevice, str]] = {}  # HA device_id -> (ours, name)
    for row in device_rows or []:
        for pair in row.get("identifiers") or []:
            if (
                isinstance(pair, (list, tuple))
                and len(pair) == 2
                and pair[0] == "mqtt"
                and pair[1] in by_ident
            ):
                dev = by_ident[pair[1]]
                name = row.get("name_by_user") or row.get("name") or dev.name
                matched[row.get("id")] = (dev, name)
                break

    # HA device_id -> [(sort key, group, entity_id)]
    buckets: dict[str, list[tuple[tuple[int, str], str, str]]] = {k: [] for k in matched}
    for row in entity_rows or []:
        if row.get("platform") != "mqtt" or row.get("disabled_by"):
            continue
        ha_dev = row.get("device_id")
        eid = row.get("entity_id")
        if ha_dev not in matched or not eid:
            continue
        dev, _ = matched[ha_dev]
        head = f"{topic_prefix}_{dev.short_id}_"
        uid = str(row.get("unique_id") or "")
        if not uid.startswith(head) or len(uid) == len(head):
            continue
        slug = uid[len(head) :]
        index = next((i for i, e in enumerate(dev.defs) if e.slug == slug), None)
        entity = dev.defs[index] if index is not None else None
        group = group_for(entity)
        if group == DIAGNOSTICS and not include_diagnostics:
            continue
        key = (index if index is not None else len(dev.defs), slug)
        buckets[ha_dev].append((key, group, eid))

    out: list[DeviceCards] = []
    order = {d.identifier: i for i, d in enumerate(ours)}
    for ha_dev, (dev, name) in sorted(matched.items(), key=lambda kv: order[kv[1][0].identifier]):
        rows = sorted(buckets[ha_dev])
        groups = []
        for title in GROUPS:
            ids = [eid for _, g, eid in rows if g == title]
            if ids:
                groups.append((title, ids))
        out.append(DeviceCards(device=dev, ha_device_id=ha_dev, name=name, groups=groups))
    return out


_PLAIN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .()&/—\-]*$")


def _scalar(value: str) -> str:
    """A YAML scalar: bare when it's plainly safe, else a JSON (= YAML
    double-quoted) string — device names are user text."""
    if _PLAIN.match(value) and not value.endswith(" "):
        return value
    return json.dumps(value, ensure_ascii=False)


def _entities_card(title: str, ids: list[str], indent: str = "") -> list[str]:
    out = [
        f"{indent}type: entities",
        f"{indent}title: {_scalar(title)}",
        f"{indent}show_header_toggle: false",
        f"{indent}state_color: true",
        f"{indent}entities:",
    ]
    out += [f"{indent}  - entity: {_scalar(eid)}" for eid in ids]
    return out


def render(cards: list[DeviceCards], layout: str = "grouped") -> str:
    """The card YAML; empty string when there is nothing to show."""
    cards = [c for c in cards if c.groups]
    if not cards:
        return ""
    if layout == "single":
        ids = [eid for c in cards for eid in c.entity_ids]
        return "\n".join(_entities_card(TITLE, ids)) + "\n"
    multi = len(cards) > 1
    lines = ["type: vertical-stack", "cards:"]
    for c in cards:
        for group, ids in c.groups:
            title = f"{TITLE} — {c.name} — {group}" if multi else f"{TITLE} — {group}"
            card = _entities_card(title, ids, indent="    ")
            card[0] = "  - " + card[0].lstrip()
            lines += card
    return "\n".join(lines) + "\n"


async def build_dashboard(
    inst: Any,
    publisher: Any,
    layout: str = "grouped",
    include_diagnostics: bool = False,
) -> dict:
    """Read HA's registries over ``inst.ws_call`` and build the card.

    Raises DashboardError rather than ever returning guessed ids.
    """
    if layout not in LAYOUTS:
        raise DashboardError(422, f"layout must be one of {', '.join(LAYOUTS)}")
    ours = our_devices(publisher)
    if not ours:
        raise DashboardError(409, "The bridge isn't publishing any gateway to MQTT yet.")
    try:
        device_rows = await inst.ws_call({"type": "config/device_registry/list"})
        entity_rows = await inst.ws_call({"type": "config/entity_registry/list"})
    except Exception as exc:
        raise DashboardError(
            503,
            f"Couldn't read the device/entity registry from Home Assistant "
            f"'{inst.name}': {str(exc) or type(exc).__name__}. The card is built "
            f"only from HA's registry, so no YAML was produced.",
        ) from exc
    cards = select_ours(
        device_rows or [],
        entity_rows or [],
        ours,
        publisher.topic_prefix,
        include_diagnostics,
    )
    yaml_text = render(cards, layout)
    if not yaml_text:
        found = "no device of ours" if not cards else "none of our entities"
        raise DashboardError(
            409,
            f"Home Assistant '{inst.name}' has {found} in its registry. Check the "
            f"MQTT integration is set up and discovery has been published.",
        )
    return {
        "yaml": yaml_text,
        "entity_count": sum(len(c.entity_ids) for c in cards),
        "devices": [
            {
                "name": c.name,
                "short_id": c.device.short_id,
                "identifier": c.device.identifier,
                "ha_device_id": c.ha_device_id,
                "entity_count": len(c.entity_ids),
            }
            for c in cards
        ],
        "layout": layout,
    }
