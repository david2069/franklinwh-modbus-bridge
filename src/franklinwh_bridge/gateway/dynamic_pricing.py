"""Dynamic (live) tariff pricing (#89, part B).

A service's ``pricing`` JSON may say ``kind: "dynamic"``. Its energy is then
priced at a LIVE rate instead of the static seasons / default_rate:

    pricing.kind    = "dynamic"            ("static" or absent = today's model)
    pricing.dynamic = {source: "nem", region: "NSW1"}
                    | {source: "ha", buy_entity: "ha:<inst>:<entity>",
                                     feedin_entity: "ha:<inst>:<entity>" | ""}

Same shape as the Local Bridge (``scheduler.tariff_dynamic_provider``), so a
tariff profile carries across. The live price comes from part A's helpers:
:func:`~franklinwh_bridge.gateway.aemo_nem.resolve_prices` (NEM: sell = buy =
spot) and :func:`~franklinwh_bridge.gateway.aemo_nem.ha_price_c_kwh` (HA unit
handling). For HA, sell falls back to buy when no feed-in entity is set or it
has no value.

Pure functions only. No network: the NEM spot is passed in from the cache.
"""

from __future__ import annotations

from typing import Any

from franklinwh_bridge.gateway.aemo_nem import (
    REGIONS,
    ha_price_c_kwh,
    resolve_prices,
)
from franklinwh_bridge.gateway.aemo_nem import (
    SOURCE as NEM_SOURCE,
)

KINDS: tuple[str, ...] = ("static", "dynamic")
SOURCES: tuple[str, ...] = ("nem", "ha")

#: A cached NEM price older than this does not price energy: that slice goes to
#: unpriced kWh. Two 5-minute dispatch intervals; the service refreshes every
#: 240 s, so a healthy cache is never older than this.
BILLING_MAX_AGE_S = 600.0

#: The billing-history label for energy priced from the NEM spot.
WHOLESALE_LABEL = "Wholesale (live)"


def is_dynamic(pricing: Any) -> bool:
    return isinstance(pricing, dict) and pricing.get("kind") == "dynamic"


def _ha_id(value: Any, field: str, *, required: bool) -> str:
    s = "" if value is None else str(value).strip()
    if not s:
        if required:
            raise ValueError("Choose a buy price entity for the Home Assistant provider.")
        return ""
    parts = s.split(":", 2)
    if len(parts) != 3 or parts[0] != "ha" or not parts[1] or not parts[2]:
        raise ValueError(f"{field} must be an HA sensor id 'ha:<instance>:<entity_id>'")
    return s


def normalise_dynamic(dyn: Any) -> dict[str, str]:
    """Validate a ``pricing.dynamic`` block and return its canonical form.

    Raises ValueError with a plain sentence. The checks are the Local Bridge's
    (``/api/tariffs/validate``): a provider must be chosen, NEM needs a region,
    HA needs a buy entity. Here the region must also be a real NEM region and
    the entities must be scheduler HA ids, as part A's settings require.
    """
    if not isinstance(dyn, dict):
        raise ValueError("Choose a live price provider (AEMO NEM or Home Assistant).")
    source = str(dyn.get("source") or "").strip().lower()
    if source == "nem":
        region = str(dyn.get("region") or "").strip().upper()
        if not region:
            raise ValueError("Choose a NEM region for the AEMO NEM provider.")
        if region not in REGIONS:
            raise ValueError(f"region must be one of {', '.join(REGIONS)}")
        return {"source": "nem", "region": region}
    if source == "ha":
        return {
            "source": "ha",
            "buy_entity": _ha_id(dyn.get("buy_entity"), "buy_entity", required=True),
            "feedin_entity": _ha_id(dyn.get("feedin_entity"), "feedin_entity", required=False),
        }
    raise ValueError("Choose a live price provider (AEMO NEM or Home Assistant).")


def normalise_pricing(pricing: Any) -> Any:
    """Validate the ``kind`` / ``dynamic`` keys of a service's pricing JSON.

    Everything else in ``pricing`` is passed through untouched (seasons,
    default_rate, demand/bonus/charge, fixed charges). A dynamic plan keeps its
    static rates on file, so switching back to Static loses nothing.
    """
    if not isinstance(pricing, dict) or "kind" not in pricing:
        return pricing
    kind = pricing.get("kind")
    if kind in (None, ""):
        kind = "static"
    if kind not in KINDS:
        raise ValueError("pricing.kind must be 'static' or 'dynamic'")
    out = {**pricing, "kind": kind}
    if kind == "dynamic":
        out["dynamic"] = normalise_dynamic(pricing.get("dynamic"))
    return out


def pricing_problems(pricing: Any) -> list[str] | None:
    """Problems for a dynamic plan, or None when the plan is not dynamic (the
    caller then runs the static rate validator)."""
    if not is_dynamic(pricing):
        return None
    try:
        normalise_dynamic(pricing.get("dynamic"))
    except ValueError as exc:
        return [str(exc)]
    return []


def label(dyn: dict | None) -> str:
    """What priced the energy, for billing history and the editor."""
    dyn = dyn or {}
    if dyn.get("source") == "nem":
        return f"{WHOLESALE_LABEL} · {NEM_SOURCE} {dyn.get('region') or ''}".strip()
    if dyn.get("source") == "ha":
        ent = str(dyn.get("buy_entity") or "").split(":", 2)[-1]
        return f"Live price · HA {ent}".strip()
    return WHOLESALE_LABEL


def live_prices(dyn: dict | None, spot: dict | None, entity_state: Any = None) -> dict[str, Any]:
    """The live buy/sell for a dynamic plan, in c/kWh and $/kWh.

    ``spot`` is the plan region's cached NEM row (or None); ``entity_state`` is
    ``sid -> HA state dict``. A side with no value is None: the caller must not
    price that energy.
    """
    dyn = dyn or {}
    src = dyn.get("source")
    if src == "nem":
        got = resolve_prices(spot)
    elif src == "ha":

        def one(sid: str) -> float | None:
            if not sid or entity_state is None:
                return None
            try:
                return ha_price_c_kwh(entity_state(sid))
            except Exception:  # pragma: no cover - defensive
                return None

        buy = one(dyn.get("buy_entity") or "")
        sell = one(dyn.get("feedin_entity") or "")
        got = resolve_prices(None, buy, sell)
        # HA: sell falls back to buy (the Local Bridge's rule).
        if got["sell_c_kwh"] is None and got["buy_c_kwh"] is not None:
            got = {**got, "sell_c_kwh": got["buy_c_kwh"], "sell_source": got["source"]}
    else:
        got = resolve_prices(None)
    buy_c, sell_c = got["buy_c_kwh"], got["sell_c_kwh"]
    return {
        **got,
        "buy": None if buy_c is None else round(buy_c / 100.0, 6),
        "sell": None if sell_c is None else round(sell_c / 100.0, 6),
        "label": label(dyn),
        "settlement_ts": (spot or {}).get("settlement_ts") if src == "nem" else None,
    }


def as_rate(prices: dict | None) -> dict[str, Any]:
    """A live price in the shape :func:`rate_model.resolve` returns, so the
    sensors and the accrual read a dynamic plan exactly like a static one."""
    p = prices or {}
    buy, sell = p.get("buy"), p.get("sell")
    return {
        "season": None,
        "time_period": None,
        "time_period_label": p.get("label") or WHOLESALE_LABEL,
        "buy": buy,
        "sell": sell,
        "tier": None,
        "billable": None if buy is None else bool(buy > 0.0),
        "reason": "dynamic" if buy is not None or sell is not None else "no_live_price",
    }
