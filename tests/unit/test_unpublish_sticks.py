"""Unpublish stays in force until Republish (#131).

It sent the tombstones and cleared discovery_published, and the run loop
republished every entity about a second later.
"""

from __future__ import annotations

from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher


class _Client:
    def __init__(self, *a, **k) -> None:
        self.published: list[tuple[str, bytes]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def publish(self, topic, payload, retain=False, qos=0):
        self.published.append((topic, payload))


async def test_unpublish_pauses_and_covers_every_gateway(monkeypatch):
    clients: list[_Client] = []

    def make(*a, **k):
        c = _Client()
        clients.append(c)
        return c

    from franklinwh_bridge.publish import mqtt_publisher as mp

    monkeypatch.setattr(mp.aiomqtt, "Client", make)
    pub = MqttPublisher(host="x")
    pub.set_device_info(DeviceInfo(serial="TESTSERIAL0000ABCD"))
    pub.register_device("shed", DeviceInfo(serial="TESTSERIAL0000EF01", gateway_id="shed"))

    count = await pub.unpublish_discovery()
    assert pub.discovery_paused is True
    topics = {t for t, _ in clients[0].published}
    assert any("_0000abcd_" in t or "_0000ABCD_" in t for t in topics)
    assert any("_shed_" in t for t in topics)  # the extra gateway too
    assert count == len([t for t in topics if t.endswith("/config")])


def test_republish_ends_the_pause():
    pub = MqttPublisher(host="x")
    pub.set_discovery_paused(True)
    pub.request_discovery_republish()
    assert pub.discovery_paused is False
