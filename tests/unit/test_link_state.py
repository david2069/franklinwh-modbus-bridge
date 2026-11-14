"""The Modbus link as the dashboard shows it (#497).

The poller keeps retrying an unreachable aGate, so the gateway still counts as
polling. These cover the link state that lets the dashboard say so.
"""

from __future__ import annotations

from types import SimpleNamespace

from franklinwh_bridge.gateway.health import HealthChecker
from franklinwh_bridge.gateway.instance import GatewayConfig, GatewayInstance
from franklinwh_bridge.modbus.poller import PollerState
from franklinwh_bridge.modbus.sample import SampleBus

NOW = 1_800_000_000.0


def _inst(*, connected: bool = True, last_good: float | None = NOW - 5,
          poll_interval: int = 10, mock: bool = False, polling: bool = True) -> GatewayInstance:
    cfg = GatewayConfig(gateway_id="spec", name="Spec aGate", host="192.0.2.10",
                        poll_interval=poll_interval, mock=mock)
    inst = GatewayInstance(cfg, db=None, global_bus=SampleBus())
    state = PollerState(connected=connected, last_good_ts=last_good, last_poll_ts=last_good)
    inst.poller = SimpleNamespace(state=state)
    inst.status.polling = polling
    return inst


def test_link_up_with_a_recent_read_is_not_stale():
    assert _inst().link(now=NOW) == {"link_up": True, "last_good_ts": NOW - 5, "stale": False}


def test_link_down_while_retrying_is_stale():
    link = _inst(connected=False, last_good=NOW - 600).link(now=NOW)
    assert link == {"link_up": False, "last_good_ts": NOW - 600, "stale": True}


def test_an_old_last_good_read_is_stale_even_when_connected():
    # 3 polls of 10 s gives 30 s; 31 s old is stale, 29 s isn't.
    assert _inst(last_good=NOW - 31).link(now=NOW)["stale"] is True
    assert _inst(last_good=NOW - 29).link(now=NOW)["stale"] is False


def test_the_stale_limit_follows_the_poll_interval():
    assert _inst(poll_interval=60, last_good=NOW - 170).link(now=NOW)["stale"] is False
    assert _inst(poll_interval=60, last_good=NOW - 190).link(now=NOW)["stale"] is True


def test_never_connected_is_stale_once_it_has_been_polling():
    link = _inst(connected=False, last_good=None).link(now=NOW)
    assert link["link_up"] is False and link["stale"] is True


def test_a_stopped_gateway_is_not_stale():
    assert _inst(connected=False, polling=False).link(now=NOW)["stale"] is False


def test_no_poller_has_no_link():
    inst = _inst()
    inst.poller = None
    assert inst.link(now=NOW) == {"link_up": False, "last_good_ts": None, "stale": False}


def test_a_mock_gateway_is_always_linked():
    inst = _inst(mock=True)
    inst.poller = SimpleNamespace(state=SimpleNamespace(last_poll_ts=NOW - 2))
    assert inst.link(now=NOW) == {"link_up": True, "last_good_ts": NOW - 2, "stale": False}


def test_to_dict_carries_the_link():
    d = _inst(connected=False, last_good=NOW - 600).to_dict()
    assert d["link_up"] is False and d["stale"] is True and d["last_good_ts"] == NOW - 600


async def test_health_probe_follows_the_poller_link(monkeypatch):
    """TCP answers but the poller's link is down: tcp_only, not connected."""
    inst = _inst(connected=False)
    inst.status.connected = True  # set at start and never cleared on a dropped link
    hc = HealthChecker(SimpleNamespace(instances={"spec": inst}))

    async def probe(*_):
        return True

    monkeypatch.setattr(hc, "_tcp_probe", probe)
    await hc._check_all()
    assert inst.status.health == "tcp_only"
