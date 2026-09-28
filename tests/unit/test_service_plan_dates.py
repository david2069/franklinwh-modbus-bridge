"""Tariff plans have a date range, and a switch can be backdated.

The motivating case is the owner's own account: **Amber Electric until 09 Sep
2026, AGL Energy from then** — a mid-month switch against a cycle that rolls on
the 1st. Before migration 45 a service row held only the CURRENT plan and
start_new_plan() overwrote retailer/network on it, so the previous plan survived
nowhere except inside already-closed billing_periods snapshots, and
plan_started_at was stamped "now". Neither "Amber ran from X to Y" nor "AGL
actually started on the 9th" could be expressed, so a period spanning the switch
was priced entirely by whichever plan happened to be current.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from franklinwh_bridge.store.db import (
    create_service,
    get_service_plans,
    init_db,
    plan_at,
    plans_overlapping,
    record_plan_change,
    update_service,
)


def ts(y: int, m: int, d: int) -> float:
    return dt.datetime(y, m, d).timestamp()


SWITCH = ts(2026, 9, 9)          # Amber -> AGL
SEP_START = ts(2026, 9, 1)       # billing cycle rolls on the 1st
OCT_START = ts(2026, 10, 1)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "plans.db")
    yield conn
    await conn.close()


@pytest.fixture
async def svc(db):
    """A service that already existed under Amber before the switch.

    create_service() opens a plan at "now", which is the truth for a service
    added today — but the case under test is one that predates the switch, so
    the opening plan is backdated to August the way a real install would have
    recorded it at the time.
    """
    s = await create_service(db, name="Home")
    await update_service(db, s["id"], retailer="Amber Electric", network="Ausgrid")
    await db.execute(
        "UPDATE service_plans SET valid_from = ?, retailer = ?, network = ? "
        "WHERE service_id = ?",
        (ts(2026, 8, 1), "Amber Electric", "Ausgrid", s["id"]),
    )
    await db.commit()
    return s["id"]


# ── Backfill ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_existing_services_get_an_open_plan(db):
    """Migration 45 backfills, so resolution works from day one rather than
    reporting "no plan" for every historical timestamp."""
    plans = await get_service_plans(db, (await create_service(db, name="X"))["id"])

    # A service created after migration still needs one; the seeded service
    # from init_db is the one the backfill covered.
    from franklinwh_bridge.store.db import get_services
    seeded = (await get_services(db))[0]["id"]
    assert len(await get_service_plans(db, seeded)) == 1
    assert plans is not None


@pytest.mark.asyncio
async def test_the_backfilled_plan_is_open_ended(db):
    from franklinwh_bridge.store.db import get_services
    seeded = (await get_services(db))[0]["id"]

    plan = (await get_service_plans(db, seeded))[0]

    assert plan["valid_to"] is None, "the current plan has not ended"


# ── The Amber -> AGL switch ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_switch_can_be_backdated(db, svc):
    """The switch is recorded after it happened — that is the normal case, and
    the reason stamping "now" was wrong."""
    new = await record_plan_change(
        db, svc, valid_from=SWITCH, retailer="AGL Energy", network="Ausgrid",
    )

    assert new["retailer"] == "AGL Energy"
    assert new["valid_from"] == SWITCH


@pytest.mark.asyncio
async def test_the_previous_plan_is_closed_at_the_switch(db, svc):
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    plans = await get_service_plans(db, svc)

    assert len(plans) == 2
    assert plans[0]["retailer"] == "Amber Electric"
    assert plans[0]["valid_to"] == SWITCH, "abuts the successor exactly"
    assert plans[1]["valid_to"] is None


@pytest.mark.asyncio
async def test_each_instant_resolves_to_the_plan_actually_in_force(db, svc):
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    before = await plan_at(db, svc, ts(2026, 9, 5))
    after = await plan_at(db, svc, ts(2026, 9, 20))

    assert before["retailer"] == "Amber Electric"
    assert after["retailer"] == "AGL Energy"


@pytest.mark.asyncio
async def test_the_boundary_instant_belongs_to_the_successor(db, svc):
    """Half-open ranges: valid_from <= ts < valid_to. Otherwise the instant of
    the switch is counted by both plans."""
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    assert (await plan_at(db, svc, SWITCH))["retailer"] == "AGL Energy"


@pytest.mark.asyncio
async def test_no_gap_between_consecutive_plans(db, svc):
    """Every instant from the first plan's start onwards must resolve."""
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")
    plans = await get_service_plans(db, svc)

    assert plans[0]["valid_to"] == plans[1]["valid_from"]


# ── The point of all this: periods that span a switch ─────────


@pytest.mark.asyncio
async def test_a_period_spanning_the_switch_reports_both_plans(db, svc):
    """September 1-30 contains the 9th. Previously this was priced entirely by
    whichever plan was current, silently attributing Amber's first nine days to
    AGL."""
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    spanning = await plans_overlapping(db, svc, SEP_START, OCT_START)

    assert [p["retailer"] for p in spanning] == ["Amber Electric", "AGL Energy"]


@pytest.mark.asyncio
async def test_a_period_wholly_inside_one_plan_reports_one(db, svc):
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    october = await plans_overlapping(db, svc, OCT_START, ts(2026, 11, 1))

    assert len(october) == 1
    assert october[0]["retailer"] == "AGL Energy"


@pytest.mark.asyncio
async def test_a_period_ending_exactly_at_the_switch_does_not_include_the_successor(db, svc):
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    before = await plans_overlapping(db, svc, SEP_START, SWITCH)

    assert [p["retailer"] for p in before] == ["Amber Electric"]


# ── Refusals and edges ────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_plan_starting_exactly_when_its_predecessor_did_is_refused(db, svc):
    """That would close a plan before it opened — a zero-length range that can
    never have been true. Refuse rather than write it."""
    with pytest.raises(ValueError, match="overlap"):
        await record_plan_change(db, svc, valid_from=ts(2026, 8, 1), retailer="Origin")


@pytest.mark.asyncio
async def test_a_plan_can_be_inserted_BETWEEN_two_existing_ones(db, svc):
    """Correcting history you did not capture at the time. Origin ran 02-09 Sep
    between Amber and AGL: Amber is closed at the 2nd, Origin is bounded by
    AGL's start rather than left open, and all three resolve independently."""
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    await record_plan_change(db, svc, valid_from=ts(2026, 9, 2), retailer="Origin")

    assert (await plan_at(db, svc, ts(2026, 8, 20)))["retailer"] == "Amber Electric"
    assert (await plan_at(db, svc, ts(2026, 9, 5)))["retailer"] == "Origin"
    assert (await plan_at(db, svc, ts(2026, 9, 20)))["retailer"] == "AGL Energy"
    origin = [p for p in await get_service_plans(db, svc) if p["retailer"] == "Origin"][0]
    assert origin["valid_to"] == SWITCH, "bounded by its successor, not left open"


@pytest.mark.asyncio
async def test_an_instant_before_any_plan_resolves_to_nothing(db, svc):
    """None, not the nearest plan: pricing energy with a tariff that was not in
    force is worse than declining to price it."""
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    assert await plan_at(db, svc, ts(1999, 1, 1)) is None


@pytest.mark.asyncio
async def test_an_unknown_service_yields_nothing(db):
    assert await record_plan_change(db, "nope", valid_from=SWITCH) is None
    assert await plan_at(db, "nope", SWITCH) is None


@pytest.mark.asyncio
async def test_rates_are_snapshotted_so_history_is_not_rewritten(db, svc):
    """Editing today's prices must not change what a closed period was charged."""
    await record_plan_change(
        db, svc, valid_from=SWITCH, retailer="AGL Energy", pricing='{"buy": 0.30}',
    )
    await update_service(db, svc, pricing='{"buy": 0.99}')

    assert (await plan_at(db, svc, ts(2026, 9, 20)))["pricing"] == '{"buy": 0.30}'


@pytest.mark.asyncio
async def test_fields_not_supplied_carry_forward_from_the_service(db, svc):
    """A retailer switch on the same network should not require restating it."""
    plan = await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    assert plan["network"] == "Ausgrid"


@pytest.mark.asyncio
async def test_pricing_carries_forward_when_the_service_already_has_some(db, svc):
    """The carry-forward reads pricing back DECODED, so it must be re-encoded.

    get_service() json-decodes the column, so inheriting it hands a dict to the
    driver — which refuses to bind it. Only reproducible when the service
    actually has pricing, which is why the plain carry-forward test missed it.
    """
    await update_service(db, svc, pricing='{"buy": 0.42}')

    plan = await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")

    assert isinstance(plan["pricing"], str)
    assert json.loads(plan["pricing"]) == {"buy": 0.42}


@pytest.mark.asyncio
async def test_each_switch_bumps_the_version(db, svc):
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")
    await update_service(db, svc, plan_version=2)
    second = await record_plan_change(db, svc, valid_from=OCT_START, retailer="Origin")

    assert second["plan_version"] == 3


@pytest.mark.asyncio
async def test_three_plans_resolve_independently(db, svc):
    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")
    await update_service(db, svc, plan_version=2)
    await record_plan_change(db, svc, valid_from=OCT_START, retailer="Origin")

    assert (await plan_at(db, svc, ts(2026, 9, 5)))["retailer"] == "Amber Electric"
    assert (await plan_at(db, svc, ts(2026, 9, 20)))["retailer"] == "AGL Energy"
    assert (await plan_at(db, svc, ts(2026, 10, 15)))["retailer"] == "Origin"


# ── Correcting a start date ───────────────────────────────────


@pytest.mark.asyncio
async def test_a_backfilled_plan_start_can_be_corrected(db, svc):
    """Migration 45 backfills valid_from = 0 when a service never recorded when
    its plan began. That covers all prior time — deliberately, so history
    resolves — but leaves nowhere to insert a predecessor. The owner's live
    service1 is in exactly this state."""
    from franklinwh_bridge.store.db import correct_plan_start

    plan = (await get_service_plans(db, svc))[0]
    await db.execute("UPDATE service_plans SET valid_from = 0 WHERE id = ?", (plan["id"],))
    await db.commit()

    fixed = await correct_plan_start(db, plan["id"], SWITCH)

    assert fixed["valid_from"] == SWITCH
    assert await plan_at(db, svc, ts(2026, 8, 20)) is None, "no longer claims August"


@pytest.mark.asyncio
async def test_correcting_a_start_keeps_the_predecessor_abutting(db, svc):
    """Moving a start must not open a gap where no plan resolves."""
    from franklinwh_bridge.store.db import correct_plan_start

    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")
    agl = [p for p in await get_service_plans(db, svc) if p["retailer"] == "AGL Energy"][0]

    await correct_plan_start(db, agl["id"], ts(2026, 9, 15))

    plans = await get_service_plans(db, svc)
    assert plans[0]["valid_to"] == ts(2026, 9, 15), "Amber follows AGL's new start"
    assert (await plan_at(db, svc, ts(2026, 9, 12)))["retailer"] == "Amber Electric"


@pytest.mark.asyncio
async def test_a_start_after_its_own_end_is_refused(db, svc):
    from franklinwh_bridge.store.db import correct_plan_start

    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")
    amber = (await get_service_plans(db, svc))[0]

    with pytest.raises(ValueError, match="not before"):
        await correct_plan_start(db, amber["id"], ts(2026, 10, 1))


# ── Every plan needs a real start date ────────────────────────


@pytest.mark.asyncio
async def test_migration_46_replaces_epoch_zero_with_a_real_start(db, svc):
    """Migration 45 used 0 for "start unknown", which asserts the plan was in
    force in 1970 and would price historical queries against a tariff nobody
    was on. 46 resolves it to when billing tracking actually began."""
    from franklinwh_bridge.store.db import MIGRATIONS, get_services

    plan = (await get_service_plans(db, svc))[0]
    await db.execute("UPDATE service_plans SET valid_from = 0 WHERE id = ?", (plan["id"],))
    await db.execute(
        "INSERT INTO billing_periods (gateway_id, period_start, period_end, created_at) "
        "VALUES ('default', ?, ?, 0)",
        (ts(2026, 8, 1), ts(2026, 9, 1)),
    )
    await db.commit()

    await db.executescript(MIGRATIONS[46])
    await db.commit()

    fixed = (await get_service_plans(db, svc))[0]
    assert fixed["valid_from"] == ts(2026, 8, 1), "earliest billing period"
    assert await get_services(db) is not None


@pytest.mark.asyncio
async def test_migration_46_falls_back_to_now_when_there_is_nothing_to_go_on(db, svc):
    """No plan start, no billing periods, no creation time. "Now" is the
    earliest defensible claim — the moment the feature arrived."""
    import time as _time

    from franklinwh_bridge.store.db import MIGRATIONS

    plan = (await get_service_plans(db, svc))[0]
    await db.execute("UPDATE service_plans SET valid_from = 0 WHERE id = ?", (plan["id"],))
    await db.execute("UPDATE services SET created_at = 0, plan_started_at = 0 WHERE id = ?", (svc,))
    await db.commit()

    await db.executescript(MIGRATIONS[46])
    await db.commit()

    got = (await get_service_plans(db, svc))[0]["valid_from"]
    assert abs(got - _time.time()) < 120, got


@pytest.mark.asyncio
async def test_migration_46_leaves_real_dates_alone(db, svc):
    from franklinwh_bridge.store.db import MIGRATIONS

    await record_plan_change(db, svc, valid_from=SWITCH, retailer="AGL Energy")
    before = await get_service_plans(db, svc)

    await db.executescript(MIGRATIONS[46])
    await db.commit()

    assert [p["valid_from"] for p in await get_service_plans(db, svc)] == \
           [p["valid_from"] for p in before]


@pytest.mark.asyncio
async def test_a_plan_cannot_be_created_without_a_real_start(db, svc):
    """Accepting 0 would just recreate what 46 exists to clean up."""
    for bad in (0, 0.0, None):
        with pytest.raises(ValueError, match="real timestamp"):
            await record_plan_change(db, svc, valid_from=bad, retailer="X")


@pytest.mark.asyncio
async def test_a_start_cannot_be_corrected_to_zero(db, svc):
    from franklinwh_bridge.store.db import correct_plan_start

    plan = (await get_service_plans(db, svc))[0]

    with pytest.raises(ValueError, match="real timestamp"):
        await correct_plan_start(db, plan["id"], 0)
