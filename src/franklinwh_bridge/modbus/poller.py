"""Async Modbus poll loop wrapping franklinwh-modbus FranklinWHController."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from typing import Any

from franklinwh_bridge.modbus.sample import Sample, SampleBus

logger = logging.getLogger(__name__)

# Modbus 0xFFFF (uint16 "not available") sentinel.  Extension registers
# sporadically return this during concurrent access or firmware glitches.
_MODBUS_UINT16_NAN = 65535

# Extension register keys that must be guarded against 0xFFFF corruption.
# These are the named keys the library's _read_extension_solar() returns.
_EXTENSION_POWER_KEYS = {
    "total_solar",
    "pv_total",
    "pv_proximal",
    "pv_remote1",
    "pv_remote2",
    "home_load_ext",
    "home_load_ext_quantized",
}

# Absolute ceiling for any single power metric (W).  Any value above this
# is treated as register corruption and replaced with the previous good
# reading (or 0 if no previous).  15 kW covers even the largest residential
# FranklinWH installations with 3× aGate stacking.
MAX_SANE_POWER_W = 15_000

# Read every poll cycle (10s default) — live telemetry
POLL_METHODS = [
    "read_battery_status",
    "read_grid_status",
    "read_solar_status",
    "read_control_status",
    "read_native_mode",
    "read_alarms",
]

# Read once at startup — static device info and ratings
STARTUP_METHODS = [
    "read_nameplate",
]


@dataclass
class PollerState:
    connected: bool = False
    last_poll_ts: float | None = None
    last_error: str | None = None
    consecutive_errors: int = 0
    polls_total: int = 0
    errors_total: int = 0


class ModbusPoller:
    """Polls a FranklinWH aGate via franklinwh-modbus and emits Samples."""

    def __init__(
        self,
        controller: Any,
        sample_bus: SampleBus,
        gateway_id: str = "default",
        poll_interval: int = 30,
        stats: Any | None = None,
        modbus_lock: asyncio.Lock | None = None,
    ) -> None:
        self._controller = controller
        self._bus = sample_bus
        self._gateway_id = gateway_id
        self._poll_interval = poll_interval
        self._state = PollerState()
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._startup_points: dict[str, Any] = {}
        self._ac_type: int = 0  # 0=Single, 1=Split, 2=Three-Phase
        self._battery_port_count: int = 1  # M714 NPrt (1 = single battery)
        self._vreg_client: Any | None = None  # cached pymodbus client
        self._last_good_ext: dict[str, int | float] = {}  # previous good extension values
        self._stats = stats  # OperationalStats (optional)
        self._modbus_lock = modbus_lock or asyncio.Lock()

    @property
    def ac_type(self) -> int:
        """Detected AC wiring type: 0=Single, 1=Split, 2=Three-Phase."""
        return self._ac_type

    @property
    def battery_port_count(self) -> int:
        """Number of battery ports detected from M714 NPrt (default 1)."""
        return self._battery_port_count

    @property
    def state(self) -> PollerState:
        return self._state

    async def _connect(self) -> bool:
        async with self._modbus_lock:
            try:
                result = await asyncio.to_thread(self._controller.connect)
                self._state.connected = bool(result)
                if self._state.connected:
                    if self._state.consecutive_errors > 0:
                        logger.warning(
                            "Reconnected to aGate at %s after %d errors",
                            self._controller.ip_address,
                            self._state.consecutive_errors,
                        )
                        if self._stats:
                            self._stats.record_conn_recovery()
                    else:
                        logger.info("Connected to aGate at %s", self._controller.ip_address)
                return self._state.connected
            except Exception as exc:
                self._state.connected = False
                self._state.last_error = str(exc)
                logger.error("Connection failed: %s", exc)
                return False

    async def _disconnect(self) -> None:
        async with self._modbus_lock:
            self._close_vreg_client()
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._controller.disconnect)
            self._state.connected = False

    async def _read_startup_points(self) -> None:
        """Read static data once after connect — nameplate and M702 ratings."""
        points: dict[str, Any] = {}

        async with self._modbus_lock:
            for method_name in STARTUP_METHODS:
                method = getattr(self._controller, method_name, None)
                if method is None:
                    continue
                try:
                    result = await asyncio.to_thread(method)
                    if isinstance(result, dict):
                        for k, v in result.items():
                            if isinstance(v, dict):
                                points.update(v)
                            else:
                                points[k] = v
                except Exception as exc:
                    logger.warning("Startup read %s failed: %s", method_name, exc)

            # M702 nameplate ratings (max charge/discharge rates) — static
            m702 = self._controller.get_model(702)
            if m702:
                try:
                    m702.read()
                    sf = getattr(m702, "W_SF", None)
                    sf_val = sf.value if sf and sf.value is not None else 0
                    for attr, key in [
                        ("WChaRteMaxRtg", "max_charge_rate_w"),
                        ("WDisChaRteMaxRtg", "max_discharge_rate_w"),
                    ]:
                        pt = getattr(m702, attr, None)
                        if pt and pt.value is not None:
                            points[key] = int(pt.value * (10**sf_val))
                except Exception as exc:
                    logger.debug("M702 rating read failed: %s", exc)

            # Read static-only models once (e.g. M703 DER Enter Service)
            # These have no POLL_METHOD calling .read(), so raw values won't
            # appear in the Explorer unless we read them here.
            for mid in self._STARTUP_READ_MODELS:
                model = self._controller.get_model(mid)
                if model:
                    try:
                        model.read()
                        logger.debug("Startup read M%d OK", mid)
                    except Exception as exc:
                        logger.debug("Startup read M%d failed: %s", mid, exc)

            # Detect AC wiring type (single/split/three-phase) from M701
            self._detect_ac_type()

        self._startup_points = points
        logger.info(
            "Startup reads complete: %d points (M1 nameplate, M702 ratings, ACType=%d)",
            len(points),
            self._ac_type,
        )

    def _detect_ac_type(self) -> None:
        """Read M701 ACType once to determine AC wiring configuration.

        ACType enum: 0=Single Phase, 1=Split Phase, 2=Three Phase.
        Called during startup after the first successful read of M701.
        """
        m701 = self._controller.get_model(701)
        if not m701:
            logger.warning("M701 not available — defaulting to single-phase")
            return

        try:
            m701.read()
            ac_pt = getattr(m701, "ACType", None)
            if ac_pt and hasattr(ac_pt, "value") and ac_pt.value is not None:
                val = int(ac_pt.value)
                if 0 <= val <= 2:
                    self._ac_type = val
                    AC_TYPE_NAMES = {0: "Single Phase", 1: "Split Phase", 2: "Three Phase"}
                    logger.info(
                        "AC type detected: %s (ACType=%d)",
                        AC_TYPE_NAMES.get(val, "Unknown"),
                        val,
                    )
                else:
                    logger.warning("Unexpected ACType value %d — defaulting to single-phase", val)
            else:
                logger.info("ACType point not available — defaulting to single-phase")
        except Exception as exc:
            logger.warning("ACType detection failed: %s — defaulting to single-phase", exc)

    def _get_scale_factor(self, model: Any, sf_name: str) -> int:
        """Get scale factor value from a model, default to 0.

        SunSpec scale factors are in range [-10, 10]. Values outside this
        range (e.g. -32768 / 0x8000) indicate corrupt data.
        """
        sf_point = getattr(model, sf_name, None)
        if sf_point and hasattr(sf_point, "value"):
            val = sf_point.value
            if val is not None and -10 <= val <= 10:
                return val
        return 0

    def _read_grid_phases(self) -> dict[str, Any]:
        """Read per-phase grid measurements from M701 with scale factors applied.

        Uses the model object already read by POLL_METHODS (read_grid_status),
        so the model has cached data from its last `.read()` call.
        No additional Modbus traffic.
        """
        points: dict[str, Any] = {}
        m701 = self._controller.get_model(701)
        if not m701:
            return points

        # Store ac_type_code for dashboard display
        ac_pt = getattr(m701, "ACType", None)
        if ac_pt and hasattr(ac_pt, "value") and ac_pt.value is not None:
            points["ac_type_code"] = int(ac_pt.value)
        else:
            points["ac_type_code"] = self._ac_type

        # Scale factors (already read by read_grid_status)
        sf_w = self._get_scale_factor(m701, "W_SF")
        sf_v = self._get_scale_factor(m701, "V_SF")
        sf_a = self._get_scale_factor(m701, "A_SF")
        sf_pf = self._get_scale_factor(m701, "PF_SF")
        sf_va = self._get_scale_factor(m701, "VA_SF")
        sf_var = self._get_scale_factor(m701, "Var_SF")

        def _scaled(pt_name: str, sf: int, precision: int) -> float | None:
            pt = getattr(m701, pt_name, None)
            if pt and hasattr(pt, "value") and pt.value is not None:
                return round(pt.value * (10**sf), precision)
            return None

        # Per-phase mapping: (sunspec_point, output_key, scale_factor, precision)
        phase_map: list[tuple[str, str, int, int]] = [
            # Line-neutral voltages
            ("VL1", "voltage_l1_v", sf_v, 1),
            ("VL2", "voltage_l2_v", sf_v, 1),
            ("VL3", "voltage_l3_v", sf_v, 1),
            # Line-line voltages
            ("VL1L2", "voltage_l1l2_v", sf_v, 1),
            ("VL2L3", "voltage_l2l3_v", sf_v, 1),
            ("VL3L1", "voltage_l3l1_v", sf_v, 1),
            # Current
            ("AL1", "current_l1_a", sf_a, 1),
            ("AL2", "current_l2_a", sf_a, 1),
            ("AL3", "current_l3_a", sf_a, 1),
            # Power
            ("WL1", "power_l1_w", sf_w, 0),
            ("WL2", "power_l2_w", sf_w, 0),
            ("WL3", "power_l3_w", sf_w, 0),
            # Power factor
            ("PFL1", "pf_l1", sf_pf, 3),
            ("PFL2", "pf_l2", sf_pf, 3),
            ("PFL3", "pf_l3", sf_pf, 3),
            # Apparent power
            ("VAL1", "va_l1", sf_va, 0),
            ("VAL2", "va_l2", sf_va, 0),
            ("VAL3", "va_l3", sf_va, 0),
            # Reactive power
            ("VarL1", "var_l1", sf_var, 0),
            ("VarL2", "var_l2", sf_var, 0),
            ("VarL3", "var_l3", sf_var, 0),
        ]

        for sunspec_pt, out_key, sf, precision in phase_map:
            val = _scaled(sunspec_pt, sf, precision)
            if val is not None:
                points[out_key] = val

        return points

    def _sanitize_extension_values(self, points: dict[str, Any]) -> None:
        """Filter Modbus 0xFFFF sentinel and physically impossible values.

        The aGate extension registers (15500-15513) sporadically return
        0xFFFF (65535) during concurrent Modbus access or firmware glitches.
        The library's validation sums three 0xFFFF registers to 196,605 and
        passes it through as ``total_solar``.

        This method:
        1. Rejects any extension power key whose value is exactly 0xFFFF
           or exceeds MAX_SANE_POWER_W.
        2. Recalculates ``total_solar`` from the (cleaned) component values
           when any component was rejected.
        3. Caches last-known-good values for fallback.
        """
        corrupted = False

        for key in _EXTENSION_POWER_KEYS:
            val = points.get(key)
            if val is None:
                continue
            if val == _MODBUS_UINT16_NAN or val > MAX_SANE_POWER_W:
                prev = self._last_good_ext.get(key, 0)
                logger.warning(
                    "Rejected corrupted %s=%s (0xFFFF sentinel or > %dW), "
                    "using previous good value %s",
                    key,
                    val,
                    MAX_SANE_POWER_W,
                    prev,
                )
                points[key] = prev
                corrupted = True
                if self._stats:
                    self._stats.record_sanitization()

        # If any component was corrupted, recalculate total_solar from clean parts
        if corrupted and "total_solar" in points:
            pv_prox = points.get("pv_proximal", 0) or 0
            pv_r1 = points.get("pv_remote1", 0) or 0
            pv_r2 = points.get("pv_remote2", 0) or 0
            pv_total = points.get("pv_total", 0) or 0
            individual_sum = pv_prox + pv_r1 + pv_r2

            if pv_total > 0 and abs(pv_total - individual_sum) < 100:
                points["total_solar"] = pv_total
            elif individual_sum > 0:
                points["total_solar"] = individual_sum
            else:
                points["total_solar"] = pv_total

        # Also guard raw vreg_ keys for 0xFFFF (Explorer display)
        for key in list(points):
            if key.startswith("vreg_") and points[key] == _MODBUS_UINT16_NAN:
                # Keep the raw value for diagnostics but flag it
                pass  # vreg_ keys are raw — leave as-is for Explorer visibility

        # Cache good values for next cycle
        for key in _EXTENSION_POWER_KEYS:
            val = points.get(key)
            if val is not None and val != _MODBUS_UINT16_NAN and val <= MAX_SANE_POWER_W:
                self._last_good_ext[key] = val

    async def _poll_once(self) -> Sample:
        """Run all read methods and merge into a single Sample.

        Acquires the shared Modbus lock for the entire poll cycle to
        prevent command handler writes from interleaving with reads
        (which causes TCP response timeouts and metric gaps).
        """
        points: dict[str, Any] = {}
        quality = "ok"

        # Include cached startup points (nameplate, ratings)
        points.update(self._startup_points)

        async with self._modbus_lock:
            for method_name in POLL_METHODS:
                method = getattr(self._controller, method_name, None)
                if method is None:
                    continue
                try:
                    result = await asyncio.to_thread(method)
                    if isinstance(result, dict):
                        for k, v in result.items():
                            if isinstance(v, dict):
                                points.update(v)
                            else:
                                points[k] = v
                except Exception as exc:
                    logger.warning("Read %s failed: %s", method_name, exc)
                    quality = "stale"

            # Derive battery health label from 713.Sta enum
            _STA_MAP = {0: "OK", 1: "Warning", 3: "Error"}
            sta = points.get("status_raw")
            if sta is not None:
                points["battery_health"] = _STA_MAP.get(sta, f"Unknown ({sta})")

            # Normalise battery state: "Idle" → "Standby" to align with
            # SunSpec M713 Sta enum and FranklinWH app terminology.
            # The library returns "Idle" for |DCW| ≤ 50W; we remap here
            # so all consumers (MQTT, dashboard, HA) see "Standby".
            if points.get("battery_state") == "Idle":
                points["battery_state"] = "Standby"

            # Derive human-readable text for M704 WSetMod enum.
            # SunSpec 704 WSetMod: 0=Off, 1=Pct of WMax (used by bridge).
            # NOTE: The library writes WSetMod=0 during send_command (library
            # quirk), but uses WSetPct for Pct-mode control.  We infer the
            # effective mode from WSetEna + WSetPct so the UI reflects reality.
            _WSET_MOD_MAP = {0: "Off", 1: "Pct"}
            wm = points.get("wset_mode")
            wset_ena = points.get("wset_enabled")
            wset_pct = points.get("wset_pct")
            if wset_ena and wset_pct:
                # Active Pct-mode command — WSetMod register may read 0 but
                # the aGate is effectively in Pct mode.
                points["wset_mode_name"] = "Pct"
            elif wm is not None:
                points["wset_mode_name"] = _WSET_MOD_MAP.get(
                    wm, f"Unknown ({wm})"
                )

            # Guard against Modbus 0xFFFF register corruption in extension values
            self._sanitize_extension_values(points)

            try:
                extra = await asyncio.to_thread(self._read_extra_points)
                points.update(extra)
            except Exception as exc:
                logger.warning("Extra point reads failed: %s", exc)

            # Fallback: derive critical battery keys from raw model cache
            # when the library's read_battery_status() returned {} (failed
            # silently).  Prevents dashboard/HA "--" on transient failures.
            self._derive_battery_from_model(points)

            # Extract per-battery stack data from individual_batteries array
            # (new in franklinwh-modbus multi-battery update)
            self._extract_per_battery(points)

        if not points:
            quality = "error"

        return Sample.now(self._gateway_id, points, quality)

    # Models read during POLL_METHODS (their point values are already cached)
    _POLLED_MODELS = [1, 502, 701, 702, 704, 713, 714, 715]

    # Static models read once at startup — configuration that doesn't change.
    # These are read separately because no POLL_METHOD calls model.read() on them.
    _STARTUP_READ_MODELS = [703]

    def _extract_raw_model_values(self) -> dict[str, Any]:
        """Extract raw SunSpec point values from already-read model objects.

        Called after POLL_METHODS have run, so model objects have cached data
        from their last `.read()` call.  No additional Modbus traffic.

        Keys are formatted as ``{model_id}.{point_name}`` (e.g. ``704.WSetEna``)
        so the SunSpec Explorer can match them to catalog entries.
        """
        raw: dict[str, Any] = {}
        # Include both polled models and startup-only models
        all_models = set(self._POLLED_MODELS) | set(self._STARTUP_READ_MODELS)
        for mid in sorted(all_models):
            model = self._controller.get_model(mid)
            if model is None:
                continue
            points_dict = getattr(model, "points", None)
            if not points_dict:
                continue
            for pt_name, pt_obj in points_dict.items():
                val = getattr(pt_obj, "value", None)
                if val is not None:
                    raw[f"{mid}.{pt_name}"] = val
        return raw

    def _derive_battery_from_model(self, points: dict[str, Any]) -> None:
        """Fill in critical battery keys from raw model cache when missing.

        The library's ``read_battery_status()`` returns ``soh``, ``wh_rating``,
        ``wh_available`` etc., but if it fails silently (returns ``{}``) those
        keys vanish from the sample.  Since ``_extract_raw_model_values()``
        has already populated the raw ``713.SoH``, ``713.WHRtg`` etc. from
        the model's cached last-read data, we can compute the derived keys
        as a safety net.  This prevents dashboard/HA entities from showing
        "--" during transient read failures.

        Only fills keys that are NOT already present — never overwrites a
        value from the library's high-level method.
        """
        m713 = self._controller.get_model(713)
        if not m713:
            return

        # Scale factors from the model (already cached from last .read())
        sf_pct = self._get_scale_factor(m713, "Pct_SF")
        sf_wh = self._get_scale_factor(m713, "WH_SF")

        # (derived_key, model_attr, scale_factor, precision)
        _FALLBACKS: list[tuple[str, str, int, int]] = [
            ("soc", "SoC", sf_pct, 1),
            ("soh", "SoH", sf_pct, 1),
            ("wh_rating", "WHRtg", sf_wh, 0),
            ("wh_available", "WHAvail", sf_wh, 0),
        ]

        filled = []
        for key, attr, sf, precision in _FALLBACKS:
            if key in points:
                continue  # library already provided it
            pt = getattr(m713, attr, None)
            if pt and hasattr(pt, "value") and pt.value is not None:
                points[key] = round(pt.value * (10 ** sf), precision)
                filled.append(key)

        if filled:
            logger.info(
                "Derived %d battery key(s) from M713 model cache: %s",
                len(filled),
                ", ".join(filled),
            )

    def _extract_per_battery(self, points: dict[str, Any]) -> None:
        """Extract per-battery stack telemetry from individual_batteries.

        The library's ``read_battery_status()`` returns an
        ``individual_batteries`` list when M714 has repeating blocks
        (multi-battery NPrt > 1).  Each entry has port, power_w,
        voltage_v, temp_c.  We flatten these into point keys like
        ``battery_1_power_w``, ``battery_2_voltage_v`` etc.

        Also updates ``_battery_port_count`` for entity filtering.
        """
        batteries = points.get("individual_batteries")
        if not batteries or not isinstance(batteries, list):
            return

        self._battery_port_count = len(batteries)
        points["battery_port_count"] = self._battery_port_count

        for bat in batteries:
            port = bat.get("port", 0)
            if port < 1:
                continue
            pw = bat.get("power_w")
            if pw is not None:
                points[f"battery_{port}_power_w"] = pw
            vv = bat.get("voltage_v")
            if vv is not None:
                points[f"battery_{port}_voltage_v"] = round(vv, 1)
            tc = bat.get("temp_c")
            if tc is not None:
                points[f"battery_{port}_temp_c"] = round(tc, 1)

    def _read_extra_points(self) -> dict[str, Any]:
        """Read points not covered by the standard controller methods."""
        points: dict[str, Any] = {}

        # ── Raw SunSpec model values for the Explorer ───────────
        # POLL_METHODS already called model.read() on M502, M701, M704,
        # M713, M714, M715.  Extract raw point values from the cached
        # model objects (no additional Modbus traffic).
        points.update(self._extract_raw_model_values())

        # ── Per-phase grid measurements (scaled from M701) ─────
        points.update(self._read_grid_phases())

        # M714 DC energy counters (battery lifetime charge/discharge)
        m714 = self._controller.get_model(714)
        if m714:
            m714.read()
            inj = getattr(m714, "DCWhInj", None)
            if inj and inj.value is not None:
                points["dc_energy_discharged_wh"] = int(inj.value)
            absorb = getattr(m714, "DCWhAbs", None)
            if absorb and absorb.value is not None:
                points["dc_energy_charged_wh"] = int(absorb.value)

        # Raw vendor extension register reads via pymodbus
        self._read_vendor_registers(points)

        return points

    def _get_vreg_client(self) -> Any:
        """Get or create a reusable pymodbus client for vendor register reads.

        Reusing one TCP connection (instead of connect/close every poll cycle)
        reduces concurrent Modbus connections to the aGate, lowering the risk
        of 0xFFFF register corruption from firmware concurrency issues.
        """
        from pymodbus.client import ModbusTcpClient

        if self._vreg_client is None or not self._vreg_client.connected:
            if self._vreg_client is not None:
                with contextlib.suppress(Exception):
                    self._vreg_client.close()
            self._vreg_client = ModbusTcpClient(
                self._controller.ip_address, port=self._controller.port
            )
            self._vreg_client.connect()
        return self._vreg_client

    def _close_vreg_client(self) -> None:
        """Close the cached pymodbus client (called during disconnect)."""
        if self._vreg_client is not None:
            with contextlib.suppress(Exception):
                self._vreg_client.close()
            self._vreg_client = None

    def _read_vendor_registers(self, points: dict[str, Any]) -> None:
        """Read FranklinWH vendor extension registers via raw Modbus.

        Three ranges on a reusable pymodbus connection:
        - 15000-15039: Undocumented registers (raw values only)
        - 15510-15513: PV energy uint32 composites (the library doesn't
          return these as named keys)
        - 16000-16002: High-resolution extension registers

        Registers 15500-15509 are NOT read here — the library already reads
        them via _read_extension_solar().  Removing this duplicate read
        eliminates concurrent access to the same registers, which was the
        primary trigger for 0xFFFF corruption on the aGate firmware.

        Raw vreg_155xx keys for the SunSpec Explorer are derived from the
        library's named keys via _derive_vreg_from_library().
        """
        try:
            client = self._get_vreg_client()
            uid = self._controller.unit_id

            # Range 1: 15000-15039 (undocumented vendor range)
            r1 = client.read_holding_registers(15000, count=40, device_id=uid)
            if not r1.isError():
                for i, val in enumerate(r1.registers):
                    points[f"vreg_{15000 + i}"] = val

            # Range 2: 15510-15513 (PV energy counters — uint32 high:low pairs)
            # Only these 4 registers; 15500-15509 handled by library.
            r2 = client.read_holding_registers(15510, count=4, device_id=uid)
            if not r2.isError():
                for i, val in enumerate(r2.registers):
                    points[f"vreg_{15510 + i}"] = val
                points["pv_energy_total_wh"] = (r2.registers[0] << 16) | r2.registers[1]
                points["pv_energy_proximal_wh"] = (r2.registers[2] << 16) | r2.registers[3]

            # Range 3: 16000-16002 (high-resolution extension registers)
            r3 = client.read_holding_registers(16000, count=3, device_id=uid)
            if not r3.isError():
                for i, val in enumerate(r3.registers):
                    points[f"vreg_{16000 + i}"] = val

        except Exception as exc:
            logger.debug("Vendor register reads failed: %s", exc)
            # Connection may be broken — force reconnect next cycle
            self._close_vreg_client()

        # Derive 15500-range vreg_ keys from library data (no extra Modbus traffic)
        self._derive_vreg_from_library(points)

    def _derive_vreg_from_library(self, points: dict[str, Any]) -> None:
        """Populate vreg_155xx keys from the library's extension register read.

        The library's _read_extension_solar() already reads registers 15500-15509
        and returns named keys.  We map those back to raw vreg_ keys for the
        SunSpec Explorer, avoiding a duplicate Modbus read of those addresses.

        Register-to-key mapping (from franklinwh-modbus controller.py):
          15500: (unnamed, battery-related — not available from library)
          15501: (unnamed — not available from library)
          15502: pv_total
          15503: pv_proximal
          15504: pv_remote1
          15505: pv_remote2
          15506: home_load_ext_quantized
          15507: ongrid_mode
          15508: self_reserve
          15509: tou_reserve
        """
        _KEY_TO_REG = {
            "pv_total": 15502,
            "pv_proximal": 15503,
            "pv_remote1": 15504,
            "pv_remote2": 15505,
            "home_load_ext_quantized": 15506,
            "ongrid_mode": 15507,
            "self_reserve": 15508,
            "tou_reserve": 15509,
        }

        for key, reg in _KEY_TO_REG.items():
            val = points.get(key)
            if val is not None:
                vreg_key = f"vreg_{reg}"
                if vreg_key not in points:
                    points[vreg_key] = int(val)

    def _backoff_delay(self) -> float:
        return min(5.0 * (2**self._state.consecutive_errors), 60.0)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._state.connected and not await self._connect():
                delay = self._backoff_delay()
                logger.info("Reconnect in %.0fs", delay)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                continue

            # Read static points after each (re)connect
            if not self._startup_points:
                await self._read_startup_points()

            try:
                sample = await self._poll_once()
                self._state.last_poll_ts = time.time()
                self._state.polls_total += 1

                if sample.quality == "error":
                    self._state.consecutive_errors += 1
                    self._state.errors_total += 1
                    self._state.last_error = "All reads failed"
                    if self._state.connected:
                        logger.warning("Modbus connection lost — all reads failed")
                        if self._stats:
                            self._stats.record_conn_drop()
                    self._state.connected = False
                    self._startup_points = {}  # Re-read on reconnect
                else:
                    self._state.consecutive_errors = 0
                    self._state.last_error = None

                # Record poll quality in operational stats
                if self._stats:
                    self._stats.record_poll(sample.quality)

                await self._bus.publish(sample)

            except Exception as exc:
                self._state.consecutive_errors += 1
                self._state.errors_total += 1
                self._state.last_error = str(exc)
                if self._state.connected:
                    logger.warning("Modbus connection lost: %s", exc)
                    if self._stats:
                        self._stats.record_conn_drop()
                self._state.connected = False
                self._startup_points = {}  # Re-read on reconnect
                logger.error("Poll failed: %s", exc)
                if self._stats:
                    self._stats.record_error(str(exc))

            # Periodically flush stats to DB
            if self._stats:
                await self._stats.maybe_flush()

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Poller started (interval=%ds)", self._poll_interval)

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._disconnect()
        logger.info("Poller stopped")

    async def poll_once(self) -> Sample:
        """Single poll for CLI 'bridge run --once'."""
        if not self._state.connected:
            await self._connect()
        if not self._startup_points:
            await self._read_startup_points()
        sample = await self._poll_once()
        self._state.last_poll_ts = time.time()
        self._state.polls_total += 1
        await self._bus.publish(sample)
        return sample
