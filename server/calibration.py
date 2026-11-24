"""Automatic light calibration and sky condition (spec sections 16.3, 16.4).

Values are kept per data source, so simulated light never calibrates the real
sensor.
"""

import json
import sqlite3
import threading
from dataclasses import dataclass
from statistics import fmean, pstdev

from server import sun
from server.config import Config

MINUTE = 60_000
DAY = 86_400_000
RECALIBRATE_MS = 3_600_000
MIN_SPAN_MS = DAY
MIN_HIGH_SUN_MINUTES = 120
MIN_NIGHT_MINUTES = 60
HIGH_SUN = 15.0
DEEP_NIGHT = -12.0
SATURATION_SPAN_MS = 3 * DAY
SATURATION_SHARE = 0.02
MIN_SIGMA = 1.0  # raw units; an often-zero night reading has no spread
CLEARNESS_MAX = 1.2
SKY_WINDOW_MIN = 15
SKY_MIN_MINUTES = 8


@dataclass
class Calibration:
    light_k: float | None = None  # raw units per W/m2; None until calibrated
    night_baseline: float = 0.0
    night_sigma: float = MIN_SIGMA
    saturation: float | None = None
    max_seen: float = 0.0
    updated_ms: int | None = None  # data time of the last calibration

    @property
    def calibrated(self) -> bool:
        return self.light_k is not None

    def light_norm(self, light: float | None) -> float | None:
        """Light as a fraction of full clear-noon sun."""
        if light is None:
            return None
        if self.light_k is None:
            return min(1.0, max(0.0, light) / self.max_seen) if self.max_seen else 0.0
        value = (light - self.night_baseline) / (self.light_k * 1000)
        return min(CLEARNESS_MAX, max(0.0, value))

    def clear_sky_norm(self, elevation_deg: float) -> float | None:
        """Expected light_norm under a clear sky."""
        return sun.clear_sky(elevation_deg) / 1000 if self.calibrated else None

    def clearness(self, light: float | None, elevation_deg: float) -> float | None:
        """Measured over clear-sky light; None unless the sun is high enough."""
        if light is None or self.light_k is None or elevation_deg <= sun.DAY_ABOVE:
            return None
        expected = self.light_k * sun.clear_sky(elevation_deg)
        return min(CLEARNESS_MAX, max(0.0, (light - self.night_baseline) / expected))

    def is_saturated(self, light: float | None) -> bool:
        return (
            self.saturation is not None
            and light is not None
            and light >= self.saturation
        )


def percentile(values: list[float], share: float) -> float:
    """Percentile with linear interpolation."""
    ordered = sorted(values)
    position = share * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def weighted_median(counts: dict[float, int]) -> float:
    half = sum(counts.values()) / 2
    seen = 0
    for value in sorted(counts):
        seen += counts[value]
        if seen >= half:
            return value
    raise ValueError("no values")


def calibrate(
    conn: sqlite3.Connection, config: Config, source: str, now: int
) -> Calibration:
    """Compute calibration from the last calibration_days before `now`."""
    start = now - config.light.calibration_days * DAY
    rows = conn.execute(
        f"""SELECT r.ts_ms / {MINUTE} * {MINUTE} AS t, r.light_raw AS light,
                   COUNT(*) AS n
            FROM readings r JOIN raw_packets p ON p.id = r.raw_id
            WHERE r.ts_ms BETWEEN ? AND ? AND r.quality = 'good' AND p.source = ?
            GROUP BY t, light""",
        (start, now, source),
    ).fetchall()
    result = Calibration(updated_ms=now)
    if not rows:
        return result

    elevations = {t: sun.elevation(config.station, t) for t in {r["t"] for r in rows}}
    span = max(elevations) - min(elevations)
    result.max_seen = max(r["light"] for r in rows)

    daytime = [r for r in rows if elevations[r["t"]] > 0]
    if config.light.saturation_detect and daytime and span >= SATURATION_SPAN_MS:
        top = max(r["light"] for r in daytime)
        at_top = sum(r["n"] for r in daytime if r["light"] == top)
        if at_top >= SATURATION_SHARE * sum(r["n"] for r in daytime):
            result.saturation = top

    night: dict[float, int] = {}
    night_minutes = set()
    for r in rows:
        if elevations[r["t"]] < DEEP_NIGHT:
            night[r["light"]] = night.get(r["light"], 0) + r["n"]
            night_minutes.add(r["t"])
    if night:
        result.night_baseline = weighted_median(night)
        deviations: dict[float, int] = {}
        for value, n in night.items():
            distance = abs(value - result.night_baseline)
            deviations[distance] = deviations.get(distance, 0) + n
        result.night_sigma = max(MIN_SIGMA, 1.4826 * weighted_median(deviations))

    # Minute means under a high sun; saturated samples understate the light.
    totals: dict[int, list[float]] = {}
    for r in rows:
        if elevations[r["t"]] > HIGH_SUN and not result.is_saturated(r["light"]):
            total = totals.setdefault(r["t"], [0.0, 0])
            total[0] += r["light"] * r["n"]
            total[1] += r["n"]
    if (
        config.light.auto_calibrate
        and span >= MIN_SPAN_MS
        and len(totals) >= MIN_HIGH_SUN_MINUTES
        and len(night_minutes) >= MIN_NIGHT_MINUTES
    ):
        k = percentile(
            [
                (total / n - result.night_baseline) / sun.clear_sky(elevations[t])
                for t, (total, n) in totals.items()
            ],
            0.95,
        )
        # A covered sensor sees no light and must not set the scale.
        if k * 1000 > 5 * result.night_sigma:
            result.light_k = k
    return result


def save(conn: sqlite3.Connection, source: str, calibration: Calibration) -> None:
    values = {
        "light_k": calibration.light_k,
        "night_baseline": calibration.night_baseline,
        "night_sigma": calibration.night_sigma,
        "light_saturation": calibration.saturation,
        "light_max_seen": calibration.max_seen,
    }
    details = json.dumps({"source": source})
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO calibration VALUES (?, ?, ?, ?)",
            [
                (f"{source}.{key}", value, calibration.updated_ms, details)
                for key, value in values.items()
            ],
        )


def load(conn: sqlite3.Connection, source: str) -> Calibration | None:
    rows = {
        row["key"]: row
        for row in conn.execute("SELECT key, value, updated_ms FROM calibration")
    }
    get = lambda key: rows[f"{source}.{key}"]["value"]  # noqa: E731
    if f"{source}.light_k" not in rows:
        return None
    return Calibration(
        light_k=get("light_k"),
        night_baseline=get("night_baseline"),
        night_sigma=get("night_sigma"),
        saturation=get("light_saturation"),
        max_seen=get("light_max_seen"),
        updated_ms=rows[f"{source}.light_k"]["updated_ms"],
    )


class Calibrator:
    """Thread-safe cache of the calibration in use for each source."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, Calibration] = {}

    def get(self, conn: sqlite3.Connection, source: str) -> Calibration:
        with self._lock:
            if source not in self._cache:
                stored = load(conn, source)
                if stored is None:
                    top = conn.execute(
                        """SELECT MAX(r.light_raw)
                           FROM readings r JOIN raw_packets p ON p.id = r.raw_id
                           WHERE r.quality = 'good' AND p.source = ?""",
                        (source,),
                    ).fetchone()[0]
                    stored = Calibration(max_seen=top or 0.0)
                self._cache[source] = stored
            return self._cache[source]

    def put(self, source: str, calibration: Calibration) -> None:
        with self._lock:
            self._cache[source] = calibration

    def observe(self, calibration: Calibration, light: float | None) -> None:
        """Let the fallback scale grow with the light seen."""
        with self._lock:
            calibration.max_seen = max(calibration.max_seen, light or 0.0)

    def is_due(self, calibration: Calibration, ts_ms: int) -> bool:
        updated = calibration.updated_ms
        return updated is None or abs(ts_ms - updated) >= RECALIBRATE_MS


def sky_condition(
    config: Config, calibration: Calibration, minutes: list, ts_ms: int
) -> tuple[str | None, float | None]:
    """Return (sky, 15-minute clearness) from minute averages.

    The sky is None in daytime without enough calibrated data.
    """
    phase = sun.phase(config.station, ts_ms)
    if phase != "day":
        return phase, None
    recent = [
        row for row in minutes if ts_ms - SKY_WINDOW_MIN * MINUTE < row["t"] <= ts_ms
    ]
    values = []
    for row in recent:
        elevation = sun.elevation(config.station, row["t"])
        kc = calibration.clearness(row["light_raw"], elevation)
        if kc is not None:
            values.append(kc)
    if len(values) < SKY_MIN_MINUTES:
        return None, None
    mean, spread = fmean(values), pstdev(values)
    saturated = sum(calibration.is_saturated(row["light_raw"]) for row in recent)
    if saturated * 2 > len(recent):
        # The sensor cannot show more, so clearness is only a lower bound.
        return "very_bright", min(1.0, mean)
    return classify_sky(mean, spread), mean


def classify_sky(clearness: float, spread: float) -> str:
    if spread >= 0.12:
        return "partly_cloudy"
    if clearness >= 0.75:
        return "clear" if spread < 0.08 else "partly_cloudy"
    return "mostly_cloudy" if clearness >= 0.35 else "overcast"
