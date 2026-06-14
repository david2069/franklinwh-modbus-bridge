"""Per-phase utilisation detection + declared-vs-detected matching."""

import pytest

from franklinwh_bridge.gateway.phase_detect import detect_phases, phase_matches
from franklinwh_bridge.store.db import create_gateway, get_gateway, init_db, update_gateway

# ── detect_phases ───────────────────────────────────────────────

def test_single_phase_l1():
    pts = {
        "701.VL1": 2440, "701.TotWhInjL1": 4644848, "701.TotWhAbsL1": 1597894,
        "701.VL2": 0, "701.TotWhInjL2": 0, "701.TotWhAbsL2": 0,
        "701.VL3": 0, "701.TotWhInjL3": 0, "701.TotWhAbsL3": 0,
    }
    out = detect_phases(pts)
    assert out["detected"] == "L1"
    assert out["phases"]["L1"]["connected"] is True
    assert out["phases"]["L1"]["utilised"] is True
    assert out["phases"]["L2"]["connected"] is False
    assert out["has_data"] is True


def test_three_phase():
    pts = {
        "701.VL1": 2400, "701.TotWhInjL1": 100,
        "701.VL2": 2400, "701.TotWhInjL2": 200,
        "701.VL3": 2400, "701.TotWhInjL3": 300,
    }
    assert detect_phases(pts)["detected"] == "L1+L2+L3"


def test_connected_but_no_energy_still_detected():
    # Voltage present, energy zero → still "connected" (wired), so detected.
    pts = {"701.VL2": 2400, "701.TotWhInjL2": 0, "701.TotWhAbsL2": 0}
    out = detect_phases(pts)
    assert out["detected"] == "L2"
    assert out["phases"]["L2"]["connected"] is True
    assert out["phases"]["L2"]["utilised"] is False


def test_no_data_returns_none():
    out = detect_phases({})
    assert out["detected"] is None
    assert out["has_data"] is False


def test_non_numeric_values_are_safe():
    out = detect_phases({"701.VL1": None, "701.TotWhInjL1": "oops"})
    assert out["detected"] is None  # None voltage / unparseable energy → not detected


# ── phase_matches ───────────────────────────────────────────────

@pytest.mark.parametrize("declared,detected,expected", [
    ("all", "L1", True),       # 'all' asserts nothing → always matches
    ("", "L1", True),
    ("L1", "L1", True),
    ("L1", "L2", False),       # real mismatch
    ("L1+L2", "L2+L1", True),  # order-insensitive
    ("L1", None, True),        # no detection → nothing to contradict
])
def test_phase_matches(declared, detected, expected):
    assert phase_matches(declared, detected) is expected


# ── linkage persistence (migration 14) ──────────────────────────

@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "link.db")
    yield conn
    await conn.close()


async def test_gateway_phase_defaults_to_all(db):
    await create_gateway(db, "gw1", "GW1", host="1.2.3.4")
    row = await get_gateway(db, "gw1")
    assert row["phase"] == "all"
    assert row["service_id"] is None


async def test_gateway_linkage_persists(db):
    await create_gateway(db, "gw1", "GW1", host="1.2.3.4")
    await update_gateway(db, "gw1", service_id="svc_abc", phase="L1")
    row = await get_gateway(db, "gw1")
    assert row["service_id"] == "svc_abc"
    assert row["phase"] == "L1"
