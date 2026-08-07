"""Unit tests for the scheduler v2 sensor registry (pure, no hardware)."""

from datetime import datetime

from franklinwh_bridge.gateway.mock_gateway import synthetic_points
from franklinwh_bridge.gateway.scheduler_sensors import (
    PV_GENERATING_THRESHOLD_W,
    SENSORS,
    sensor_catalog,
    snapshot,
)

NOW = datetime(2026, 8, 7, 14, 30)  # Friday

# A hand-built points dict using the real canonical keys.
POINTS = {
    "soc": 62.5,
    "battery_power_w": -1200.0,  # charging
    "total_solar": 3400.0,
    "grid_power_w": -800.0,
    "home_load_ext": 1400.0,
    "mode_name": "Self-Consumption",
    "mode_raw": 2,
    "connection_state": "Connected",
}


# ── key mapping ───────────────────────────────────────────────


def test_snapshot_maps_canonical_keys():
    snap = snapshot(POINTS, NOW)
    assert snap["battery.soc_pct"] == 62.5
    assert snap["battery.power_w"] == -1200.0
    assert snap["solar.power_w"] == 3400.0
    assert snap["grid.power_w"] == -800.0
    assert snap["load.power_w"] == 1400.0
    assert snap["mode.name"] == "Self-Consumption"
    assert snap["mode.raw"] == 2.0


def test_snapshot_always_contains_every_sensor_id():
    snap = snapshot({}, NOW)  # empty points
    assert set(snap) == {s.id for s in SENSORS}
    # unavailable numeric/enum/bool sensors are None (fail closed downstream)
    assert snap["battery.soc_pct"] is None
    assert snap["mode.name"] is None
    assert snap["grid.connected"] is None


def test_non_numeric_value_is_none():
    assert snapshot({"soc": "n/a"}, NOW)["battery.soc_pct"] is None


def test_numeric_string_is_coerced():
    assert snapshot({"soc": "62.5"}, NOW)["battery.soc_pct"] == 62.5


# ── grid.connected derivation ─────────────────────────────────


def test_grid_connected_from_connection_state():
    assert snapshot({"connection_state": "Connected"}, NOW)["grid.connected"] is True
    assert snapshot({"connection_state": "Disconnected"}, NOW)["grid.connected"] is False


def test_grid_connected_falls_back_to_grid_mode():
    assert snapshot({"grid_mode": "Grid Following"}, NOW)["grid.connected"] is True
    assert snapshot({"grid_mode": "Grid Forming"}, NOW)["grid.connected"] is False


def test_grid_connected_prefers_connection_state_over_grid_mode():
    pts = {"connection_state": "Disconnected", "grid_mode": "Grid Following"}
    assert snapshot(pts, NOW)["grid.connected"] is False


def test_grid_connected_none_when_no_signal():
    assert snapshot({}, NOW)["grid.connected"] is None


# ── pv.is_generating derivation ───────────────────────────────


def test_pv_generating_threshold():
    assert snapshot({"total_solar": PV_GENERATING_THRESHOLD_W + 1}, NOW)["pv.is_generating"] is True
    assert (
        snapshot({"total_solar": PV_GENERATING_THRESHOLD_W - 1}, NOW)["pv.is_generating"] is False
    )
    assert snapshot({"total_solar": 0}, NOW)["pv.is_generating"] is False


def test_pv_generating_none_when_no_solar_key():
    assert snapshot({}, NOW)["pv.is_generating"] is None


# ── time.* from clock ─────────────────────────────────────────


def test_time_sensors():
    snap = snapshot(POINTS, NOW)
    assert snap["time.hour"] == 14
    assert snap["time.dow"] == 4  # Friday (0=Mon)
    assert snap["time.month"] == 8


# ── price stub ────────────────────────────────────────────────


def test_price_is_stubbed_none():
    assert snapshot({"price.export_c_per_kwh": 25}, NOW)["price.export_c_per_kwh"] is None


# ── realistic sample from the mock ────────────────────────────


def test_snapshot_over_synthetic_points():
    pts = synthetic_points("gw1", tick=42, ts=1_754_000_000.0)
    snap = snapshot(pts, NOW)
    # mock always reports Connected and emits every mapped key
    assert snap["grid.connected"] is True
    assert isinstance(snap["battery.soc_pct"], float)
    assert isinstance(snap["solar.power_w"], float)
    assert isinstance(snap["mode.name"], str)
    assert isinstance(snap["pv.is_generating"], bool)


# ── catalog for /api/sensors ──────────────────────────────────


def test_sensor_catalog_shape_without_points():
    cat = sensor_catalog()
    ids = {row["id"] for row in cat}
    assert "battery.soc_pct" in ids
    assert "price.export_c_per_kwh" in ids
    first = cat[0]
    assert set(first) == {"id", "label", "unit", "kind"}  # no getter, no value
    assert "getter" not in first


def test_sensor_catalog_includes_values_when_points_given():
    cat = sensor_catalog(POINTS, NOW)
    by_id = {row["id"]: row for row in cat}
    assert by_id["battery.soc_pct"]["value"] == 62.5
    assert by_id["price.export_c_per_kwh"]["value"] is None
