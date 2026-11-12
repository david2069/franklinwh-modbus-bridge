"""A gateway that is deleted or stops publishing to HA leaves nothing retained under
``<prefix>/<short_id>/`` (#182). Before the fix its discovery configs were tombstoned,
but its last values stayed retained and availability was left at "offline"."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from franklinwh_bridge.publish import cleanup
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher

#: What the broker holds: the shed gateway's values (one from an older version),
#: a neighbour whose id starts the same, and another install's gateway.
RETAINED = {
    "fwh_mb/shed/availability": b"online",
    "fwh_mb/shed/battery/battery_soc_pct": b"55",
    "fwh_mb/shed/old/removed_point": b"1",
    "fwh_mb/shed2/battery/battery_soc_pct": b"40",
    "fwh_mb/house/battery/battery_soc_pct": b"70",
}


class _Broker:
    """Stands in for aiomqtt.Client: replays the retained topics a filter matches."""

    def __init__(self, *a, **k) -> None:
        self.filters: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def subscribe(self, topic_filter):
        self.filters.append(topic_filter)

    @property
    def messages(self):
        async def gen():
            for f in self.filters:
                head = f.removesuffix("#")
                for t, p in RETAINED.items():
                    if t.startswith(head):
                        yield SimpleNamespace(topic=t, payload=p, retain=True)
            await asyncio.Event().wait()
        return gen()


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    monkeypatch.setattr(cleanup.aiomqtt, "Client", _Broker)
    monkeypatch.setattr(cleanup, "_COLLECT_S", 0.05)


def publisher() -> MqttPublisher:
    pub = MqttPublisher(host="x", topic_prefix="fwh_mb")
    pub._state.connected = True
    return pub


def queued(pub: MqttPublisher) -> list:
    out = []
    while not pub._queue.empty():
        out.append(pub._queue.get_nowait())
    return out


async def settle(pub: MqttPublisher) -> None:
    await asyncio.gather(*getattr(pub, "_clearing", {}).values())


def cleared(msgs) -> set[str]:
    return {m.topic for m in msgs if m.retain and m.payload == ""}


async def test_unpublishing_a_gateway_clears_its_retained_values():
    pub = publisher()
    pub.register_device("shed", DeviceInfo(serial="TESTSERIAL0000EF01", gateway_id="shed"))
    queued(pub)
    await pub.unregister_device("shed", tombstone=True)
    await settle(pub)
    msgs = queued(pub)
    gone = cleared(msgs)
    assert {"fwh_mb/shed/battery/battery_soc_pct", "fwh_mb/shed/old/removed_point",
            "fwh_mb/shed/availability"} <= gone
    others = {"fwh_mb/shed2/battery/battery_soc_pct", "fwh_mb/house/battery/battery_soc_pct"}
    assert not others & gone
    # The availability clear goes out after the "offline", so nothing is left.
    avail = [m.payload for m in msgs if m.topic == "fwh_mb/shed/availability"]
    assert avail[-1] == ""


async def test_a_rebind_without_tombstone_clears_nothing():
    pub = publisher()
    pub.register_device("shed", DeviceInfo(serial="TESTSERIAL0000EF01", gateway_id="shed"))
    queued(pub)
    await pub.unregister_device("shed", tombstone=False)
    await settle(pub)
    assert not cleared(queued(pub))


async def test_deleting_an_unpublished_gateway_clears_its_leftovers():
    """Publish off, then delete (the issue's repro): no device is registered any more."""
    pub = publisher()
    pub.clear_gateway_topics("shed")
    await settle(pub)
    assert cleared(queued(pub)) == {"fwh_mb/shed/availability",
                                    "fwh_mb/shed/battery/battery_soc_pct",
                                    "fwh_mb/shed/old/removed_point"}


async def test_default_gateway_unpublish_clears_its_values():
    pub = publisher()
    info = DeviceInfo(serial="TESTSERIALSHED0000")
    pub.set_device_info(info)
    sid = info.short_id
    RETAINED[f"fwh_mb/{sid}/battery/battery_soc_pct"] = b"60"
    try:
        pub.unbind_default_device(tombstone=True)
        await settle(pub)
        assert f"fwh_mb/{sid}/battery/battery_soc_pct" in cleared(queued(pub))
    finally:
        del RETAINED[f"fwh_mb/{sid}/battery/battery_soc_pct"]


async def test_no_broker_connection_no_clear():
    pub = publisher()
    pub._state.connected = False
    pub.clear_gateway_topics("shed")
    assert not pub._clearing and not queued(pub)


async def test_default_id_is_never_cleared_by_delete():
    pub = publisher()
    pub.clear_gateway_topics("default")
    assert not pub._clearing
