"""Utility-service billing params + tariff-window sensors."""

from __future__ import annotations

from datetime import datetime

import pytest

from franklinwh_bridge.gateway.billing import BillingStore
from franklinwh_bridge.gateway.scheduler_sensors import _in_window, snapshot
from franklinwh_bridge.store.db import (
    CURRENT_SCHEMA_VERSION,
    create_service,
    get_schema_version,
    init_db,
    update_service,
)


@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "b.db")
    yield conn
    await conn.close()


async def test_migration_and_billing_roundtrip(db):
    assert await get_schema_version(db) == CURRENT_SCHEMA_VERSION >= 33
    s = await create_service(db, "Svc")
    assert s["has_peak_demand"] == 0 and s["demand_window"] is None  # defaults
    win = {"months": [1, 2, 12], "days": [0, 1, 2, 3, 4], "start": "14:00", "end": "20:00"}
    r = await update_service(
        db, s["id"], has_peak_demand=True, min_monthly_bill=30.5, pricing_api="ha",
        demand_window=win,
    )
    assert r["has_peak_demand"] == 1
    assert r["min_monthly_bill"] == 30.5
    assert r["pricing_api"] == "ha"
    assert r["demand_window"] == win  # JSON decoded back to a dict
    # clearing a window persists as NULL
    r2 = await update_service(db, s["id"], demand_window=None)
    assert r2["demand_window"] is None


def test_in_window():
    w = {"months": [1, 2, 12], "days": [0, 1, 2, 3, 4], "start": "14:00", "end": "20:00"}
    assert _in_window(w, datetime(2026, 1, 5, 15, 0)) is True    # Mon Jan 15:00
    assert _in_window(w, datetime(2026, 1, 3, 15, 0)) is False   # Sat
    assert _in_window(w, datetime(2026, 1, 5, 21, 0)) is False   # after 20:00
    assert _in_window(w, datetime(2026, 6, 1, 15, 0)) is False   # wrong month
    # overnight wrap + empty months/days = all
    w2 = {"start": "22:00", "end": "06:00"}
    assert _in_window(w2, datetime(2026, 1, 1, 23, 0)) is True
    assert _in_window(w2, datetime(2026, 1, 1, 3, 0)) is True
    assert _in_window(w2, datetime(2026, 1, 1, 12, 0)) is False


async def test_billing_store_and_sensor(db):
    s = await create_service(db, "Svc")
    await update_service(
        db, s["id"], has_peak_demand=True, has_export_bonus=True,
        demand_window={"start": "00:00", "end": "23:59"},
        bonus_window={"months": [6], "start": "00:00", "end": "23:59"},
    )
    store = BillingStore(db)
    await store.load()
    pts = store.as_points()
    assert len(pts["tariff_demand_windows"]) == 1
    # demand active any time today; bonus only in June
    snap = snapshot(pts, datetime(2026, 3, 10, 12, 0))
    assert snap["tariff.demand_window_active"] is True
    assert snap["tariff.bonus_window_active"] is False
    assert snapshot(pts, datetime(2026, 6, 10, 12, 0))["tariff.bonus_window_active"] is True
