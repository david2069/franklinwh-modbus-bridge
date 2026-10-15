"""AEMO NEM wholesale spot price + optional HA price entities (#89, part A).

Matches the Local Bridge's ``aemo_nem.py`` so automations port between the two
bridges: same AEMO endpoint (``ELEC_NEM_SUMMARY``, public, no key), same row
pick (``REGIONID``), same conversion (``PRICE`` is the 5-minute RRP in $/MWh;
c/kWh = RRP / 10, rounded to 2), same five regions, 240 s cache, 12 s timeout,
and the same ``tariff.spot_price`` / ``tariff.feed_in_price`` sensor names.

How it fits this bridge (not the Local Bridge's structure) — the same shape as
the Open-Meteo feature (#88, ``solar_forecast.py``):

* Settings live in ``app_config`` under one JSON key. ``nem_region == ""``
  means off: nothing is fetched until a region is set.
* The scheduler's points merge is synchronous, so it never waits on AEMO. A
  background loop (:meth:`NemPricingService.run`) keeps the cache warm with
  ``httpx`` (async) and :meth:`NemPricingService.as_points` only reads it.
* Never raises. A failed fetch returns the last good value flagged
  ``stale: True``, or None.

Price precedence (same as the Local Bridge): NEM first (buy = sell = spot),
then a configured HA price entity overwrites its own side ("HA wins"). An HA
value in ``$``/AUD per kWh is scaled x100; anything else is taken as c/kWh.

Part B of #89 (a ``pricing.kind: "dynamic"`` tariff, ``dynamic_pricing.py``)
reuses :class:`NemSpotClient` and :func:`resolve_prices`. While the active plan
is dynamic, :meth:`NemPricingService.as_points` makes ``tariff.spot_price`` /
``tariff.feed_in_price`` follow the PLAN's source (it overrides the settings
above) and adds the live $/kWh that ``tariff.buy_rate`` / ``tariff.sell_rate``
and the billing accrual read. A static plan leaves both exactly as before.
"""

from __future__ import annotations

import asyncio
import calendar
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import httpx

from franklinwh_bridge.store.db import get_app_config, set_app_config

logger = logging.getLogger(__name__)

AEMO_URL = "https://visualisations.aemo.com.au/aemo/apps/api/report/ELEC_NEM_SUMMARY"
#: The five NEM regions (AEMO ``REGIONID``).
REGIONS: tuple[str, ...] = ("NSW1", "QLD1", "SA1", "TAS1", "VIC1")
CACHE_S = 240.0  # NEM dispatch is 5-min; ~4 min keeps us never stale-by-much
TIMEOUT_S = 12.0
#: A cached price older than this no longer feeds the condition sensors (they
#: fail closed). The route still reports it, flagged stale.
SENSOR_MAX_AGE_S = 1800.0
#: NEM time (``SETTLEMENTDATE``) is AEST all year: UTC+10, no daylight saving.
NEM_UTC_OFFSET_S = 10 * 3600
APP_CONFIG_KEY = "nem_pricing"
SOURCE = "AEMO NEM"

#: Sensor id -> point key the sensor registry reads.
POINT_KEYS: dict[str, str] = {
    "tariff.spot_price": "tariff_spot_price",
    "tariff.feed_in_price": "tariff_feed_in_price",
}


# ── Pure helpers ──────────────────────────────────────────────


def settlement_epoch(settlement: Any) -> int | None:
    """AEMO ``SETTLEMENTDATE`` (NEM time, ``YYYY-MM-DDTHH:MM:SS``) -> epoch s."""
    if not isinstance(settlement, str):
        return None
    try:
        dt = datetime.strptime(settlement[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return calendar.timegm(dt.timetuple()) - NEM_UTC_OFFSET_S


def parse_summary(data: Any, region: str) -> dict[str, Any] | None:
    """The ``region`` row of an ``ELEC_NEM_SUMMARY`` response, or None."""
    rows = (data or {}).get("ELEC_NEM_SUMMARY") if isinstance(data, dict) else None
    for row in rows or []:
        if not isinstance(row, dict) or row.get("REGIONID") != region:
            continue
        try:
            rrp = float(row["PRICE"])
        except (KeyError, TypeError, ValueError):
            return None
        settlement = row.get("SETTLEMENTDATE")
        return {
            "region": region,
            "rrp_mwh": round(rrp, 2),
            "price_c_kwh": round(rrp / 10.0, 2),
            "settlement": settlement,
            "settlement_ts": settlement_epoch(settlement),
            "source": SOURCE,
            "stale": False,
        }
    return None


def ha_price_c_kwh(state_obj: dict | None) -> float | None:
    """An HA price entity's state in c/kWh. A ``$`` / AUD / dollar unit is
    scaled x100; anything else (``c/kWh``, ``¢/kWh``, unitless) is taken as
    c/kWh. Feed-in may be negative. Unavailable / non-numeric -> None."""
    if not state_obj:
        return None
    try:
        v = float(state_obj.get("state"))
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    unit = str((state_obj.get("attributes") or {}).get("unit_of_measurement") or "")
    u = unit.lower()
    if "$" in unit or u.startswith("aud") or "dollar" in u:
        return round(v * 100.0, 4)
    return round(v, 4)


def resolve_prices(
    spot: dict | None, ha_buy: float | None = None, ha_sell: float | None = None
) -> dict[str, Any]:
    """Live buy/sell in c/kWh. NEM first (sell = buy = spot), then each HA
    price that resolved overwrites its own side. ``source`` is the BUY side's
    source (``"nem"``, ``"ha-entity"`` or None); ``sell_source`` the sell's."""
    buy = sell = None
    buy_src = sell_src = None
    if spot and spot.get("price_c_kwh") is not None:
        buy = sell = spot["price_c_kwh"]
        buy_src = sell_src = "nem"
    if ha_buy is not None:
        buy, buy_src = ha_buy, "ha-entity"
    if ha_sell is not None:
        sell, sell_src = ha_sell, "ha-entity"
    return {
        "source": buy_src,
        "buy_c_kwh": buy,
        "sell_c_kwh": sell,
        "sell_source": sell_src,
    }


# ── Fetch + cache ─────────────────────────────────────────────

Fetcher = Callable[[], Awaitable[dict]]


async def fetch_summary() -> dict:
    """One ``ELEC_NEM_SUMMARY`` request (raises — the client handles it)."""
    async with httpx.AsyncClient(
        timeout=TIMEOUT_S,
        headers={"Accept": "application/json", "User-Agent": "franklinwh-modbus-bridge"},
    ) as client:
        r = await client.get(AEMO_URL)
        r.raise_for_status()
        return r.json()


class NemSpotClient:
    """Per-region spot cache over one AEMO summary request. Never raises."""

    def __init__(
        self, *, fetcher: Fetcher | None = None, clock: Callable[[], float] = time.time
    ) -> None:
        self._fetch = fetcher or fetch_summary
        self._clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}  # region -> (fetched_at, spot)
        self._lock = asyncio.Lock()

    async def spot_price(self, region: str | None, *, force: bool = False) -> dict | None:
        """Current spot for a NEM region: fresh within 240 s, else refetched; on
        failure the last good value flagged ``stale: True``, else None."""
        region = (region or "").upper()
        if region not in REGIONS:
            return None
        async with self._lock:
            now = self._clock()
            hit = self._cache.get(region)
            if not force and hit and now - hit[0] < CACHE_S:
                return hit[1]
            try:
                data = await self._fetch()
                # One request carries every region: cache them all.
                for rg in REGIONS:
                    row = parse_summary(data, rg)
                    if row is not None:
                        self._cache[rg] = (now, row)
                fresh = self._cache.get(region)
                if fresh and fresh[0] == now:
                    return fresh[1]
                logger.warning("AEMO NEM summary has no price for %s", region)
            except Exception as exc:
                logger.warning("AEMO NEM fetch failed: %s", exc)
            return {**hit[1], "stale": True} if hit else None

    def cached(self, region: str | None, *, max_age_s: float | None = None) -> dict | None:
        """Last good spot for ``region`` (no network), ``stale`` once past the
        cache window; None if never fetched or older than ``max_age_s``."""
        hit = self._cache.get((region or "").upper())
        if not hit:
            return None
        age = self._clock() - hit[0]
        if max_age_s is not None and age > max_age_s:
            return None
        return {**hit[1], "stale": True} if age >= CACHE_S else hit[1]


# ── Settings + service ────────────────────────────────────────


@dataclass
class PricingSettings:
    """``nem_region`` "" = off. Entities are scheduler ids ``ha:<inst>:<entity>``
    ("" = none): an HA buy price and an HA feed-in price, both optional."""

    nem_region: str = ""
    tariff_price_entity: str = ""
    tariff_feedin_entity: str = ""


def _validate(key: str, v: Any) -> str:
    s = "" if v is None else str(v).strip()
    if key == "nem_region":
        s = s.upper()
        if s and s not in REGIONS:
            raise ValueError(f"nem_region must be one of {', '.join(REGIONS)} or empty (off)")
        return s
    if s:
        parts = s.split(":", 2)
        if len(parts) != 3 or parts[0] != "ha" or not parts[1] or not parts[2]:
            raise ValueError(f"{key} must be an HA sensor id 'ha:<instance>:<entity_id>' or empty")
    return s


EntityStateFn = Callable[[str], "dict | None"]


class NemPricingService:
    """Pricing settings + the NEM cache; keeps it warm while a region is set."""

    def __init__(
        self,
        db: Any,
        *,
        client: NemSpotClient | None = None,
        entity_state: EntityStateFn | None = None,
    ) -> None:
        self._db = db
        self.client = client or NemSpotClient()
        self._entity_state = entity_state or (lambda _sid: None)
        self.settings = PricingSettings()
        self._wake = asyncio.Event()
        #: The active plan's ``pricing.dynamic`` (None = static plan). Set by
        #: main.py to ``BillingStore.dynamic_config`` (#89 part B).
        self._plan_dynamic: Callable[[], dict | None] = lambda: None

    def set_plan_source(self, fn: Callable[[], dict | None]) -> None:
        self._plan_dynamic = fn

    def wake(self) -> None:
        """Refetch now, e.g. after a plan switched to a NEM region."""
        self._wake.set()

    def plan_dynamic(self) -> dict | None:
        try:
            return self._plan_dynamic()
        except Exception:  # pragma: no cover - defensive
            return None

    def wanted_region(self) -> str:
        """The region the loop keeps warm: the settings' one, else the dynamic
        plan's (one AEMO response caches every region, so one is enough)."""
        if self.settings.nem_region:
            return self.settings.nem_region
        dyn = self.plan_dynamic() or {}
        return (dyn.get("region") or "") if dyn.get("source") == "nem" else ""

    def dynamic_prices(self, dyn: dict | None, *, max_age_s: float | None = None) -> dict:
        """Live buy/sell for a dynamic-plan provider (cache only, no network).
        Default max age is the billing one: an older NEM price prices nothing."""
        from franklinwh_bridge.gateway.dynamic_pricing import BILLING_MAX_AGE_S, live_prices

        dyn = dyn or {}
        age = BILLING_MAX_AGE_S if max_age_s is None else max_age_s
        spot = (
            self.client.cached(dyn.get("region"), max_age_s=age)
            if dyn.get("source") == "nem"
            else None
        )
        return live_prices(dyn, spot, self._entity_state)

    async def preview_prices(self, dyn: dict) -> dict:
        """Like :meth:`dynamic_prices`, but a NEM region is fetched if cold
        (the tariff editor shows a region's price before it is saved, even
        with the settings' region off). ``stale`` marks an old fallback."""
        from franklinwh_bridge.gateway.dynamic_pricing import live_prices

        spot = None
        if (dyn or {}).get("source") == "nem":
            spot = await self.client.spot_price(dyn.get("region"))
        got = live_prices(dyn, spot, self._entity_state)
        return {**got, "stale": bool((spot or {}).get("stale"))}

    def plan_prices(self) -> dict | None:
        """The active dynamic plan's live price, or None for a static plan."""
        dyn = self.plan_dynamic()
        return None if dyn is None else self.dynamic_prices(dyn)

    async def load(self) -> None:
        try:
            raw = await get_app_config(self._db, APP_CONFIG_KEY)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("NEM pricing settings load failed: %s", exc)
            return
        if not raw:
            return
        try:
            stored = json.loads(raw)
        except (ValueError, TypeError):
            return
        for k in asdict(self.settings):
            if k in stored:
                with contextlib.suppress(TypeError, ValueError):
                    setattr(self.settings, k, _validate(k, stored[k]))

    def values(self) -> dict[str, Any]:
        return asdict(self.settings)

    async def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply + persist known keys. Invalid values raise ValueError and
        nothing is changed."""
        fields = asdict(self.settings)
        validated = {k: _validate(k, v) for k, v in updates.items() if k in fields}
        for k, v in validated.items():
            setattr(self.settings, k, v)
        await set_app_config(self._db, APP_CONFIG_KEY, json.dumps(self.values()))
        self._wake.set()  # fetch the new region now, not in 4 minutes
        return self.values()

    def ha_prices(self) -> dict[str, float | None]:
        """The configured HA buy / feed-in prices in c/kWh (cache only)."""
        s = self.settings

        def one(sid: str) -> float | None:
            if not sid:
                return None
            try:
                return ha_price_c_kwh(self._entity_state(sid))
            except Exception:  # pragma: no cover - defensive
                return None

        return {"buy_c_kwh": one(s.tariff_price_entity), "sell_c_kwh": one(s.tariff_feedin_entity)}

    def active(self, spot: dict | None) -> dict[str, Any]:
        ha = self.ha_prices()
        return resolve_prices(spot, ha["buy_c_kwh"], ha["sell_c_kwh"])

    def as_points(self) -> dict[str, Any]:
        """Sensor values as point keys for the (sync) scheduler points merge.
        Cache only — never a network call. None when off / no data / too old."""
        region = self.settings.nem_region
        spot = self.client.cached(region, max_age_s=SENSOR_MAX_AGE_S) if region else None
        act = self.active(spot)
        out: dict[str, Any] = {
            POINT_KEYS["tariff.spot_price"]: act["buy_c_kwh"],
            POINT_KEYS["tariff.feed_in_price"]: act["sell_c_kwh"],
        }
        dyn = self.plan_dynamic()
        if dyn is not None:
            # The active dynamic tariff wins over the settings (the Local
            # Bridge's precedence). Spot/feed-in keep part A's sensor max age;
            # the $/kWh rate is the one billing uses, so the sensor shows the
            # rate the accrual is actually charging.
            live = self.dynamic_prices(dyn, max_age_s=SENSOR_MAX_AGE_S)
            bill = self.dynamic_prices(dyn)
            out.update({
                POINT_KEYS["tariff.spot_price"]: live["buy_c_kwh"],
                POINT_KEYS["tariff.feed_in_price"]: live["sell_c_kwh"],
                "tariff_dynamic_buy": bill["buy"],
                "tariff_dynamic_sell": bill["sell"],
                "tariff_pricing_label": bill["label"],
            })
        return out

    async def run(self) -> None:
        """Keep the cache warm while a region is set. Cancel the task to stop."""
        while True:
            region = self.wanted_region()
            if region:
                try:
                    await self.client.spot_price(region)
                except Exception as exc:  # pragma: no cover - spot_price never raises
                    logger.debug("AEMO NEM refresh failed: %s", exc)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), CACHE_S)
            self._wake.clear()
