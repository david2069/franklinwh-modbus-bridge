"""Tests for Sample dataclass and SampleBus."""

from franklinwh_bridge.modbus.sample import Sample, SampleBus


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
