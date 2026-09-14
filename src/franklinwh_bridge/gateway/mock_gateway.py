"""Mock gateway — synthetic data source for multi-gateway demo/testing.

A mock gateway emits simulated ``Sample`` records on its poll interval
WITHOUT opening any Modbus connection.  It therefore never contends for a
real aGate's single Modbus session and gets a synthetic, collision-free
serial — making it safe to run alongside the real gateway purely to exercise
the multi-gateway selector and Site aggregation in the UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time

from franklinwh_bridge.modbus.sample import Sample, SampleBus

logger = logging.getLogger(__name__)


def mock_serial(gateway_id: str) -> str:
    """Synthetic, collision-free serial for a mock gateway."""
    return f"MOCK-{gateway_id.upper()}"


class MockController:
    """Stand-in controller for a mock gateway — no real Modbus I/O."""

    def __init__(self, gateway_id: str, ac_type: int = 0) -> None:
        # 0 single phase, 1 split phase (US aGate: L1+L2), 2 three phase.
        self.ac_type = ac_type
        self.gateway_id = gateway_id

    @property
    def serial(self) -> str:
        return mock_serial(self.gateway_id)

    def connect(self) -> bool:
        return True

    def disconnect(self) -> None:
        pass

    def read_nameplate(self) -> dict:
        return {
            "serial": self.serial,
            "model": "aGate (mock)",
            "manufacturer": "FranklinWH (mock)",
            "version": "MOCK",
        }


def synthetic_points(
    gateway_id: str,
    tick: int,
    ts: float | None = None,
    ac_type: int = 0,
    pv_channels: bool = True,
) -> dict:
    """Build one synthetic sample for *gateway_id* at *tick*.

    Uses wall-clock time-of-day to drive realistic diurnal patterns:
    solar follows a daytime bell curve, home load peaks at breakfast and
    dinner, battery charges on solar surplus and discharges in the evening.
    Per-gateway seed shifts capacity/timing so multiple mocks look distinct.
    """
    seed = sum(ord(c) for c in gateway_id) or 1

    # Per-gateway capacity/timing variations
    solar_peak_kw  = 3.5 + 2.5 * ((seed * 7)  % 100) / 100.0   # 3.5–6 kW peak solar
    home_base_w    = 400 + 400 * ((seed * 17) % 100) / 100.0    # 400–800 W base load
    batt_cap_kw    = 2.0 + 1.5 * ((seed * 13) % 100) / 100.0   # 2–3.5 kW battery limit
    soc_morning    = 25.0 + 20.0 * ((seed * 11) % 100) / 100.0  # morning SOC 25–45%

    # Fractional hour on the LOCAL clock.
    #
    # This was `now_ts % 86400`, which is seconds since UTC midnight — so the
    # synthetic solar bell peaked at UTC noon, i.e. 22:00 in Sydney. The mock's
    # "day" ran ~10 h out of phase with the timeline it is drawn on, making a
    # mock gateway show solar at night. The rest of the bridge evaluates
    # triggers and TOU windows on the local clock; the mock has to agree.
    now_ts = ts if ts is not None else time.time()
    lt = time.localtime(now_ts)
    hour = lt.tm_hour + lt.tm_min / 60.0 + lt.tm_sec / 3600.0

    # ── Solar: smooth bell curve, zero before dawn / after dusk ──
    # Half-sine from 6 h to 20 h; width per-gateway slightly varies
    dawn, dusk = 6.0, 20.0
    solar_factor = max(0.0, math.sin((hour - dawn) * math.pi / (dusk - dawn)))
    # Small cloud-cover ripple (slow tick-based noise, NOT a sine rollercoster)
    cloud_noise = 0.88 + 0.12 * math.sin(tick / 15.0 + seed * 0.7)
    solar_w = round(solar_peak_kw * 1000 * solar_factor ** 1.3 * cloud_noise, 1)

    # ── Home load: base + gaussian morning (8h) + evening (19h) peaks ──
    morning_peak = 900 * math.exp(-((hour - 8.0) ** 2) / 1.5)
    evening_peak = 1400 * math.exp(-((hour - 19.0) ** 2) / 3.5)
    load_jitter  = 1.0 + 0.04 * math.sin(tick / 4.0 + seed * 1.3)
    home_w = round((home_base_w + morning_peak + evening_peak) * load_jitter, 1)

    # ── Battery: absorb solar surplus during day, discharge in evening ──
    # net_surplus > 0 → charge; < 0 → discharge
    net_surplus = solar_w - home_w
    raw_batt = -net_surplus * 0.75   # negative = charging (convention)
    battery_w = round(max(-batt_cap_kw * 1000, min(batt_cap_kw * 1000, raw_batt)), 1)

    # ── SOC: integrate battery power across the day from a morning base ──
    # Approximate: morning low → afternoon high (solar charging) → evening low
    soc_noon_peak = min(95.0, soc_morning + 55.0)
    soc_curve = soc_morning + (soc_noon_peak - soc_morning) * max(0.0, math.sin(
        max(0.0, (hour - 6.0) * math.pi / 14.0)
    )) ** 0.7
    soc_jitter = 0.3 * math.sin(tick / 25.0 + seed * 0.5)
    soc = round(max(5.0, min(98.0, soc_curve + soc_jitter)), 1)

    grid_w = round(home_w - solar_w - battery_w, 1)

    if battery_w > 80:
        bstate = "Discharging"
    elif battery_w < -80:
        bstate = "Charging"
    else:
        bstate = "Standby"

    # ── Temperatures: vary with solar heat and battery activity ──
    temp_base_amb = 18 + (seed % 8)
    temp_base_cab = 28 + (seed % 12)
    heat_factor   = solar_factor * 0.5 + abs(battery_w) / 8000.0
    temp_drift    = math.sin(tick / 40.0 + seed * 0.4) * 1.5
    ambient_temp  = round(temp_base_amb + 4.0 * solar_factor + temp_drift, 1)
    cabinet_temp  = round(temp_base_cab + 6.0 * heat_factor + temp_drift, 1)

    # ── Operating mode: stable blocks (Self-Consumption day, TOU evening) ──
    if hour >= 22.0 or hour < 6.0:
        mode_name = "Emergency Backup"   # overnight reserve mode
    elif hour >= 16.0:
        mode_name = "TOU"                # evening peak tariff
    else:
        mode_name = "Self-Consumption"   # daytime self-use

    # Grid mode: Forming only when island/backup mode active, rare otherwise
    grid_mode = "Grid Forming" if mode_name == "Emergency Backup" else "Grid Following"

    points = {
        "soc":                soc,
        "battery_power_w":    battery_w,
        "battery_dc_power_w": battery_w,
        "grid_power_w":       grid_w,
        "total_solar":        solar_w,
        "home_load_ext":      home_w,
        "battery_state":      bstate,
        "connection_state":   "Connected",
        "inverter_state":     "Running",
        "mode_name":          mode_name,
        "grid_mode":          grid_mode,
        "ambient_temp_c":     ambient_temp,
        "cabinet_temp_c":     cabinet_temp,
    }

    # ── Battery nameplate + health ────────────────────────────────
    # Without these, 11 entities (capacity, SoH, rates, per-battery stats) sit
    # at "unknown" in HA on a mock device.
    capacity_wh = 13600.0            # one aPower ≈ 13.6 kWh
    points.update({
        "soh":                   96.0,
        "wh_rating":             capacity_wh,
        "available_capacity_wh": round(capacity_wh * soc / 100.0, 1),
        "max_charge_rate_w":     5000.0,
        "max_discharge_rate_w":  5000.0,
        "battery_current_a":     round(battery_w / 400.0, 2),   # ~400V pack
        "battery_temp_c":        cabinet_temp,
        "battery_health":        "OK",
        "battery_1_power_w":     battery_w,
        "battery_1_voltage_v":   round(380.0 + soc * 0.4, 1),
        "battery_1_temp_c":      cabinet_temp,
    })

    # ── Grid electrical + per-phase ───────────────────────────────
    # ac_type: 0 single phase (L1 only), 1 split phase (L1+L2, the US aGate —
    # which is what the FranklinWH Cloud and Local APIs assume unconditionally,
    # even for a single-phase install), 2 three phase.
    nominal_v = 240.0 if ac_type >= 1 else 230.0
    freq = 60.0 if ac_type >= 1 else 50.0
    phases = 1 if ac_type == 0 else (2 if ac_type == 1 else 3)

    # Split the total across phases. Deliberately UNEVEN — a perfectly balanced
    # mock hides exactly the imbalance bugs per-phase views exist to show.
    shares = {1: [1.0], 2: [0.55, 0.45], 3: [0.38, 0.34, 0.28]}[phases]
    pf = 0.97

    points.update({
        "grid_voltage_v":   nominal_v,
        "grid_frequency_hz": freq,
        "grid_current_a":   round(abs(grid_w) / nominal_v, 2),
        "grid_power_factor": pf,
        "apparent_power_va": round(abs(grid_w) / pf, 1),
        "reactive_power_var": round(abs(grid_w) * 0.2, 1),
    })
    for i, share in enumerate(shares, start=1):
        leg_w = round(grid_w * share, 1)
        points[f"voltage_l{i}_v"] = round(nominal_v + (i - 1) * 1.4, 1)
        points[f"current_l{i}_a"] = round(abs(leg_w) / nominal_v, 2)
        points[f"power_l{i}_w"] = leg_w
        points[f"pf_l{i}"] = pf
        points[f"va_l{i}"] = round(abs(leg_w) / pf, 1)
        points[f"var_l{i}"] = round(abs(leg_w) * 0.2, 1)
    if phases >= 2:
        points["voltage_l1l2_v"] = round(nominal_v * 2, 1)
    if phases == 3:
        points["voltage_l2l3_v"] = round(nominal_v * 1.732, 1)
        points["voltage_l3l1_v"] = round(nominal_v * 1.732, 1)

    # ── Solar channels ────────────────────────────────────────────
    # An aGate meters PV on a proximal port plus up to two remote channels
    # (e.g. a second array on its own inverter). Splitting the total lets the
    # remote-PV entities carry a value instead of sitting unknown.
    if pv_channels:
        points["pv_proximal"] = round(solar_w * 0.6, 1)
        points["pv_remote1"] = round(solar_w * 0.3, 1)
        points["pv_remote2"] = round(solar_w * 0.1, 1)
    else:
        points["pv_proximal"] = solar_w
        points["pv_remote1"] = 0.0
        points["pv_remote2"] = 0.0

    # ── Lifetime energy counters ──────────────────────────────────
    # Monotonic in tick so state_class=total_increasing never sees a decrease,
    # which HA would treat as a meter reset.
    kwh = tick / 360.0
    points.update({
        "grid_export_energy_wh":     round(1_500_000 + kwh * 700, 1),
        "grid_import_energy_wh":     round(2_400_000 + kwh * 900, 1),
        "pv_energy_total_wh":        round(9_800_000 + kwh * 1600, 1),
        "pv_energy_proximal_wh":     round(6_100_000 + kwh * 1000, 1),
        "dc_energy_discharged_wh":   round(3_200_000 + kwh * 800, 1),
        "dc_energy_charged_wh":      round(3_700_000 + kwh * 850, 1),
    })

    return points


class _MockPollerState:
    """Minimal stand-in for ModbusPoller.state (read by to_dict/list)."""

    last_poll_ts: float | None = None


class MockPoller:
    """Publishes synthetic samples to *sample_bus* every *poll_interval* s."""

    def __init__(
        self,
        sample_bus: SampleBus,
        gateway_id: str,
        poll_interval: int,
        ac_type: int = 0,
    ) -> None:
        self._bus = sample_bus
        self._gateway_id = gateway_id
        self._interval = max(1, poll_interval)
        self._task: asyncio.Task | None = None
        self._tick = 0
        self.ac_type = ac_type
        self.state = _MockPollerState()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            pts = synthetic_points(
                self._gateway_id, self._tick, ac_type=self.ac_type,
            )
            self._tick += 1
            self.state.last_poll_ts = time.time()
            await self._bus.publish(Sample.now(self._gateway_id, pts))
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
