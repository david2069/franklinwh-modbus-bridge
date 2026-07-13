"""Read-only hardware tests against a real FranklinWH aGate.

These tests verify the bridge's data path works with real Modbus hardware.
No registers are written — safe to run at any time.

Run with:
    pytest tests/hardware/ -v -m "hardware and not destructive"

(Plain `-m hardware` also picks up the write-based tests in
test_reversion_and_reserve_persistence.py, since pytest markers are
additive — use the "and not destructive" form if you want read-only only.)
"""

import pytest

pytestmark = pytest.mark.hardware


class TestDeviceIdentity:

    def test_read_nameplate(self, controller):
        info = controller.read_nameplate()
        assert info["manufacturer"] == "FranklinWH Technologies Co., Ltd"
        assert "aGate" in info["model"]
        assert len(info["serial"]) > 0
        assert len(info["version"]) > 0

    def test_serial_format(self, controller):
        info = controller.read_nameplate()
        assert len(info["serial"]) >= 8


class TestBatteryStatus:

    def test_soc_in_range(self, controller):
        status = controller.read_battery_status()
        assert 0 <= status["soc"] <= 100

    def test_soh_in_range(self, controller):
        status = controller.read_battery_status()
        assert 0 <= status["soh"] <= 100

    def test_wh_rating(self, controller):
        status = controller.read_battery_status()
        assert status["wh_rating"] > 0

    def test_battery_state_valid(self, controller):
        status = controller.read_battery_status()
        # Library may return title-case or uppercase enum descriptions
        state = status["battery_state"].upper()
        assert state in ("CHARGING", "DISCHARGING", "IDLE")


class TestGridStatus:

    def test_voltage_sane(self, controller):
        status = controller.read_grid_status()
        assert 100 < status["voltage_v"] < 300

    def test_frequency_sane(self, controller):
        status = controller.read_grid_status()
        assert 45 < status["frequency_hz"] < 65

    def test_connection_state(self, controller):
        status = controller.read_grid_status()
        assert status["connection_state"] in ("Connected", "Disconnected", "Fault")

    def test_inverter_state(self, controller):
        status = controller.read_grid_status()
        valid_states = (
            "Off", "Sleeping", "Starting", "Running", "Throttled",
            "Shutting Down", "Fault", "Standby", "Test", "Manufacturing",
        )
        assert status["inverter_state"] in valid_states


class TestSolarStatus:

    def test_solar_power_not_negative(self, controller):
        status = controller.read_solar_status()
        assert status["ac_power_w"] >= 0

    def test_extension_registers(self, controller):
        status = controller.read_solar_status()
        ext = status.get("extension")
        if ext is None:
            pytest.skip("Extension registers not available")
        assert "home_load_ext" in ext
        assert "ongrid_mode" in ext
        assert "self_reserve" in ext
        assert "tou_reserve" in ext


class TestControlStatus:

    def test_control_readable(self, controller):
        status = controller.read_control_status()
        assert "wset_enabled" in status or "wset_ena" in status

    def test_loc_rem_ctl(self, controller):
        status = controller.read_control_status()
        assert status.get("loc_rem_ctl_name") in (
            "Local", "Remote", None
        )


class TestNativeMode:

    def test_mode_readable(self, controller):
        mode = controller.read_native_mode()
        assert "mode_name" in mode
        assert "mode_raw" in mode

    def test_reserves_readable(self, controller):
        mode = controller.read_native_mode()
        assert 0 <= mode.get("self_reserve_pct", 0) <= 100
        assert 0 <= mode.get("tou_reserve_pct", 0) <= 100


class TestFullCatalogCapture:
    """Test the bridge's catalog capture path against real hardware."""

    def test_sunspec_models_present(self, controller):
        """Verify the device exposes the expected 17 SunSpec models."""
        device = controller._device
        model_ids = set()
        for model_list in device.models.values():
            if isinstance(model_list, list):
                for m in model_list:
                    model_ids.add(m.model_type)
            else:
                model_ids.add(model_list.model_type)

        assert 1 in model_ids
        assert 701 in model_ids
        assert 713 in model_ids
        assert 714 in model_ids
        assert len(model_ids) >= 15

    def test_all_read_methods_succeed(self, controller):
        """Verify every read method returns data without exceptions."""
        results = {
            "nameplate": controller.read_nameplate(),
            "battery": controller.read_battery_status(),
            "grid": controller.read_grid_status(),
            "solar": controller.read_solar_status(),
            "control": controller.read_control_status(),
            "native_mode": controller.read_native_mode(),
        }
        for name, data in results.items():
            assert isinstance(data, dict), f"{name} returned {type(data)}"
            assert len(data) > 0, f"{name} returned empty dict"
