"""Tests for Sample dataclass and SampleBus."""

import time

from franklinwh_bridge.modbus.sample import STICKY_TTL_SECONDS, Sample, SampleBus


def test_sample_creation():
    s = Sample(gateway_id="gw1", ts=1000.0, points={"p1": 42}, quality="ok")
    assert s.gateway_id == "gw1"
    assert s.points["p1"] == 42


def test_sample_now():
    s = Sample.now("gw1", {"voltage": 240.5})
    assert s.gateway_id == "gw1"
    assert s.ts > 0
    assert s.quality == "ok"


async def test_sample_bus_publish():
    bus = SampleBus()
    received = []

    async def on_sample(sample: Sample):
        received.append(sample)

    bus.subscribe(on_sample)
    sample = Sample.now("gw1", {"v": 1})
    await bus.publish(sample)
    assert len(received) == 1
    assert received[0] is sample


async def test_sample_bus_last_sample():
    bus = SampleBus()
    assert bus.last_sample is None
    sample = Sample.now("gw1", {"v": 1})
    await bus.publish(sample)
    assert bus.last_sample is sample


async def test_sample_bus_multiple_subscribers():
    bus = SampleBus()
    counts = [0, 0]

    async def cb0(s):
        counts[0] += 1

    async def cb1(s):
        counts[1] += 1

    bus.subscribe(cb0)
    bus.subscribe(cb1)
    await bus.publish(Sample.now("gw1", {}))
    assert counts == [1, 1]


async def test_sample_bus_unsubscribe():
    bus = SampleBus()
    received = []

    async def on_sample(s):
        received.append(s)

    bus.subscribe(on_sample)
    await bus.publish(Sample.now("gw1", {"a": 1}))
    assert len(received) == 1

    bus.unsubscribe(on_sample)
    await bus.publish(Sample.now("gw1", {"b": 2}))
    assert len(received) == 1


async def test_sample_bus_handles_subscriber_error():
    bus = SampleBus()
    good_received = []

    async def bad_cb(s):
        raise ValueError("boom")

    async def good_cb(s):
        good_received.append(s)

    bus.subscribe(bad_cb)
    bus.subscribe(good_cb)
    await bus.publish(Sample.now("gw1", {}))
    assert len(good_received) == 1


# ── Sticky overlay tests ──────────────────────────────────────


async def test_sticky_points_survive_publish():
    """Sticky points should appear in the sample after a new publish."""
    bus = SampleBus()
    bus.inject_sticky({"703.SN": "ABC123", "703.Md": "aGate"})
    assert bus.sticky_count == 2

    # Simulate a poll that doesn't include model 703
    await bus.publish(Sample.now("gw1", {"soc": 85, "voltage_v": 240}))

    last = bus.last_sample
    assert last is not None
    assert last.points["soc"] == 85
    assert last.points["703.SN"] == "ABC123"
    assert last.points["703.Md"] == "aGate"


async def test_sticky_poll_wins_on_conflict():
    """When poll data and sticky have the same key, poll data wins."""
    bus = SampleBus()
    bus.inject_sticky({"soc": 50, "703.SN": "ABC"})

    await bus.publish(Sample.now("gw1", {"soc": 85}))

    last = bus.last_sample
    assert last is not None
    assert last.points["soc"] == 85  # poll wins
    assert last.points["703.SN"] == "ABC"  # sticky preserved


async def test_sticky_expires_after_ttl():
    """Sticky points should be purged after TTL elapses."""
    bus = SampleBus()
    bus.inject_sticky({"703.SN": "OLD"})

    # Fast-forward past TTL
    expired_ts = time.time() - STICKY_TTL_SECONDS - 1
    bus._sticky_points["703.SN"] = ("OLD", expired_ts)

    await bus.publish(Sample.now("gw1", {"soc": 85}))

    last = bus.last_sample
    assert last is not None
    assert "703.SN" not in last.points
    assert bus.sticky_count == 0


async def test_sticky_partial_expiry():
    """Only expired sticky points are purged; fresh ones survive."""
    bus = SampleBus()
    now = time.time()
    bus._sticky_points["703.SN"] = ("OLD", now - STICKY_TTL_SECONDS - 1)
    bus._sticky_points["703.Md"] = ("FRESH", now)

    await bus.publish(Sample.now("gw1", {"soc": 85}))

    last = bus.last_sample
    assert last is not None
    assert "703.SN" not in last.points  # expired
    assert last.points["703.Md"] == "FRESH"  # still alive
    assert bus.sticky_count == 1


def test_clear_sticky_all():
    """clear_sticky() with no prefix removes everything."""
    bus = SampleBus()
    bus.inject_sticky({"703.SN": "A", "701.W": 100})
    assert bus.sticky_count == 2
    removed = bus.clear_sticky()
    assert removed == 2
    assert bus.sticky_count == 0


def test_clear_sticky_prefix():
    """clear_sticky(prefix) only removes matching keys."""
    bus = SampleBus()
    bus.inject_sticky({"703.SN": "A", "703.Md": "B", "701.W": 100})
    removed = bus.clear_sticky(prefix="703.")
    assert removed == 2
    assert bus.sticky_count == 1
    assert "701.W" in {k for k in bus._sticky_points}


async def test_sticky_survives_multiple_publishes():
    """Sticky points persist across multiple poll cycles."""
    bus = SampleBus()
    bus.inject_sticky({"703.SN": "ABC123"})

    # Three consecutive polls
    for i in range(3):
        await bus.publish(Sample.now("gw1", {"soc": 80 + i}))
        assert bus.last_sample is not None
        assert bus.last_sample.points["703.SN"] == "ABC123"
        assert bus.last_sample.points["soc"] == 80 + i
