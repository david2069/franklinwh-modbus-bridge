"""Mock gateways: simulated control, and an honest refusal where it can't work.

- Control is simulated: MockController implements the write methods the real
  CommandHandler calls, and the synthetic samples follow the simulated state.
  So HA controls, the Controls tab and schedules work on a mock.
- The Sequencer reads/writes real Modbus registers, which a mock doesn't have.
  It used to error on one; it now refuses a mock target with a clear 400.
"""

from __future__ import annotations

import pytest
from franklinwh_modbus import BatteryCommand
from httpx import ASGITransport, AsyncClient

from franklinwh_bridge.gateway.mock_gateway import MockController, MockPoller
from franklinwh_bridge.main import app
from franklinwh_bridge.modbus.sample import SampleBus


def _poller() -> tuple[MockController, MockPoller]:
    ctl = MockController("demo")
    return ctl, MockPoller(SampleBus(), "demo", 10, controller=ctl)


def test_samples_follow_a_simulated_dispatch():
    ctl, poller = _poller()
    before = poller.sample_points(10)
    assert before["loc_rem_ctl_name"] == "Local"

    ok, msg = ctl.send_command(BatteryCommand(power_watts=3000), duration_s=600)
    assert ok and "mock" in msg
    pts = poller.sample_points(10)
    assert pts["battery_power_w"] == -3000.0  # points: negative = charging
    assert pts["battery_state"] == "Charging"
    assert pts["loc_rem_ctl_name"] == "Remote"
    # The grid balances the commanded battery, not the synthetic one.
    assert pts["grid_power_w"] == pytest.approx(
        pts["home_load_ext"] - pts["total_solar"] - pts["battery_power_w"], abs=0.2,
    )

    assert ctl.reset_control_state() is True
    assert poller.sample_points(10)["loc_rem_ctl_name"] == "Local"


def test_soc_integrates_and_respects_the_reserve():
    ctl, poller = _poller()
    ctl.set_self_consumption_reserve(50)
    ctl.send_command(BatteryCommand(power_watts=-5000))  # hard discharge
    socs = [poller.sample_points(600)["soc"] for _ in range(12)]  # ~2h
    assert socs[-1] < socs[0]
    final = poller.sample_points(600)
    # Held at the reserve floor rather than discharging through it.
    assert final["soc"] >= 49.0
    if final["soc"] <= 50.0:
        assert final["battery_power_w"] == 0.0


def test_mode_and_reserves_read_back():
    ctl, poller = _poller()
    assert ctl.set_native_mode(3) == (True, "Simulated mode change to TOU (mock gateway)")
    ctl.set_tou_reserve(40)
    pts = poller.sample_points(10)
    assert pts["mode_name"] == "TOU"
    assert pts["tou_reserve_pct"] == 40
    assert ctl.set_native_mode(9)[0] is False


def test_dispatch_expires_after_its_duration(monkeypatch):
    import franklinwh_bridge.gateway.mock_gateway as mg

    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(mg.time, "time", lambda: clock["t"])
    ctl, _ = _poller()
    ctl.send_command(BatteryCommand(power_watts=2000), duration_s=60)
    assert ctl.active_command_w() == 2000
    clock["t"] += 61
    assert ctl.active_command_w() is None


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as ac,
        app.router.lifespan_context(app),
    ):
        yield ac


async def test_mock_is_commandable_end_to_end_and_refused_by_the_sequencer(client):
    resp = await client.post(
        "/api/gateways", json={"gateway_id": "demo", "name": "Demo", "mock": True},
    )
    assert resp.status_code == 201
    inst = app.state.registry.get("demo")
    assert inst.command_handler is not None  # so schedules resolve to it too

    # HA gets the control entities (select/number) for it, not just sensors.
    dev = app.state.mqtt_publisher.get_device("demo")
    assert any(e.slug == "operating_mode" for e in dev.entities)

    resp = await client.post(
        "/api/gateways/demo/command", json={"slug": "operating_mode", "value": "TOU"},
    )
    assert resp.status_code == 200
    assert inst.controller.mode == "TOU"

    # The Sequencer needs real registers.
    resp = await client.post(
        "/api/sequence/execute",
        json={"gateway_id": "demo", "inline": '{"WMaxLimPct": 50}', "dry_run": True},
    )
    assert resp.status_code == 400
    assert "mock" in resp.json()["detail"]
