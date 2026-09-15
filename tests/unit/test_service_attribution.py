"""Billing history must say WHO supplied it (migration 40).

Demand charges, two-way export, bonus credits and standing charges accrued
against a gateway with no record of the plan that priced them, so switching
retailer left old and new periods indistinguishable — and the only way to
retire a provider was to delete it, taking its history with it.
"""

from __future__ import annotations

import datetime as dt

import pytest

from franklinwh_bridge.store.db import (
    create_service,
    get_service,
    init_db,
    insert_billing_period,
    service_has_history,
    start_new_plan,
    update_service,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "attr.db")
    yield conn
    await conn.close()


def _period(start: float, **kw) -> dict:
    base = {
        "gateway_id": "default", "period_start": start, "period_end": start + 86400,
        "demand_peak_kw": 1.0, "net_total": 10.0, "created_at": start,
    }
    base.update(kw)
    return base


# ── Supplier identity ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_retailer_and_network_are_independent(db):
    """AU: you can switch retailer and stay on the same network, whose two-way
    export tariff applies either way."""
    svc = await create_service(db, name="Home")

    row = await update_service(db, svc["id"], retailer="AGL", network="Ausgrid")
    assert (row["retailer"], row["network"]) == ("AGL", "Ausgrid")

    row = await update_service(db, svc["id"], retailer="Origin")
    assert row["retailer"] == "Origin"
    assert row["network"] == "Ausgrid", "switching retailer must not change the network"


@pytest.mark.asyncio
async def test_period_records_who_supplied_it(db):
    await insert_billing_period(db, _period(
        1000.0, service_id="service1", retailer="AGL", network="Ausgrid",
        plan_version=3,
    ))

    assert await service_has_history(db, "service1") == 1


@pytest.mark.asyncio
async def test_text_columns_are_not_written_as_zero(db):
    """The record defaults are numeric; without per-field defaults a missing
    retailer is stored as the integer 0 and renders as "0" where a name goes."""
    await insert_billing_period(db, _period(2000.0, service_id="s1"))

    from franklinwh_bridge.store.db import get_billing_periods
    row = (await get_billing_periods(db, "default"))[0]

    assert row["retailer"] == ""
    assert row["network"] == ""


# ── Plan boundaries ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_switching_plan_bumps_the_version(db):
    svc = await create_service(db, name="Home")
    await update_service(db, svc["id"], retailer="AGL")

    row = await start_new_plan(db, svc["id"], retailer="Origin")

    assert row["plan_version"] == 2
    assert row["retailer"] == "Origin"
    assert row["plan_started_at"] is not None


@pytest.mark.asyncio
async def test_switching_plan_leaves_history_alone(db):
    """Closed periods keep the retailer and version that produced them, so the
    history still reads as what it was after a switch."""
    svc = await create_service(db, name="Home")
    await update_service(db, svc["id"], retailer="AGL")
    await insert_billing_period(db, _period(
        1000.0, service_id=svc["id"], retailer="AGL", plan_version=1,
    ))

    await start_new_plan(db, svc["id"], retailer="Origin")

    from franklinwh_bridge.store.db import get_billing_periods
    row = (await get_billing_periods(db, "default"))[0]
    assert row["retailer"] == "AGL", "history must not be rewritten by a switch"
    assert row["plan_version"] == 1


@pytest.mark.asyncio
async def test_a_re_contract_can_keep_the_same_retailer(db):
    """A new plan with the same supplier is as common as changing supplier."""
    svc = await create_service(db, name="Home")
    await update_service(db, svc["id"], retailer="AGL")

    row = await start_new_plan(db, svc["id"])

    assert row["plan_version"] == 2
    assert row["retailer"] == "AGL"


# ── Retire, don't delete ──────────────────────────────────────


@pytest.mark.asyncio
async def test_a_retired_service_keeps_its_history(db):
    svc = await create_service(db, name="Old provider")
    await insert_billing_period(db, _period(1000.0, service_id=svc["id"]))

    await update_service(db, svc["id"], enabled=0)

    assert (await get_service(db, svc["id"]))["enabled"] == 0
    assert await service_has_history(db, svc["id"]) == 1


@pytest.mark.asyncio
async def test_a_service_with_no_history_reports_none(db):
    svc = await create_service(db, name="Fresh")

    assert await service_has_history(db, svc["id"]) == 0


@pytest.mark.asyncio
async def test_a_disabled_service_stops_pricing(db):
    """BillingStore must skip a retired service, or a provider you left would
    keep pricing today's energy."""
    from franklinwh_bridge.gateway.billing import BillingStore

    svc = await create_service(db, name="Old")
    await update_service(db, svc["id"], retailer="AGL", enabled=0)

    store = BillingStore(db)
    await store.load()

    assert store.plan()["retailer"] != "AGL"


# ── Connection vs retail ──────────────────────────────────────
# PTO and the approved export limit are the NETWORK's grant against the
# connection point. They are not terms of the retail plan and must outlive it.


@pytest.mark.asyncio
async def test_switching_retailer_preserves_the_network_grant(db):
    """Switching AGL -> Origin leaves the Ausgrid approval alone. Previously
    this held only because start_new_plan happened not to mention the fields."""
    svc = await create_service(db, name="Home")
    await update_service(
        db, svc["id"], retailer="AGL", network="Ausgrid",
        pto_status="approved", pto_reference="PTO-12345", export_limit_kw=5.0,
    )

    await start_new_plan(db, svc["id"], retailer="Origin")

    row = await get_service(db, svc["id"])
    assert row["retailer"] == "Origin", "the retailer did change"
    assert row["network"] == "Ausgrid"
    assert row["pto_status"] == "approved"
    assert row["pto_reference"] == "PTO-12345"
    assert row["export_limit_kw"] == 5.0


@pytest.mark.asyncio
async def test_pto_defaults_to_unknown_not_approved(db):
    """An unrecorded approval must never read as granted."""
    svc = await create_service(db, name="Fresh")

    assert (await get_service(db, svc["id"]))["pto_status"] == "unknown"


@pytest.mark.asyncio
async def test_pto_sensor_treats_unknown_as_unknown(db):
    """service.pto_approved is None when unrecorded — not False, which would
    read as 'refused', and not True, which would invite exporting."""
    from franklinwh_bridge.gateway.scheduler_sensors import snapshot

    now = dt.datetime(2026, 1, 1)

    def approved(status):
        return snapshot({"service_pto_status": status}, now)["service.pto_approved"]

    assert approved("unknown") is None
    assert approved("approved") is True
    assert approved("pending") is False


# ── Solar export vs battery export ────────────────────────────
# Different permissions with different sources. A subsidy commonly bans
# exporting battery energy while solar export stays permitted, and it is
# BATTERY export that a force-discharge to grid depends on.


@pytest.mark.asyncio
async def test_battery_export_can_be_banned_while_solar_is_allowed(db):
    """The subsidy case, which one export flag could not express."""
    svc = await create_service(db, name="Rebated")

    row = await update_service(
        db, svc["id"], solar_export_allowed=1, battery_export_allowed=0,
        export_restriction_note="battery rebate — no export of subsidised storage",
    )

    assert row["solar_export_allowed"] == 1
    assert row["battery_export_allowed"] == 0
    assert "rebate" in row["export_restriction_note"]


@pytest.mark.asyncio
async def test_both_default_to_permitted(db):
    svc = await create_service(db, name="Fresh")
    row = await get_service(db, svc["id"])

    assert row["solar_export_allowed"] == 1
    assert row["battery_export_allowed"] == 1


@pytest.mark.asyncio
async def test_the_sensors_report_them_separately(db):
    """Updates the SEEDED service, not a new one: BillingStore takes its plan
    from the FIRST service (the v1 single-service assumption), so a second
    service would leave the first still driving every sensor."""
    from franklinwh_bridge.gateway.billing import BillingStore
    from franklinwh_bridge.store.db import get_services

    first = (await get_services(db))[0]
    await update_service(db, first["id"], solar_export_allowed=1, battery_export_allowed=0)

    store = BillingStore(db)
    await store.load()
    pts = store.as_points()

    assert pts["service_solar_export_allowed"] is True
    assert pts["service_battery_export_allowed"] is False
