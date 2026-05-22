"""Entity definitions for HA MQTT Discovery.

Each EntityDef maps one HA entity to a key in the poller's merged Sample.points
dict.  The controller's read_*_status() methods return pre-scaled values, so
stat_key values here match those dict keys directly.

Slug, ha_type, and unique_id format are immutable once published (AP-2 policy
from the HA integrator).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EntityDef:
    slug: str
    name: str
    ha_type: str
    state_group: str
    stat_key: str = ""

    unit: str = ""
    device_class: str = ""
    state_class: str = ""
    icon: str = ""
    entity_category: str = ""

    value_scale: float = 1.0
    value_precision: int | None = None

    is_control: bool = False
    options: list[str] = field(default_factory=list)
    min_val: float | None = None
    max_val: float | None = None
    step: float | None = None

    def format_value(self, raw_value: float | int | str) -> str:
        if isinstance(raw_value, str):
            return raw_value
        if self.value_scale == 1.0 and self.value_precision is None:
            return str(raw_value)
        scaled = raw_value * self.value_scale
        if self.value_precision is not None:
            return f"{scaled:.{self.value_precision}f}"
        return str(scaled)

    def unique_id(self, short_id: str) -> str:
        return f"franklinwh_{short_id}_{self.slug}"

    def state_topic(self, short_id: str) -> str:
        return f"franklinwh/{short_id}/{self.state_group}/{self.slug}"

    def command_topic(self, short_id: str) -> str | None:
        if self.is_control:
            return f"franklinwh/{short_id}/control/{self.slug}/set"
        return None

    def discovery_topic(self, short_id: str) -> str:
        return f"homeassistant/{self.ha_type}/franklinwh_{short_id}_{self.slug}/config"


# ---------------------------------------------------------------------------
# Entity registry — maps controller read method keys → HA entities
#
# stat_key corresponds to keys in Sample.points, which are populated by
# ModbusPoller merging read_battery_status(), read_grid_status(),
# read_solar_status(), read_control_status(), read_native_mode().
# ---------------------------------------------------------------------------

BRIDGE_ENTITIES: list[EntityDef] = [
    # === BATTERY (from read_battery_status) ===
    EntityDef(
        slug="battery_soc",
        name="State of Charge",
        ha_type="sensor",
        state_group="battery",
        stat_key="soc",
        unit="%",
        device_class="battery",
        state_class="measurement",
        icon="mdi:battery",
    ),
    EntityDef(
        slug="battery_soh",
        name="State of Health",
        ha_type="sensor",
        state_group="battery",
        stat_key="soh",
        unit="%",
        state_class="measurement",
        icon="mdi:battery-heart-variant",
    ),
    EntityDef(
        slug="battery_power_kw",
        name="Battery Power",
        ha_type="sensor",
        state_group="battery",
        stat_key="battery_power_w",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:battery-charging",
        value_scale=0.001,
        value_precision=3,
    ),
    EntityDef(
        slug="battery_current_a",
        name="Battery Current",
        ha_type="sensor",
        state_group="battery",
        stat_key="battery_current_a",
        unit="A",
        device_class="current",
        state_class="measurement",
        icon="mdi:current-dc",
    ),
    EntityDef(
        slug="battery_state",
        name="Battery State",
        ha_type="sensor",
        state_group="battery",
        stat_key="battery_state",
        icon="mdi:battery-sync",
    ),
    EntityDef(
        slug="total_capacity_kwh",
        name="Total Capacity",
        ha_type="sensor",
        state_group="capacity",
        stat_key="wh_rating",
        unit="kWh",
        device_class="energy",
        icon="mdi:battery-high",
        state_class="measurement",
        entity_category="diagnostic",
        value_scale=0.001,
        value_precision=3,
    ),
    EntityDef(
        slug="available_capacity_kwh",
        name="Available Capacity",
        ha_type="sensor",
        state_group="capacity",
        stat_key="wh_available",
        unit="kWh",
        device_class="energy",
        state_class="measurement",
        icon="mdi:battery-50",
        value_scale=0.001,
        value_precision=3,
    ),

    # === GRID / AC (from read_grid_status) ===
    EntityDef(
        slug="grid_power_kw",
        name="Grid Power",
        ha_type="sensor",
        state_group="power",
        stat_key="grid_power_w",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:transmission-tower",
        value_scale=0.001,
        value_precision=3,
    ),
    EntityDef(
        slug="grid_voltage_v",
        name="Grid Voltage",
        ha_type="sensor",
        state_group="status",
        stat_key="voltage_v",
        unit="V",
        device_class="voltage",
        state_class="measurement",
        icon="mdi:flash",
    ),
    EntityDef(
        slug="grid_frequency_hz",
        name="Frequency",
        ha_type="sensor",
        state_group="status",
        stat_key="frequency_hz",
        unit="Hz",
        device_class="frequency",
        state_class="measurement",
        icon="mdi:sine-wave",
    ),
    EntityDef(
        slug="grid_current_a",
        name="Grid Current",
        ha_type="sensor",
        state_group="status",
        stat_key="current_a",
        unit="A",
        device_class="current",
        state_class="measurement",
        icon="mdi:current-ac",
    ),
    EntityDef(
        slug="grid_apparent_power_va",
        name="Apparent Power",
        ha_type="sensor",
        state_group="status",
        stat_key="grid_va",
        unit="VA",
        device_class="apparent_power",
        state_class="measurement",
        icon="mdi:flash-triangle",
    ),
    EntityDef(
        slug="grid_reactive_power_var",
        name="Reactive Power",
        ha_type="sensor",
        state_group="status",
        stat_key="grid_var",
        unit="var",
        device_class="reactive_power",
        state_class="measurement",
        icon="mdi:math-sin",
    ),
    EntityDef(
        slug="grid_power_factor",
        name="Power Factor",
        ha_type="sensor",
        state_group="status",
        stat_key="power_factor",
        device_class="power_factor",
        state_class="measurement",
        icon="mdi:angle-acute",
    ),
    EntityDef(
        slug="grid_connection_state",
        name="Grid Connection",
        ha_type="sensor",
        state_group="status",
        stat_key="connection_state",
        icon="mdi:transmission-tower-import",
        entity_category="diagnostic",
    ),
    EntityDef(
        slug="inverter_state",
        name="Inverter State",
        ha_type="sensor",
        state_group="status",
        stat_key="inverter_state",
        icon="mdi:power-settings",
        entity_category="diagnostic",
    ),
    EntityDef(
        slug="grid_export_kwh",
        name="Grid Export Energy",
        ha_type="sensor",
        state_group="energy",
        stat_key="grid_export_wh",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:transmission-tower-export",
        value_scale=0.001,
        value_precision=3,
    ),
    EntityDef(
        slug="grid_import_kwh",
        name="Grid Import Energy",
        ha_type="sensor",
        state_group="energy",
        stat_key="grid_import_wh",
        unit="kWh",
        device_class="energy",
        state_class="total_increasing",
        icon="mdi:transmission-tower-import",
        value_scale=0.001,
        value_precision=3,
    ),

    # === TEMPERATURES (from read_grid_status) ===
    EntityDef(
        slug="ambient_temp_c",
        name="Ambient Temperature",
        ha_type="sensor",
        state_group="status",
        stat_key="ambient_temp_c",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        icon="mdi:thermometer",
        entity_category="diagnostic",
    ),
    EntityDef(
        slug="cabinet_temp_c",
        name="Cabinet Temperature",
        ha_type="sensor",
        state_group="status",
        stat_key="cabinet_temp_c",
        unit="°C",
        device_class="temperature",
        state_class="measurement",
        icon="mdi:thermometer",
        entity_category="diagnostic",
    ),

    # === SOLAR (from read_solar_status) ===
    EntityDef(
        slug="solar_power_kw",
        name="Solar Power",
        ha_type="sensor",
        state_group="solar",
        stat_key="ac_power_w",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:solar-power",
        value_scale=0.001,
        value_precision=3,
    ),

    # === EXTENSION REGISTERS (from read_solar_status → extension) ===
    EntityDef(
        slug="home_load_kw",
        name="Home Load",
        ha_type="sensor",
        state_group="power",
        stat_key="home_load_ext",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:home-lightning-bolt",
        value_scale=0.001,
        value_precision=3,
    ),
    EntityDef(
        slug="pv_total_kw",
        name="PV Total Power",
        ha_type="sensor",
        state_group="solar",
        stat_key="pv_total",
        unit="kW",
        device_class="power",
        state_class="measurement",
        icon="mdi:solar-power-variant",
        value_scale=0.001,
        value_precision=3,
    ),

    # === OPERATING MODE (from read_native_mode) ===
    EntityDef(
        slug="operating_mode_sensor",
        name="Operating Mode",
        ha_type="sensor",
        state_group="status",
        stat_key="mode_name",
        icon="mdi:cog-outline",
        entity_category="diagnostic",
    ),

    # === CONTROL (from read_control_status) ===
    EntityDef(
        slug="control_mode",
        name="Control Mode",
        ha_type="sensor",
        state_group="status",
        stat_key="loc_rem_ctl_name",
        icon="mdi:remote",
        entity_category="diagnostic",
    ),
    EntityDef(
        slug="wset_enabled",
        name="Remote Power Control",
        ha_type="sensor",
        state_group="status",
        stat_key="wset_enabled",
        icon="mdi:toggle-switch",
        entity_category="diagnostic",
    ),
    EntityDef(
        slug="power_setpoint_kw",
        name="Power Setpoint",
        ha_type="sensor",
        state_group="status",
        stat_key="wset_watts",
        unit="kW",
        device_class="power",
        icon="mdi:target",
        entity_category="diagnostic",
        value_scale=0.001,
        value_precision=3,
    ),

    # === WRITABLE CONTROLS ===
    EntityDef(
        slug="operating_mode",
        name="Operating Mode",
        ha_type="select",
        state_group="control",
        stat_key="mode_name",
        icon="mdi:cog",
        is_control=True,
        options=["Backup", "Self-Consumption", "TOU"],
    ),
    EntityDef(
        slug="self_reserve_pct",
        name="Self-Consumption Reserve SOC",
        ha_type="number",
        state_group="control",
        stat_key="self_reserve_pct",
        unit="%",
        icon="mdi:battery-charging-50",
        is_control=True,
        min_val=0,
        max_val=100,
        step=1,
    ),
    EntityDef(
        slug="tou_reserve_pct",
        name="TOU Reserve SOC",
        ha_type="number",
        state_group="control",
        stat_key="tou_reserve_pct",
        unit="%",
        icon="mdi:battery-clock",
        is_control=True,
        min_val=0,
        max_val=100,
        step=1,
    ),

]


def get_entity_by_slug(slug: str) -> EntityDef | None:
    for ent in BRIDGE_ENTITIES:
        if ent.slug == slug:
            return ent
    return None
