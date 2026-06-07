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
