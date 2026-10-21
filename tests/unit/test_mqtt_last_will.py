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
        _Client.made.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def publish(self, *a, **kw):
        pass

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
