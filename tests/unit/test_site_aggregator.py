"""Tests for SiteAggregator — computes virtual site-level metrics."""

import pytest

from franklinwh_bridge.gateway.aggregator import SiteAggregator
from franklinwh_bridge.modbus.sample import Sample


@pytest.fixture
def aggregator():
    return SiteAggregator()


async def test_empty_aggregator(aggregator):
    assert aggregator.gateway_count == 0
    assert aggregator.site_points == {}


async def test_single_gateway_sample(aggregator):
    sample = Sample.now("gw1", {
        "total_solar": 3000,
        "battery_power_w": -500,
        "grid_power_w": 200,
        "home_load_ext": 2700,
        "soc": 75,
    })
    await aggregator.on_sample(sample)

    pts = aggregator.site_points
    assert pts["site_total_solar_w"] == 3000
    assert pts["site_battery_power_w"] == -500
    assert pts["site_grid_power_w"] == 200
    assert pts["site_home_load_w"] == 2700
    assert pts["site_soc_avg"] == 75
    assert pts["site_soc_min"] == 75
    assert pts["site_soc_max"] == 75
    assert pts["site_gateway_count"] == 1
    assert pts["site_is_off_grid"] is False


async def test_multi_gateway_aggregation(aggregator):
    """Two gateways should sum power and average SoC."""
    s1 = Sample.now("gw1", {
        "total_solar": 3000,
        "battery_power_w": -500,
        "grid_power_w": 200,
        "home_load_ext": 2700,
        "soc": 80,
    })
    s2 = Sample.now("gw2", {
        "total_solar": 2000,
        "battery_power_w": 1000,
        "grid_power_w": -500,
        "home_load_ext": 2500,
        "soc": 60,
    })
    await aggregator.on_sample(s1)
    await aggregator.on_sample(s2)

    pts = aggregator.site_points
    assert pts["site_total_solar_w"] == 5000  # 3000 + 2000
    assert pts["site_battery_power_w"] == 500  # -500 + 1000
    assert pts["site_grid_power_w"] == -300  # 200 + -500
    assert pts["site_home_load_w"] == 5200  # 2700 + 2500
    assert pts["site_soc_avg"] == 70  # (80 + 60) / 2
    assert pts["site_soc_min"] == 60
    assert pts["site_soc_max"] == 80
    assert pts["site_gateway_count"] == 2


async def test_emits_canonical_dashboard_keys(aggregator):
    """Site aggregate must expose the canonical point keys the dashboard binds
    to, not only ``site_*`` aliases — otherwise the Site view renders all '--'.
    """
    s1 = Sample.now("gw1", {
        "total_solar": 3000, "battery_power_w": -500,
        "grid_power_w": 200, "home_load_ext": 2700, "soc": 80,
    })
    s2 = Sample.now("gw2", {
        "total_solar": 2000, "battery_power_w": 1000,
        "grid_power_w": -500, "home_load_ext": 2500, "soc": 60,
    })
    await aggregator.on_sample(s1)
    await aggregator.on_sample(s2)

    pts = aggregator.site_points
    # Canonical keys the dashboard getters read.
    assert pts["total_solar"] == 5000
    assert pts["battery_power_w"] == 500
    assert pts["battery_dc_power_w"] == 500
    assert pts["grid_power_w"] == -300
    assert pts["home_load_ext"] == 5200
    assert pts["soc"] == 70
    assert pts["connection_state"] == "Connected"
    # Aliases remain for future site-level MQTT entities.
    assert pts["site_total_solar_w"] == 5000


@pytest.mark.parametrize(
    "battery_w, expected",
    [(600, "Discharging"), (-600, "Charging"), (0, "Standby"), (30, "Standby")],
)
async def test_battery_state_derivation(aggregator, battery_w, expected):
    """Aggregate battery_state follows summed power sign (+=discharging) with a
    50 W deadband."""
    await aggregator.on_sample(Sample.now("gw1", {"battery_power_w": battery_w}))
    assert aggregator.site_points["battery_state"] == expected


async def test_off_grid_detection(aggregator):
    s1 = Sample.now("gw1", {"soc": 50, "connection_state": "Connected"})
    await aggregator.on_sample(s1)
    assert aggregator.site_points["site_is_off_grid"] is False

    s2 = Sample.now("gw2", {"soc": 40, "connection_state": "Disconnected"})
    await aggregator.on_sample(s2)
    assert aggregator.site_points["site_is_off_grid"] is True


async def test_latest_overwrites_previous(aggregator):
    s1 = Sample.now("gw1", {"total_solar": 3000, "soc": 80})
    await aggregator.on_sample(s1)
    assert aggregator.site_points["site_total_solar_w"] == 3000

    # Same gateway, updated values
    s2 = Sample.now("gw1", {"total_solar": 1000, "soc": 75})
    await aggregator.on_sample(s2)
    assert aggregator.site_points["site_total_solar_w"] == 1000
    assert aggregator.site_points["site_soc_avg"] == 75


async def test_clear_gateway(aggregator):
    s1 = Sample.now("gw1", {"total_solar": 3000, "soc": 80})
    s2 = Sample.now("gw2", {"total_solar": 2000, "soc": 60})
    await aggregator.on_sample(s1)
    await aggregator.on_sample(s2)

    aggregator.clear_gateway("gw1")
    assert aggregator.gateway_count == 1
    pts = aggregator.site_points
    assert pts["site_total_solar_w"] == 2000
    assert pts["site_soc_avg"] == 60


async def test_get_gateway_points(aggregator):
    s1 = Sample.now("gw1", {"soc": 80, "total_solar": 3000})
    await aggregator.on_sample(s1)

    pts = aggregator.get_gateway_points("gw1")
    assert pts["soc"] == 80
    assert pts["total_solar"] == 3000

    # Nonexistent gateway returns empty dict
    assert aggregator.get_gateway_points("nope") == {}


async def test_partial_data(aggregator):
    """One gateway has solar, the other doesn't."""
    s1 = Sample.now("gw1", {"total_solar": 3000, "soc": 80})
    s2 = Sample.now("gw2", {"soc": 60})  # no solar
    await aggregator.on_sample(s1)
    await aggregator.on_sample(s2)

    pts = aggregator.site_points
    assert pts["site_total_solar_w"] == 3000  # only gw1's solar
    assert pts["site_soc_avg"] == 70  # both contribute to SoC


# ── Per-phase site aggregation (MP2 / Topology A) ───────────────

async def test_per_phase_buckets_by_tag(aggregator):
    """Each gateway's canonical power is bucketed into its tagged phase."""
    await aggregator.on_sample(Sample.now("a", {"grid_power_w": 100, "home_load_ext": 1000}))
    await aggregator.on_sample(Sample.now("b", {"grid_power_w": 200, "home_load_ext": 2000}))
    aggregator.set_gateway_phase("a", "L1")
    aggregator.set_gateway_phase("b", "L2")

    pts = aggregator.site_points
    # grand totals still sum everything
    assert pts["site_grid_power_w"] == 300
    # per-phase buckets split by tag
    assert pts["site_grid_power_L1"] == 100
    assert pts["site_grid_power_L2"] == 200
    assert pts["site_home_load_L1"] == 1000
    assert pts["site_home_load_L2"] == 2000
    # L3 has no gateway → no key emitted
    assert "site_grid_power_L3" not in pts


async def test_untagged_gateway_only_in_grand_total(aggregator):
    """A gateway with no tag (defaults 'all') contributes to the total but
    not to any per-phase bucket."""
    await aggregator.on_sample(Sample.now("a", {"grid_power_w": 150}))
    # no set_gateway_phase → defaults to 'all'
    pts = aggregator.site_points
    assert pts["site_grid_power_w"] == 150
    assert "site_grid_power_L1" not in pts
    assert "site_grid_power_L2" not in pts
    assert "site_grid_power_L3" not in pts


async def test_two_gateways_same_phase_sum(aggregator):
    await aggregator.on_sample(Sample.now("a", {"grid_power_w": 100}))
    await aggregator.on_sample(Sample.now("b", {"grid_power_w": 250}))
    aggregator.set_gateway_phase("a", "L1")
    aggregator.set_gateway_phase("b", "L1")
    assert aggregator.site_points["site_grid_power_L1"] == 350


async def test_clear_gateway_drops_phase(aggregator):
    await aggregator.on_sample(Sample.now("a", {"grid_power_w": 100}))
    aggregator.set_gateway_phase("a", "L1")
    assert aggregator.site_points["site_grid_power_L1"] == 100
    aggregator.clear_gateway("a")
    assert "a" not in aggregator._phases
    assert aggregator.site_points == {}
