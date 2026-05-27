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

    @property
    def ac_type(self) -> int:
        """Detected AC wiring type: 0=Single, 1=Split, 2=Three-Phase."""
        return self._ac_type

    @property
    def state(self) -> PollerState:
        return self._state

    async def _connect(self) -> bool:
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
                else:
                    logger.info("Connected to aGate at %s", self._controller.ip_address)
            return self._state.connected
        except Exception as exc:
            self._state.connected = False
            self._state.last_error = str(exc)
            logger.error("Connection failed: %s", exc)
            return False

    async def _disconnect(self) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self._controller.disconnect)
        self._state.connected = False

    async def _read_startup_points(self) -> None:
        """Read static data once after connect — nameplate and M702 ratings."""
        points: dict[str, Any] = {}

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
                        points[key] = int(pt.value * (10 ** sf_val))
            except Exception as exc:
                logger.debug("M702 rating read failed: %s", exc)

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
                return round(pt.value * (10 ** sf), precision)
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

    async def _poll_once(self) -> Sample:
        """Run all read methods and merge into a single Sample."""
        points: dict[str, Any] = {}
        quality = "ok"

        # Include cached startup points (nameplate, ratings)
        points.update(self._startup_points)

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

        try:
            extra = await asyncio.to_thread(self._read_extra_points)
            points.update(extra)
        except Exception as exc:
            logger.warning("Extra point reads failed: %s", exc)

        if not points:
            quality = "error"

        return Sample.now(self._gateway_id, points, quality)

    # Models read during POLL_METHODS (their point values are already cached)
    _POLLED_MODELS = [1, 502, 701, 702, 704, 713, 714, 715]

    def _extract_raw_model_values(self) -> dict[str, Any]:
        """Extract raw SunSpec point values from already-read model objects.

        Called after POLL_METHODS have run, so model objects have cached data
        from their last `.read()` call.  No additional Modbus traffic.

        Keys are formatted as ``{model_id}.{point_name}`` (e.g. ``704.WSetEna``)
        so the SunSpec Explorer can match them to catalog entries.
        """
        raw: dict[str, Any] = {}
        for mid in self._POLLED_MODELS:
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

    def _read_vendor_registers(self, points: dict[str, Any]) -> None:
        """Read FranklinWH vendor extension registers via raw Modbus.

        Three ranges:
        - 15000-15039: Undocumented registers (raw values only)
        - 15500-15509: Documented extension registers (raw + library overlap)
        - 15510-15513: PV energy totals (uint32 composed values)
        """
        try:
            from pymodbus.client import ModbusTcpClient

            client = ModbusTcpClient(
                self._controller.ip_address, port=self._controller.port
            )
            client.connect()
            uid = self._controller.unit_id

            # Range 1: 15000-15039 (undocumented vendor range)
            r1 = client.read_holding_registers(15000, count=40, device_id=uid)
            if not r1.isError():
                for i, val in enumerate(r1.registers):
                    points[f"vreg_{15000 + i}"] = val

            # Range 2: 15500-15513 (documented extension range, full raw read)
            # The library already reads these via _read_extension_solar() and
            # provides named keys (pv_total, ongrid_mode, etc.), but registers
            # 15500-15501 have no named mapping.  Raw vreg_ keys fill the gap.
            r2 = client.read_holding_registers(15500, count=14, device_id=uid)
            if not r2.isError():
                for i, val in enumerate(r2.registers):
                    points[f"vreg_{15500 + i}"] = val
                # uint32 composed values (15510-15511 and 15512-15513)
                points["pv_energy_total_wh"] = (
                    (r2.registers[10] << 16) | r2.registers[11]
                )
                points["pv_energy_proximal_wh"] = (
                    (r2.registers[12] << 16) | r2.registers[13]
                )

            client.close()
        except Exception as exc:
            logger.debug("Vendor register reads failed: %s", exc)

    def _backoff_delay(self) -> float:
        return min(5.0 * (2 ** self._state.consecutive_errors), 60.0)

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
                    self._state.connected = False
                    self._startup_points = {}  # Re-read on reconnect
                else:
                    self._state.consecutive_errors = 0
                    self._state.last_error = None

                await self._bus.publish(sample)

            except Exception as exc:
                self._state.consecutive_errors += 1
                self._state.errors_total += 1
                self._state.last_error = str(exc)
                if self._state.connected:
                    logger.warning("Modbus connection lost: %s", exc)
                self._state.connected = False
                self._startup_points = {}  # Re-read on reconnect
                logger.error("Poll failed: %s", exc)

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )

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
