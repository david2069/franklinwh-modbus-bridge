"""Background loops survive an exception instead of dying silently (#31).

Each of these loops used to catch only CancelledError, so any other exception
ended it for the life of the process while the app kept serving HTTP.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from franklinwh_bridge.gateway.health import HealthChecker
from franklinwh_bridge.publish import command_handler as ch
from franklinwh_bridge.publish.command_handler import CommandHandler
from franklinwh_bridge.publish.mqtt_publisher import MqttPublisher


def _inst(host: str) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(enabled=True, mock=False, host=host, port=502),
        status=SimpleNamespace(health="unknown", connected=True, polling=True),
    )


async def test_health_survives_gateway_added_mid_probe(monkeypatch):
    """Adding a gateway while a probe awaits used to raise "dictionary changed
    size during iteration" and end the health monitor."""
    instances = {"a": _inst("10.0.0.1"), "b": _inst("10.0.0.2")}
    hc = HealthChecker(SimpleNamespace(instances=instances))

    async def probe(host, port):
        instances.setdefault("c", _inst("10.0.0.3"))  # registry changes mid-pass
        return True

    monkeypatch.setattr(hc, "_tcp_probe", probe)
    await hc._check_all()

    assert instances["a"].status.health == "connected"
    assert instances["b"].status.health == "connected"


async def test_health_one_failing_gateway_does_not_skip_the_rest(monkeypatch):
    instances = {"bad": _inst("10.0.0.1"), "good": _inst("10.0.0.2")}
    hc = HealthChecker(SimpleNamespace(instances=instances))

    async def probe(host, port):
        if host == "10.0.0.1":
            raise RuntimeError("boom")
        return True

    monkeypatch.setattr(hc, "_tcp_probe", probe)
    await hc._check_all()

    assert instances["good"].status.health == "connected"


async def test_health_loop_keeps_running_after_a_failed_pass(monkeypatch):
    hc = HealthChecker(SimpleNamespace(instances={}), check_interval_s=0)
    passes = 0

    async def check_all():
        nonlocal passes
        passes += 1
        if passes == 1:
            raise RuntimeError("boom")

    monkeypatch.setattr(hc, "_check_all", check_all)
    await hc.start()
    for _ in range(50):
        if passes >= 2:
            break
        await asyncio.sleep(0.01)
    await hc.stop()

    assert passes >= 2


async def test_mqtt_listener_survives_a_failing_command():
    """A handler that raises used to end the listener: state publishing went on
    while HA commands were silently dropped."""
    pub = MqttPublisher(host="x")
    handled = []

    async def handle(message):
        if message.topic == "bad":
            raise ValueError("boom")
        handled.append(message.topic)

    pub._handle_mqtt_message = handle

    async def messages():
        for topic in ("bad", "good"):
            yield SimpleNamespace(topic=topic)

    await pub._subscribe_listener(SimpleNamespace(messages=messages()))

    assert handled == ["good"]


async def test_watchdog_survives_a_failed_check(monkeypatch):
    """The watchdog enforces a running force's limits; one raised check must not
    leave the force running unsupervised."""
    monkeypatch.setattr(ch, "SOC_CHECK_INTERVAL_S", 0)
    h = CommandHandler(SimpleNamespace(), None)
    h._state.watchdog_s = 0
    h._state.action = "Force Discharge"
    h._target_soc = 20
    reads = iter([RuntimeError("read failed"), 15.0])

    def read_soc():
        v = next(reads)
        if isinstance(v, Exception):
            raise v
        return v

    released = []

    async def release(reason):
        released.append(reason)

    monkeypatch.setattr(h, "_read_soc", read_soc)
    monkeypatch.setattr(h, "_release_command", release)
    await asyncio.wait_for(h._watchdog_loop(), timeout=2)

    assert released == ["target_soc_reached"]
