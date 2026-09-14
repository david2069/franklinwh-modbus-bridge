"""A gateway must never publish under another gateway's MQTT topics.

Regression cover for 2026-09-13, observed live on the broker: a mock gateway's
SoC (28%) and a real aGate's (82.4%) alternating on the SAME topic,
``franklinwh/00000001/battery/battery_soc``, flip-flopping the Home Assistant
entity on every poll.

Cause: queue_sample() fell back to the single-device path for ANY gateway with
no registered device, so a second gateway published as the first.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo


def _sample(gateway_id: str, soc: float):
    return SimpleNamespace(gateway_id=gateway_id, points={"soc": soc}, ts=0.0)


@pytest.fixture
def publisher():
    """A publisher configured for the default gateway, with queueing stubbed
    so the test observes topics rather than a live broker."""
    from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher

    pub = MqttPublisher.__new__(MqttPublisher)
    pub._gateway_id = "default"
    pub._devices = {}
    pub._command_handler = None
    pub._device_info = DeviceInfo(serial="10060006A02F00000001", gateway_id="default")
    pub._entities = [
        SimpleNamespace(
            stat_key="soc",
            slug="battery_soc",
            state_topic=lambda sid: f"franklinwh/{sid}/battery/battery_soc",
            unique_id=lambda sid: f"franklinwh_{sid}_battery_soc",
            format_value=lambda v: str(v),
        )
    ]
    # The publisher enqueues MqttMessage objects; a real asyncio.Queue lets the
    # test read exactly what would have gone to the broker.
    pub._queue = asyncio.Queue()
    return pub


def _topics(pub):
    out = []
    while not pub._queue.empty():
        msg = pub._queue.get_nowait()
        out.append((msg.topic, msg.payload))
    return out


@pytest.mark.asyncio
async def test_default_gateway_still_publishes(publisher):
    """The single-device fallback must keep working for its own gateway."""
    await publisher.queue_sample(_sample("default", 82.4))

    topics = [t for t, _ in _topics(publisher)]
    assert any("00000001/battery/battery_soc" in t for t in topics), topics


@pytest.mark.asyncio
async def test_second_gateway_does_not_publish_as_the_first(publisher):
    """The actual defect: a mock gateway's sample reaching the real aGate's topic."""
    await publisher.queue_sample(_sample("Mock GW 1", 28.0))

    captured = _topics(publisher)
    assert captured == [], (
        "a gateway with no registered device published anyway — "
        f"topics: {[t for t, _ in captured]}"
    )


@pytest.mark.asyncio
async def test_the_two_gateways_never_share_a_topic(publisher):
    """End-to-end shape of the bug report: interleaved samples, one topic."""
    for _ in range(3):
        await publisher.queue_sample(_sample("default", 82.4))
        await publisher.queue_sample(_sample("Mock GW 1", 28.0))

    soc_topic = "franklinwh/00000001/battery/battery_soc"
    captured = _topics(publisher)
    values = {p for t, p in captured if t == soc_topic}
    assert values == {"82.4"}, f"topic carried more than one gateway's values: {values}"


@pytest.mark.asyncio
async def test_registered_second_gateway_gets_its_own_topic(publisher):
    """Once a device IS registered, the gateway publishes under its own id."""
    dev_info = DeviceInfo(serial="MOCK-Mock GW 1", gateway_id="Mock GW 1")
    publisher._devices["Mock GW 1"] = SimpleNamespace(
        gateway_id="Mock GW 1",
        device_info=dev_info,
        command_handler=None,
        entities=publisher._entities,
    )

    await publisher.queue_sample(_sample("Mock GW 1", 28.0))

    topics = [t for t, _ in _topics(publisher)]
    assert topics, "registered gateway published nothing"
    assert all("/00000001/" not in t for t in topics), topics


def test_short_id_namespaces_non_default_gateways():
    """The namespacing the topics depend on."""
    default = DeviceInfo(serial="10060006A02F00000001", gateway_id="default")
    other = DeviceInfo(serial="10060006A02F00000001", gateway_id="Mock GW 1")

    # The default gateway's id must never move — existing HA entities key off it.
    assert default.short_id == "00000001"
    assert other.short_id != default.short_id


def test_short_id_is_topic_safe():
    """Topic segments must not contain spaces.

    A space is legal in MQTT but broke real tooling: mosquitto_sub -v separates
    topic and payload with a space, so a topic containing one cannot be parsed
    back out — which silently defeated a cleanup script during this fix.
    """
    d = DeviceInfo(serial="MOCK-MOCK GW 1", gateway_id="Mock GW 1")

    assert d.short_id == "mock_gw_1"
    assert " " not in d.short_id


def test_mock_serial_is_not_mangled_into_the_id():
    """_serial_tail takes the last 8 chars, which butchers a mock serial:
    "MOCK-MOCK GW 1" → "OCK GW 1", giving franklinwh/Mock GW 1_OCK GW 1/…"""
    d = DeviceInfo(serial="MOCK-MOCK GW 1", gateway_id="Mock GW 1")

    assert "OCK GW 1" not in d.short_id
    assert "ock_gw_1_ock" not in d.short_id
