"""#325: the broker marks the gateway "offline" if the bridge vanishes.

Without a last will, a crashed bridge left the retained availability "online"
and HA showed frozen values as live.
"""

import asyncio

import pytest

from franklinwh_bridge.publish import mqtt_publisher as mp
from franklinwh_bridge.publish.mqtt_publisher import DeviceInfo, MqttPublisher

SERIAL = "10060006A02F2417ABCD"


class _Client:
    made: list = []

    def __init__(self, **kw):
        self.kw = kw
        self.retained: list[str] = []
        _Client.made.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def publish(self, topic, payload=None, retain=False, qos=0):
        if retain and payload:  # an empty retained payload clears a topic
            self.retained.append(topic)

    async def subscribe(self, *a, **kw):
        pass

    async def unsubscribe(self, *a, **kw):
        pass

    @property
    def messages(self):
        async def gen():
            await asyncio.Event().wait()
            yield None

        return gen()


@pytest.fixture
def clients(monkeypatch):
    _Client.made = []
    monkeypatch.setattr(mp.aiomqtt, "Client", _Client)
    return _Client.made


def test_will_topic_needs_exactly_one_gateway():
    pub = MqttPublisher(host="x", client_id="c")
    assert pub._will_topic() is None
    info = DeviceInfo(serial=SERIAL)
    pub.set_device_info(info)
    assert pub._will_topic() == f"franklinwh/{info.short_id}/availability"


async def test_reconnects_once_to_carry_the_will(clients):
    pub = MqttPublisher(host="x", client_id="c")
    task = asyncio.create_task(pub._run_loop())
    await asyncio.sleep(0.05)
    assert clients[0].kw["will"] is None  # no gateway announced yet

    info = DeviceInfo(serial=SERIAL)
    pub.set_device_info(info)  # the gateway registers after connect
    for _ in range(40):
        if len(clients) > 1:
            break
        await asyncio.sleep(0.05)
    pub._stop_event.set()
    await asyncio.wait_for(task, 3)

    will = clients[1].kw["will"]
    assert will.topic == f"franklinwh/{info.short_id}/availability"
    assert will.payload == "offline" and will.retain is True
    assert len(clients) == 2  # once, not a loop


async def test_discovery_goes_out_on_the_connection_that_stays(clients):
    """#412: a gateway registered after connect had its discovery published on
    the connection about to close for the will, at QoS 0, so the broker kept
    none of it, and it was never sent again: no entities in HA."""
    pub = MqttPublisher(host="x", client_id="c")
    task = asyncio.create_task(pub._run_loop())
    await asyncio.sleep(0.05)
    pub.register_device("demo", DeviceInfo(serial="MOCK-DEMO", gateway_id="demo"))
    for _ in range(60):
        if len(clients) > 1 and any(t.endswith("/config") and "demo" in t
                                    for t in clients[1].retained):
            break
        await asyncio.sleep(0.05)
    pub._stop_event.set()
    await asyncio.wait_for(task, 3)

    assert len(clients) == 2  # the reconnect that carries the will
    configs = [t for t in clients[1].retained
               if t.startswith("homeassistant/") and t.endswith("/config") and "demo" in t]
    assert len(configs) == len(pub.get_device("demo").entities)
