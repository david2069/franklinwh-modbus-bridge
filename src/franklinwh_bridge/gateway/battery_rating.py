"""Battery power rating: the reference every force command is sized against (#133, #134).

The aGate is commanded with a percentage (Model 704 ``WSetPct``) of a power the
bridge has to supply. The aGate reports its nameplate charge and discharge
rating over SunSpec (Model 702 ``WChaRteMaxRtg``/``WDisChaRteMaxRtg``; 5000 W
each on the reference site), and that is the rating used (owner, 2026-10-19,
#269). In order of preference:

* ``calibrated`` — the owner ran a full-rate dispatch from the official app
  (TOU battery dispatch: force charge from grid / force discharge to grid, with
  import and export unlimited) and the bridge recorded the peak;
* ``m702_confirmed`` — the nameplate, and the battery has been seen at 80–115 %
  of it;
* ``m702_nameplate`` — the nameplate as reported.

With no M702 rating at all (missing or failed read) the rating is unknown and
force commands are refused: the bridge never assumes 5000 W.

The guard from #133 stays: on a site with four aPower 2, "100 %" ran at 20 kW.
A rating stops being trusted when the battery is seen above it (the passive
detector) or a command overshoots (the guard in ``CommandHandler``), until it is
calibrated. Mock gateways are always trusted: they drive simulated state.

Telemetry sign: ``battery_power_w`` > 0 is discharging, < 0 charging.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DIRECTIONS = ("charge", "discharge")

#: Model 702 is confirmed when the battery has run at this share of it...
CONFIRM_MIN = 0.80
#: ...and contradicted when it has been seen above this share (plus slack).
CONTRADICT_MAX = 1.15
CONTRADICT_SLACK_W = 300
#: A calibration needs at least this long and this much power to count.
CALIBRATION_MIN_S = 60
CALIBRATION_MIN_W = 500
#: Readings above this are corrupt, not power (matches the metrics guard).
ABSURD_W = 150_000
#: Persist a new peak only when it beats the stored one by this much.
PEAK_SAVE_STEP_W = 50

CALIBRATE_HINT = (
    "Calibrate it: Settings → Site & Gateways → this gateway → Battery power rating"
)


@dataclass
class Decision:
    """Whether a force command in one direction may be sent, and against what."""

    direction: str
    trusted: bool
    rating_w: int | None
    source: str  # calibrated | m702_confirmed | m702_nameplate | contradicted | unknown | mock
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "trusted": self.trusted,
            "rating_w": self.rating_w,
            "source": self.source,
            "reason": self.reason,
        }


@dataclass
class _Dir:
    m702_w: int | None = None
    peak_w: int | None = None
    peak_at: float | None = None
    calibrated_w: int | None = None
    calibrated_at: float | None = None
    contradicted_at: float | None = None
    contradicted_reason: str | None = None


@dataclass
class _Calibration:
    direction: str
    started_at: float
    peak_w: int = 0
    soc_at_start: float | None = None


@dataclass
class BatteryRating:
    gateway_id: str
    mock: bool = False
    dirs: dict[str, _Dir] = field(default_factory=lambda: {d: _Dir() for d in DIRECTIONS})
    calibration: _Calibration | None = None
    _db: aiosqlite.Connection | None = None

    # ── decision ─────────────────────────────────────────────────────────

    def decision(self, direction: str) -> Decision:
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        d = self.dirs[direction]
        if self.mock:
            return Decision(direction, True, d.m702_w or 5000, "mock",
                            "mock gateway: simulated battery")
        contradicted = d.contradicted_at is not None and (
            d.calibrated_at is None or d.contradicted_at > d.calibrated_at)
        if contradicted:
            return Decision(direction, False, None, "contradicted",
                            f"{d.contradicted_reason}. {CALIBRATE_HINT}")
        if d.calibrated_w:
            return Decision(direction, True, d.calibrated_w, "calibrated",
                            f"calibrated at {d.calibrated_w} W")
        if d.m702_w:
            if d.peak_w and d.peak_w > d.m702_w * CONTRADICT_MAX + CONTRADICT_SLACK_W:
                return Decision(direction, False, None, "contradicted",
                                f"battery seen at {d.peak_w} W, above the aGate's rating of "
                                f"{d.m702_w} W. {CALIBRATE_HINT}")
            if d.peak_w and d.peak_w >= d.m702_w * CONFIRM_MIN:
                return Decision(direction, True, d.m702_w, "m702_confirmed",
                                f"aGate rating {d.m702_w} W, seen at {d.peak_w} W")
            # The aGate's SunSpec nameplate is a valid rating (owner, 2026-10-19,
            # #269): used as reported. The guard above still withdraws it if
            # the battery is ever seen above it.
            return Decision(direction, True, d.m702_w, "m702_nameplate",
                            f"aGate nameplate rating (SunSpec M702) {d.m702_w} W")
        return Decision(direction, False, None, "unknown",
                        f"battery {direction} power rating unknown. {CALIBRATE_HINT}")

    def status(self) -> dict[str, Any]:
        cal = self.calibration
        return {
            "gateway_id": self.gateway_id,
            "charge": {**self.decision("charge").as_dict(), **_dir_dict(self.dirs["charge"])},
            "discharge": {**self.decision("discharge").as_dict(),
                          **_dir_dict(self.dirs["discharge"])},
            "calibration": None if cal is None else {
                "direction": cal.direction,
                "started_at": cal.started_at,
                "elapsed_s": round(time.time() - cal.started_at, 1),
                "peak_w": cal.peak_w,
                "soc_at_start": cal.soc_at_start,
            },
        }

    # ── evidence ─────────────────────────────────────────────────────────

    async def record_m702(self, charge_w: int | None, discharge_w: int | None) -> None:
        changed = False
        for direction, value in (("charge", charge_w), ("discharge", discharge_w)):
            if value and value > 0 and self.dirs[direction].m702_w != int(value):
                self.dirs[direction].m702_w = int(value)
                changed = True
        if changed:
            await self._save()

    async def observe(self, points: dict[str, Any], ts: float | None = None) -> None:
        """Feed one poll: track peaks, the calibration window, and the detector."""
        bw = points.get("battery_power_w")
        if bw is None:
            return
        try:
            bw = float(bw)
        except (TypeError, ValueError):
            return
        if abs(bw) > ABSURD_W:
            return
        now = ts or time.time()
        direction = "discharge" if bw > 0 else "charge"
        mag = int(round(abs(bw)))
        if self.calibration and self.calibration.direction == direction:
            self.calibration.peak_w = max(self.calibration.peak_w, mag)
        d = self.dirs[direction]
        if mag and (d.peak_w is None or mag >= d.peak_w + PEAK_SAVE_STEP_W):
            before = self.decision(direction)
            d.peak_w, d.peak_at = mag, now
            after = self.decision(direction)
            if before.trusted and after.source == "contradicted":
                d.contradicted_at = now
                d.contradicted_reason = after.reason.split(". ")[0]
                logger.warning("Gateway %s: %s — force %s commands blocked until recalibrated",
                               self.gateway_id, d.contradicted_reason, direction)
            await self._save()

    async def contradict(self, direction: str, reason: str) -> None:
        """The overshoot guard saw the battery run above the rating it was sent against."""
        d = self.dirs[direction]
        d.contradicted_at = time.time()
        d.contradicted_reason = reason
        logger.warning("Gateway %s: %s — force %s commands blocked until recalibrated",
                       self.gateway_id, reason, direction)
        await self._save()

    # ── calibration ──────────────────────────────────────────────────────

    def start_calibration(self, direction: str, soc: float | None) -> None:
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}")
        self.calibration = _Calibration(direction, time.time(), 0, soc)

    async def finish_calibration(self) -> Decision:
        cal = self.calibration
        if cal is None:
            raise ValueError("no calibration is running")
        elapsed = time.time() - cal.started_at
        if elapsed < CALIBRATION_MIN_S:
            raise ValueError(f"run the dispatch for at least {CALIBRATION_MIN_S} s "
                             f"(it has been {elapsed:.0f} s)")
        if cal.peak_w < CALIBRATION_MIN_W:
            raise ValueError(f"the battery only reached {cal.peak_w} W while {cal.direction}"
                             f"ing — start the {cal.direction} dispatch in the app first")
        d = self.dirs[cal.direction]
        d.calibrated_w, d.calibrated_at = cal.peak_w, time.time()
        self.calibration = None
        logger.info("Gateway %s: %s power calibrated at %d W",
                    self.gateway_id, cal.direction, d.calibrated_w)
        await self._save()
        return self.decision(cal.direction)

    def cancel_calibration(self) -> None:
        self.calibration = None

    # ── persistence ──────────────────────────────────────────────────────

    async def load(self, db: aiosqlite.Connection | None) -> None:
        """Load stored evidence; seed peaks from recorded history the first time."""
        if db is None:  # no store (tests): evidence lives in memory only
            return
        self._db = db
        async with db.execute(
            "SELECT direction, m702_w, peak_w, peak_at, calibrated_w, calibrated_at, "
            "contradicted_at, contradicted_reason FROM battery_ratings WHERE gateway_id = ?",
            (self.gateway_id,),
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            if r[0] in self.dirs:
                self.dirs[r[0]] = _Dir(*r[1:])
        if not rows and not self.mock:
            await self._seed_peaks_from_history(db)

    async def _seed_peaks_from_history(self, db: aiosqlite.Connection) -> None:
        """A site that has dispatched at full power already holds the evidence."""
        sql = ("SELECT MAX(battery_w), MAX(-battery_w) FROM {t} "
               "WHERE gateway_id = ? AND ABS(battery_w) <= ?")
        best = {"discharge": 0, "charge": 0}
        for table in ("metrics", "metrics_archive"):
            try:
                async with db.execute(sql.format(t=table), (self.gateway_id, ABSURD_W)) as cur:
                    row = await cur.fetchone()
            except aiosqlite.Error:
                continue
            if row:
                best["discharge"] = max(best["discharge"], int(row[0] or 0))
                best["charge"] = max(best["charge"], int(row[1] or 0))
        for direction, value in best.items():
            if value > 0:
                self.dirs[direction].peak_w = value
                self.dirs[direction].peak_at = time.time()
        await self._save()

    async def _save(self) -> None:
        if self._db is None:
            return
        for direction, d in self.dirs.items():
            await self._db.execute(
                "INSERT INTO battery_ratings (gateway_id, direction, m702_w, peak_w, peak_at, "
                "calibrated_w, calibrated_at, contradicted_at, contradicted_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(gateway_id, direction) DO UPDATE SET m702_w=excluded.m702_w, "
                "peak_w=excluded.peak_w, peak_at=excluded.peak_at, "
                "calibrated_w=excluded.calibrated_w, calibrated_at=excluded.calibrated_at, "
                "contradicted_at=excluded.contradicted_at, "
                "contradicted_reason=excluded.contradicted_reason",
                (self.gateway_id, direction, d.m702_w, d.peak_w, d.peak_at, d.calibrated_w,
                 d.calibrated_at, d.contradicted_at, d.contradicted_reason),
            )
        await self._db.commit()


def _dir_dict(d: _Dir) -> dict[str, Any]:
    return {
        "m702_w": d.m702_w,
        "peak_w": d.peak_w,
        "calibrated_w": d.calibrated_w,
        "calibrated_at": d.calibrated_at,
        "contradicted_at": d.contradicted_at,
    }
