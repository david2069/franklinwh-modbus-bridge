"""MP4 — per-gateway phase_view preference (Topology B) entity filtering."""

import pytest

from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher
from franklinwh_bridge.store.db import create_gateway, get_gateway, init_db


def _phase_slugs(pub):
    return {e.slug for e in pub.entities if e.phase is not None}


# ── publisher filtering ─────────────────────────────────────────

def test_default_both_keeps_per_phase_on_three_phase():
    pub = MqttPublisher()
    pub.set_ac_type(2)  # three-phase
    # default phase_view is 'both' → per-phase entities present
    assert _phase_slugs(pub)


def test_aggregate_suppresses_per_phase_on_three_phase():
    pub = MqttPublisher()
    pub.set_ac_type(2)
    pub.set_phase_view("aggregate")
    assert _phase_slugs(pub) == set()
    # non-phase entities are untouched
    assert any(e.phase is None for e in pub.entities)


def test_aggregate_does_not_strip_single_phase_l1():
    """A single-phase unit keeps its L1 entities even in 'aggregate' view —
    that L1 data IS its real measurement, not a redundant breakdown."""
    pub = MqttPublisher()
    pub.set_ac_type(0)  # single-phase → only L1 per-phase entities
    before = _phase_slugs(pub)
    pub.set_phase_view("aggregate")
    assert _phase_slugs(pub) == before  # unchanged
    assert before  # there is at least one L1 entity


def test_per_phase_keeps_per_phase():
    pub = MqttPublisher()
    pub.set_ac_type(2)
    pub.set_phase_view("per_phase")
    assert _phase_slugs(pub)


def test_invalid_view_falls_back_to_both():
    pub = MqttPublisher()
    pub.set_ac_type(2)
    pub.set_phase_view("nonsense")
    assert pub._phase_view == "both"
    assert _phase_slugs(pub)


def test_toggle_back_to_both_restores_per_phase():
    pub = MqttPublisher()
    pub.set_ac_type(2)
    full = _phase_slugs(pub)
    pub.set_phase_view("aggregate")
    assert _phase_slugs(pub) == set()
    pub.set_phase_view("both")
    assert _phase_slugs(pub) == full


# ── migration 15 default ────────────────────────────────────────

@pytest.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "pv.db")
    yield conn
    await conn.close()


async def test_gateway_phase_view_defaults_to_both(db):
    await create_gateway(db, "gw1", "GW1", host="1.2.3.4")
    row = await get_gateway(db, "gw1")
    assert row["phase_view"] == "both"
