"""Rule engine for inferred conditions (spec section 17).

Rules look at one-minute averages and are evaluated once per minute of *data*
time, so simulated, replayed and delayed data behave like live data.
"""

import math
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from statistics import fmean, median
from zoneinfo import ZoneInfo

from server import aggregates, calibration, derive, sun
from server.alerts import AlertManager
from server.calibration import Calibration, percentile
from server.config import Config

MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR
HISTORY_MINUTES = 25 * 60
WARM_UP_MINUTES = 180
UNKNOWN_CLEAR_MINUTES = 60
MAX_GAP_MINUTES = 24 * 60
SPIKE_MERGE_MS = 2 * MINUTE
SUSTAINED_LIGHT_MS = 60_000
MIN_LIGHT_SEEN = 20.0


@dataclass
class Result:
    """Outcome of one rule for one minute; None means "cannot tell"."""

    active: bool
    title: str = ""
    explanation: str = ""
    severity: str = "info"
    confidence: str = "medium"
    details: dict = field(default_factory=dict)
    clear: bool | None = None  # default: not active
    clear_now: bool = False  # end at once, without the clear period
    start_ms: int | None = None  # one-shot events: when it began

    def is_clear(self) -> bool:
        return not self.active if self.clear is None else self.clear


class State:
    """What the rules can see at minute `now`."""

    def __init__(self, config: Config, light: Calibration):
        self.config = config
        self.light = light
        self.minutes: deque[dict] = deque(maxlen=HISTORY_MINUTES)
        self.now = 0
        self.foreign: deque[tuple[int, int]] = deque()  # (minute, tx_id)
        self.battery: tuple[float, float] | None = None
        self.active_types: set[str] = set()

    @property
    def row(self) -> dict | None:
        last = self.minutes[-1] if self.minutes else None
        return last if last and last["t"] == self.now else None

    def window(self, length: int, before: int = 0) -> list[dict]:
        """Rows of the `length` minutes that end `before` minutes ago."""
        end = self.now - before * MINUTE
        start = end - length * MINUTE
        rows = []
        for row in reversed(self.minutes):
            if row["t"] <= start:
                break
            if row["t"] <= end:
                rows.append(row)
        rows.reverse()
        return rows

    def covered(self, length: int, before: int = 0) -> list[dict] | None:
        """Like window(), but None unless at least half of it has data."""
        rows = self.window(length, before)
        return rows if len(rows) * 2 >= length else None

    def mean(self, key: str, length: int, before: int = 0) -> float | None:
        rows = self.covered(length, before)
        values = [r[key] for r in rows or [] if r[key] is not None]
        return fmean(values) if values else None

    def slope(self, key: str, length: int) -> float | None:
        rows = self.covered(length)
        points = [(r["t"], r[key]) for r in rows or [] if r[key] is not None]
        return derive.slope_per_hour(points)

    def local(self) -> datetime:
        return datetime.fromtimestamp(
            self.now / 1000, ZoneInfo(self.config.station.timezone)
        )

    def elevation(self) -> float:
        return sun.elevation(self.config.station, self.now)

    def scale(self) -> tuple[Calibration, bool]:
        """Calibration to use, and whether it is only provisional.

        Until the sensor is calibrated, the brightest light of the past day is
        taken as a clear sky.
        """
        if self.light.calibrated:
            return self.light, False
        ratios = [
            r["light_raw"] / sun.clear_sky(r["elev"])
            for r in self.minutes
            if r["elev"] > sun.DAY_ABOVE
        ]
        if len(ratios) < 30:
            return Calibration(), True
        return Calibration(light_k=percentile(ratios, 0.95) or None), True

    def clearness(self, rows: list[dict] | None) -> tuple[float | None, bool]:
        """Mean clearness of the rows, and whether it is provisional."""
        scale, provisional = self.scale()
        values = [scale.clearness(r["light_raw"], r["elev"]) for r in rows or []]
        values = [v for v in values if v is not None]
        return (fmean(values) if len(values) >= 5 else None), provisional

    def light_share(self, light: float) -> float | None:
        """Light as a fraction of full sun, or of the brightest light seen."""
        if self.light.calibrated:
            return self.light.light_norm(light)
        top = max((r["light_raw"] for r in self.minutes), default=0.0)
        return light / top if top >= MIN_LIGHT_SEEN else None

    def night_threshold(self) -> float:
        k = self.config.light.night_light_sigma
        if self.light.calibrated:
            return self.light.night_baseline + k * self.light.night_sigma
        night = [r["light_raw"] for r in self.minutes if r["elev"] < sun.NIGHT_BELOW]
        base = median(night) if night else 0.0
        return base + k * calibration.MIN_SIGMA


class Rule:
    type = ""
    persist = 0  # minutes the condition must hold
    clear_after = 0  # minutes the clear condition must hold
    one_shot = False
    refractory = 0  # minutes before a one-shot event may repeat

    def evaluate(self, s: State) -> Result | None:
        raise NotImplementedError


class Freeze(Rule):
    type, persist, clear_after = "freeze", 5, 15

    def evaluate(self, s):
        if s.row is None:
            return None
        t, limit = s.row["temp_c"], s.config.thresholds.freeze_c
        return Result(
            t <= limit, "Freezing now",
            f"Temperature is {t:.1f} °C, at or below {limit:.1f} °C.",
            "warning", "high", {"temp_c": t}, clear=t > limit + 0.5,
        )


class FrostRiskTonight(Rule):
    type, persist, clear_after = "frost_risk_tonight", 10, 60

    def evaluate(self, s):
        slope = s.slope("temp_c", 60)
        if s.row is None or slope is None:
            return None
        t, dew = s.row["temp_c"], s.row["dew_point_c"]
        up = s.elevation() > 0
        local = s.local()
        if dew is None or (up and local.hour < 15):
            return Result(False, clear_now=up and local.hour < 12)
        daylight = [r for r in s.window(14 * 60) if r["elev"] > sun.DAY_ABOVE][-120:]
        clearness, provisional = s.clearness(daylight)
        sky = (
            f"the sky was mostly clear (clearness {clearness:.2f}) in the last daylight"
            if clearness is not None
            else "cloud information is unavailable"
        )
        limit = s.config.thresholds.frost_watch_c + 3
        return Result(
            t <= limit and slope <= -0.5 and dew <= 2
            and (clearness is None or clearness >= 0.6),
            "Frost possible tonight",
            f"Temperature is {t:.1f} °C and falling {-slope:.1f} °C per hour, the dew "
            f"point is {dew:.1f} °C, and {sky}.",
            "watch", "low" if clearness is None or provisional else "medium",
            {"temp_c": t, "slope": slope, "dew_point_c": dew, "clearness": clearness},
        )


class FrostLikely(Rule):
    type, persist, clear_after = "frost_likely", 15, 30

    def evaluate(self, s):
        if s.row is None:
            return None
        t = s.row["temp_c"]
        point = derive.frost_point(t, s.row["rh_pct"])
        if point is None:
            return Result(False)
        gap = t - point
        return Result(
            t <= 1 and gap <= 1.5 and s.elevation() < 0,
            "Frost likely forming",
            f"Temperature is {t:.1f} °C, only {gap:.1f} °C above the frost point "
            f"({point:.1f} °C), at night.",
            "warning", "medium", {"temp_c": t, "frost_point_c": point},
        )


class FogDew(Rule):
    type, persist, clear_after = "fog_dew", 15, 30

    def evaluate(self, s):
        if s.row is None or s.row["dew_point_c"] is None:
            return None
        t, rh = s.row["temp_c"], s.row["rh_pct"]
        spread = t - s.row["dew_point_c"]
        limits = s.config.thresholds
        kind = "dew" if t > 0 else "frost"
        text = (
            f"Temperature is within {spread:.1f} °C of the dew point and humidity is "
            f"{rh:.0f}%."
        )
        clearness, _ = s.clearness(s.window(15))
        title = f"{kind.capitalize()} likely"
        if s.elevation() < 0:
            title = f"Fog or {kind} likely"
        elif clearness is not None and clearness < 0.5:
            title = f"Fog or {kind} likely"
            text += f" Light is only {clearness:.0%} of a clear sky."
        return Result(
            spread <= limits.fog_spread_c and rh >= limits.fog_rh,
            title, text, "info", "medium",
            {"spread_c": spread, "rh_pct": rh, "clearness": clearness},
        )


class RainOnset(Rule):
    type, clear_after = "rain_onset", 30

    def evaluate(self, s):
        rows = s.covered(30)
        if s.row is None or rows is None:
            return None
        rh, t = s.row["rh_pct"], s.row["temp_c"]
        lowest = min(rows, key=lambda r: r["rh_pct"])
        rise = rh - lowest["rh_pct"]
        drop = max(r["temp_c"] for r in rows) - t
        minutes = max(1, (s.now - lowest["t"]) // MINUTE)
        now_kc, _ = s.clearness(s.window(5))
        old_kc, _ = s.clearness(s.window(10, before=20))
        confidence = "medium"
        if now_kc is None or old_kc is None:
            light = "Light data unavailable."
        else:
            light = f"Clearness went from {old_kc:.2f} to {now_kc:.2f}."
            confidence = "high" if old_kc - now_kc >= 0.3 else "medium"
        return Result(
            rise >= 10 and rh >= 90 and drop >= 1,
            "Rain may be starting",
            f"Humidity rose from {lowest['rh_pct']:.0f}% to {rh:.0f}% in {minutes} min "
            f"and temperature fell {drop:.1f} °C. {light}",
            "watch", confidence,
            {"rh_rise": rise, "temp_drop": drop, "kc_now": now_kc, "kc_before": old_kc},
            clear=rh < 85,
        )


class StormDarkening(Rule):
    type, persist, clear_after = "storm_darkening", 5, 30

    def evaluate(self, s):
        if s.row is None:
            return None
        if s.elevation() <= sun.DAY_ABOVE:
            return Result(False)
        now, provisional = s.clearness(s.covered(5))
        before = [
            kc
            for back in (15, 20, 25)
            if (kc := s.clearness(s.covered(5, before=back))[0]) is not None
        ]
        if now is None or not before:
            return None
        drop = max(before) - now
        return Result(
            drop >= 0.5, "Sudden darkening",
            f"Clearness fell from {max(before):.2f} to {now:.2f} within about 20 min.",
            "info", "low" if provisional else "medium",
            {"before": max(before), "now": now},
        )


class FrontPassage(Rule):
    type, one_shot, refractory = "front_passage", True, 180

    def evaluate(self, s):
        t, dew = s.mean("temp_c", 10), s.mean("dew_point_c", 10)
        if t is None or dew is None:
            return None
        for length in (60, 90, 120):
            old_t = s.mean("temp_c", 10, before=length)
            old_dew = s.mean("dew_point_c", 10, before=length)
            if old_t is None or old_dew is None:
                continue
            dt, dd = t - old_t, dew - old_dew
            if abs(dt) >= 3 and abs(dd) >= 3 and dt * dd > 0:
                verb, kind = ("fell", "cold") if dt < 0 else ("rose", "warm")
                return Result(
                    True, "Weather change — front passing",
                    f"Temperature {verb} {abs(dt):.1f} °C and dew point {verb} "
                    f"{abs(dd):.1f} °C in {length} min, as in a {kind} front.",
                    "info", "medium", {"dt": dt, "dtd": dd, "minutes": length},
                    start_ms=s.now - length * MINUTE,
                )
        return Result(False)


class Heat(Rule):
    type, persist, clear_after = "heat", 10, 30

    def evaluate(self, s):
        if s.row is None:
            return None
        t, rh = s.row["temp_c"], s.row["rh_pct"]
        hi = derive.instant(t, rh)["feels_like_c"]
        x = s.config.thresholds
        band = None
        for limit, name, severity in (
            (x.heat_caution_hi_c, "Heat caution", "watch"),
            (x.heat_extreme_caution_hi_c, "Heat: extreme caution", "warning"),
            (x.heat_danger_hi_c, "Heat danger", "critical"),
            (x.heat_extreme_danger_hi_c, "Extreme heat danger", "critical"),
        ):
            if hi >= limit:
                band = (name, severity)
        name, severity = band or ("", "info")
        return Result(
            band is not None, name,
            f"It feels like {hi:.1f} °C ({t:.1f} °C with {rh:.0f}% humidity).",
            severity, "high", {"heat_index_c": hi}, clear=hi < x.heat_caution_hi_c - 1,
        )


class SolarHeating(Rule):
    type, persist, clear_after = "solar_heating", 15, 30

    def evaluate(self, s):
        rows = s.covered(30)
        if s.row is None or rows is None:
            return None
        if s.elevation() <= 0:
            return Result(False)
        slope = s.slope("temp_c", 30)
        r = pearson([x["light_raw"] for x in rows], [x["temp_c"] for x in rows])
        clearness, provisional = s.clearness(s.window(10))
        if slope is None or r is None or clearness is None:
            return Result(False)
        return Result(
            r >= 0.7 and clearness >= 0.6 and slope >= 1.5,
            "Sensor in direct sun — temperature may read high",
            f"Temperature rises {slope:.1f} °C per hour in step with the light "
            f"(correlation {r:.2f}) under a bright sky (clearness {clearness:.2f}).",
            "info", "low" if provisional else "medium",
            {"slope": slope, "r": r, "clearness": clearness},
        )


class SensorCovered(Rule):
    type, clear_after = "sensor_covered", 10

    def evaluate(self, s):
        if s.row is None:
            return None
        share = s.light_share(s.row["light_raw"])
        if share is None:
            return None
        rows = s.covered(120) or []
        shares = [s.light_share(r["light_raw"]) for r in rows]
        dark = (
            bool(rows)
            and rows[0]["t"] <= s.now - 115 * MINUTE
            and all(r["elev"] > 15 for r in rows)
            and max(shares) < 0.03
        )
        snow = s.row["temp_c"] <= 1
        return Result(
            dark,
            "Sensor may be covered (possibly snow)" if snow
            else "Sensor may be covered (snow, leaves, debris)",
            f"The sun has been up for at least 2 h, but light stayed at or below "
            f"{max(shares, default=0):.1%} of "
            f"{'full sun' if s.light.calibrated else 'the brightest light seen'}. "
            f"Temperature is "
            f"{s.row['temp_c']:.1f} °C.",
            "watch", "medium" if s.light.calibrated else "low",
            {"light_share": share, "temp_c": s.row["temp_c"]}, clear=share > 0.1,
        )


class TagMoved(Rule):
    type, persist, clear_after = "tag_moved", 5, 30

    def evaluate(self, s):
        recent, before = s.covered(5), s.covered(60, before=5)
        if recent is None or before is None:
            return None
        now = median(r["rssi_dbm"] for r in recent)
        base = median(r["rssi_dbm"] for r in before)
        settled = median(r["rssi_dbm"] for r in s.window(30))
        return Result(
            abs(now - base) >= 6, "Signal changed — tag moved or blocked?",
            f"Signal strength went from {base:.1f} to {now:.1f} dBm.",
            "info", "medium", {"before_dbm": base, "now_dbm": now},
            # A steady new level counts as the new normal.
            clear=abs(now - settled) < 3,
        )


class BatteryTrend(Rule):
    type = "battery_trend"

    def evaluate(self, s):
        if s.battery is None:
            return None
        rssi, packets = s.battery
        return Result(
            rssi <= -0.5 and packets < 0, "Battery may be weakening",
            f"Over the last week the signal fell {-rssi:.1f} dB per day and the number "
            f"of packets received changed by {packets:.0f} per day.",
            "info", "low", {"rssi_per_day": rssi, "packets_per_day": packets},
        )


class SensorStuck(Rule):
    type = "sensor_stuck"

    def evaluate(self, s):
        rows = s.covered(31)
        if s.row is None or rows is None or rows[0]["t"] > s.now - 30 * MINUTE:
            return None
        same = all(
            r["temp_min"] == r["temp_max"] == rows[0]["temp_min"]
            and r["rh_min"] == r["rh_max"] == rows[0]["rh_min"]
            for r in rows
        )
        lights = [r["light_raw"] for r in rows]
        moved = max(lights) - min(lights)
        return Result(
            same and moved > max(10.0, 0.05 * max(lights)), "Readings not changing",
            f"Temperature ({s.row['temp_c']:.2f} °C) and humidity "
            f"({s.row['rh_pct']:.0f}%) stayed identical for 30 min while light changed "
            f"by {moved:.0f}.",
            "watch", "medium", {"light_change": moved}, clear=not same,
        )


class ForeignTags(Rule):
    type = "foreign_tags"

    def evaluate(self, s):
        tags = sorted({tx for _, tx in s.foreign})
        return Result(
            bool(tags), "Other tags nearby",
            f"Heard {len(tags)} other tag(s) in the last hour: "
            f"{', '.join(map(str, tags[:10]))}.",
            "info", "high", {"tx_ids": tags[:50]},
        )


class OpenWindows(Rule):
    type, persist, clear_after = "open_windows", 10, 10

    def evaluate(self, s):
        if s.row is None or s.row["dew_point_c"] is None:
            return None
        t, dew = s.row["temp_c"], s.row["dew_point_c"]
        local = s.local()
        good = derive.should_open_windows(
            t, dew, s.config.home.indoor_setpoint_c,
            s.config.thresholds.open_window_dewpoint_max_c,
            bool(s.active_types & {"rain_onset", "fog_dew", "heat"}),
            local.hour + local.minute / 60, s.elevation() > 0,
        )
        return Result(
            good, "Good time to open windows",
            f"Outside is {t:.1f} °C and dry (dew point {dew:.1f} °C). Opening windows "
            "could cool the house for free.",
            "info", "medium", {"temp_c": t, "dew_point_c": dew},
        )


class WindowCondensation(Rule):
    type, persist, clear_after = "window_condensation", 30, 30

    def evaluate(self, s):
        if s.row is None:
            return None
        home, t = s.config.home, s.row["temp_c"]
        risk = derive.condensation_risk(
            home.indoor_setpoint_c, home.indoor_rh_assumed, t, home.window_u_value
        )
        glass = derive.glass_temperature(home.indoor_setpoint_c, t, home.window_u_value)
        return Result(
            risk == "high", "Window condensation likely",
            f"Estimate from your indoor settings: with {t:.1f} °C outside, the inner "
            f"glass is about {glass:.1f} °C, below the indoor dew point.",
            "info", "low", {"glass_c": glass},
        )


RULES = (
    Freeze, FrostRiskTonight, FrostLikely, FogDew, RainOnset, StormDarkening,
    FrontPassage, Heat, SolarHeating, SensorCovered, TagMoved, BatteryTrend,
    SensorStuck, ForeignTags, OpenWindows, WindowCondensation,
)  # fmt: skip


def pearson(xs: list[float], ys: list[float]) -> float | None:
    mx, my = fmean(xs), fmean(ys)
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sx * sy)


@dataclass
class RuleState:
    rule: Rule
    true_since: int | None = None
    clear_since: int | None = None
    unknown_since: int | None = None
    last_one_shot: int | None = None


class Engine:
    """Runs the rules for one data source.

    Call tick() regularly, always from the same thread.
    """

    def __init__(self, config: Config, source: str, alerts: AlertManager):
        self.config = config
        self.source = source
        self.alerts = alerts
        self.state = State(config, Calibration())
        self.rules = [RuleState(cls()) for cls in RULES]
        self.position: int | None = None  # last evaluated minute
        self.last_good: dict | None = None
        self._spike: tuple[int, int] | None = None  # first and last bright reading
        self._battery_day = None

    # Reading data

    def tick(
        self, conn: sqlite3.Connection, light: Calibration, now_ms: int
    ) -> None:
        """Evaluate every minute completed since the last call."""
        self.state.light = light
        latest = conn.execute(
            """SELECT r.ts_ms, r.temp_c FROM readings r
               JOIN raw_packets p ON p.id = r.raw_id
               WHERE r.quality = 'good' AND p.source = ?
               ORDER BY r.id DESC LIMIT 1""",
            (self.source,),
        ).fetchone()
        if latest is None:
            return
        self.last_good = dict(latest)
        end = latest["ts_ms"] // MINUTE * MINUTE  # this minute is still open
        if self.position is None or end < self.position - HOUR:
            self._start(conn, end)
        if end - MINUTE > self.position:
            self._run(conn, self.position + MINUTE, end - MINUTE)
            self.alerts.save_position(self.source, self.position)
        self._check_offline(conn, now_ms)

    def _start(self, conn, end: int) -> None:
        """Resume after a restart, or begin with a new stretch of data."""
        self.state.minutes.clear()
        self.rules = [RuleState(cls()) for cls in RULES]
        self._spike = None
        saved = self.alerts.load_position(conn, self.source)
        first = conn.execute(
            """SELECT MIN(r.ts_ms) FROM readings r
               JOIN raw_packets p ON p.id = r.raw_id
               WHERE r.quality = 'good' AND p.source = ? AND r.ts_ms >= ?""",
            (self.source, end - DAY),
        ).fetchone()[0]
        if saved is None or not end - DAY <= saved < end:
            if saved is not None:
                self._close_all(saved, "a new stretch of data began")
            saved = max(first, end - DAY) // MINUTE * MINUTE - MINUTE
        # Rebuild history and timers quietly; open events come from the database.
        warm = saved - WARM_UP_MINUTES * MINUTE
        self._load(conn, saved - HISTORY_MINUTES * MINUTE, warm)
        self.position = warm
        self.alerts.quiet = True
        try:
            self._run(conn, warm + MINUTE, saved)
        finally:
            self.alerts.quiet = False

    def _load(self, conn, start: int, end: int) -> None:
        for row in self._minute_rows(conn, start, end).values():
            self.state.minutes.append(row)

    def _minute_rows(self, conn, start: int, end: int) -> dict[int, dict]:
        rows = conn.execute(
            f"""SELECT r.ts_ms / {MINUTE} * {MINUTE} AS t, COUNT(*) AS n,
                  AVG(r.temp_c) AS temp_c, MIN(r.temp_c) AS temp_min,
                  MAX(r.temp_c) AS temp_max, AVG(r.rh_pct) AS rh_pct,
                  MIN(r.rh_pct) AS rh_min, MAX(r.rh_pct) AS rh_max,
                  AVG(r.dew_point_c) AS dew_point_c, AVG(r.light_raw) AS light_raw,
                  MAX(r.light_raw) AS light_max, AVG(r.rssi_dbm) AS rssi_dbm
                FROM readings r JOIN raw_packets p ON p.id = r.raw_id
                WHERE r.ts_ms >= ? AND r.ts_ms < ? AND r.quality = 'good'
                  AND p.source = ?
                GROUP BY t ORDER BY t""",
            (start, end + MINUTE, self.source),
        ).fetchall()
        station = self.config.station
        return {
            r["t"]: {**dict(r), "elev": sun.elevation(station, r["t"])} for r in rows
        }

    def _foreign(self, conn, start: int, end: int) -> dict[int, list[int]]:
        allowed = self.config.tag.allowed_tx_ids
        if not allowed:
            return {}
        marks = ",".join("?" * len(allowed))
        found: dict[int, list[int]] = {}
        for row in conn.execute(
            f"""SELECT DISTINCT ts_ms / {MINUTE} * {MINUTE} AS t, tx_id
                FROM raw_packets WHERE ts_ms >= ? AND ts_ms < ? AND crc_ok
                  AND source = ? AND tx_id NOT IN ({marks})""",
            (start, end + MINUTE, self.source, *allowed),
        ):
            found.setdefault(row["t"], []).append(row["tx_id"])
        return found

    # Evaluating

    def _run(self, conn, start: int, end: int) -> None:
        # In slices, so that a long backlog is not held in memory at once.
        while start <= end:
            stop = min(end, start + (MAX_GAP_MINUTES - 1) * MINUTE)
            rows = self._minute_rows(conn, start, stop)
            foreign = self._foreign(conn, start, stop)
            if not rows and stop < end:
                # A day without data: nothing known before it still holds.
                self._close_all(start, "there has been no data")
            for t in range(start, stop + MINUTE, MINUTE):
                self._minute(conn, t, rows.get(t), foreign.get(t, []))
            start = stop + MINUTE

    def _minute(self, conn, t: int, row: dict | None, foreign: list[int]) -> None:
        s = self.state
        self.position = s.now = t
        if row:
            s.minutes.append(row)
        s.foreign.extend((t, tx) for tx in foreign)
        while s.foreign and s.foreign[0][0] <= t - HOUR:
            s.foreign.popleft()
        self._update_battery(conn, t)
        s.active_types = self.alerts.active_types(self.source)
        self._night_light(conn, t, row)
        for state in self.rules:
            self._apply(state, state.rule.evaluate(s), t)
        if "solar_heating" in self.alerts.active_types(self.source):
            self.alerts.flag_solar_heating(self.source, t, t + MINUTE)

    def _apply(self, state: RuleState, result: Result | None, t: int) -> None:
        rule = state.rule
        active = self.alerts.get_active(self.source, rule.type)
        if result is None:
            state.true_since = state.clear_since = None
            state.unknown_since = state.unknown_since or t
            if active and t - state.unknown_since >= UNKNOWN_CLEAR_MINUTES * MINUTE:
                self.alerts.close(self.source, rule.type, t)
            return
        state.unknown_since = None
        if rule.one_shot:
            recent = state.last_one_shot
            if result.active and (
                recent is None or t - recent >= rule.refractory * MINUTE
            ):
                state.last_one_shot = t
                self.alerts.one_shot(self.source, rule.type, result, result.start_ms, t)
            return
        if not active:
            if not result.active:
                state.true_since = None
                return
            state.true_since = state.true_since or t
            if t - state.true_since >= rule.persist * MINUTE:
                state.clear_since = None
                self.alerts.open(self.source, rule.type, result, state.true_since, t)
            return
        state.true_since = None
        if result.active:
            self.alerts.escalate(self.source, rule.type, result, t)
        if result.clear_now:
            self.alerts.close(self.source, rule.type, t)
        elif result.is_clear():
            state.clear_since = state.clear_since or t
            if t - state.clear_since >= rule.clear_after * MINUTE:
                self.alerts.close(self.source, rule.type, t)
        else:
            state.clear_since = None

    def _close_all(self, t: int, reason: str) -> None:
        for kind in self.alerts.active_types(self.source) - {"tag_offline"}:
            self.alerts.close(self.source, kind, t, f" Ended because {reason}.")
        for state in self.rules:
            state.true_since = state.clear_since = None

    def _night_light(self, conn, t: int, row: dict | None) -> None:
        """Light at night, timed by individual readings."""
        s = self.state
        threshold = s.night_threshold() if row and s.elevation() < sun.NIGHT_BELOW else None
        if threshold is not None and row["light_max"] > threshold:
            for reading in conn.execute(
                """SELECT r.ts_ms, r.light_raw FROM readings r
                   JOIN raw_packets p ON p.id = r.raw_id
                   WHERE r.ts_ms >= ? AND r.ts_ms < ? AND r.light_raw > ?
                     AND r.quality = 'good' AND p.source = ? ORDER BY r.ts_ms""",
                (t, t + MINUTE, threshold, self.source),
            ):
                self._spike_reading(reading["ts_ms"], reading["light_raw"], threshold)
        last = self._spike[1] if self._spike else t
        if self._spike and t + MINUTE - last >= SPIKE_MERGE_MS:
            self._end_spike()

    def _spike_reading(self, ts: int, light: float, threshold: float) -> None:
        if self._spike and ts - self._spike[1] >= SPIKE_MERGE_MS:
            self._end_spike()
        if self._spike:
            self._spike = (self._spike[0], ts)
            return
        self._spike = (ts, ts)
        result = Result(
            True, "Light detected at night",
            f"Light reached {light:.0f}, above the night level of {threshold:.0f}.",
            "info", "high", {"light_raw": light, "threshold": threshold},
        )
        if not self.alerts.get_active(self.source, "night_light"):
            self.alerts.open(self.source, "night_light", result, ts, ts)

    def _end_spike(self) -> None:
        first, last = self._spike
        self._spike = None
        seconds = round((last - first) / 1000) + 1
        if last - first >= SUSTAINED_LIGHT_MS:
            text = f"Sustained for about {seconds / 60:.0f} min (porch light or lamp?)."
        else:
            text = f"A brief flash of about {seconds} s (headlights or flashlight?)."
        self.alerts.close(self.source, "night_light", last, f" {text}")

    def _update_battery(self, conn, t: int) -> None:
        """Once per local day: weekly trend of signal and packet count."""
        day = sun.local_date(self.config.station, t)
        if day == self._battery_day:
            return
        self._battery_day = day
        midnight = aggregates.local_midnight(t, self.config.station.timezone)
        medians, counts = [], []
        for back in range(7, 0, -1):
            rows = conn.execute(
                """SELECT r.rssi_dbm AS v, COUNT(*) AS n
                   FROM readings r JOIN raw_packets p ON p.id = r.raw_id
                   WHERE r.ts_ms >= ? AND r.ts_ms < ? AND r.quality = 'good'
                     AND p.source = ? GROUP BY v""",
                (midnight - back * DAY, midnight - (back - 1) * DAY, self.source),
            ).fetchall()
            if rows:
                weights = {r["v"]: r["n"] for r in rows}
                medians.append(((7 - back) * HOUR, calibration.weighted_median(weights)))
                counts.append(((7 - back) * HOUR, float(sum(weights.values()))))
        # Days stand in for hours, so the slope is per day.
        self.state.battery = (
            (derive.slope_per_hour(medians), derive.slope_per_hour(counts))
            if len(medians) >= 5
            else None
        )

    def _check_offline(self, conn, now_ms: int) -> None:
        """Silence from the tag, judged by the clock the data runs on."""
        last = self.last_good["ts_ms"]
        now = now_ms
        if self.source != "hardware":
            # Other packets still show how far simulated time has come.
            now = conn.execute(
                "SELECT ts_ms FROM raw_packets WHERE source = ? ORDER BY id DESC LIMIT 1",
                (self.source,),
            ).fetchone()[0]
        silence = (now - last) / 1000
        recent = self.state.window(10)
        spacing = 60 * len(recent) / sum(r["n"] for r in recent) if recent else 0
        warning = max(self.config.tag.offline_warning_s, 5 * spacing)
        critical = max(self.config.tag.offline_critical_s, warning)
        if silence < warning:
            self.alerts.close(self.source, "tag_offline", last)
            return
        temp = self.last_good["temp_c"]
        hint = ""
        if temp < -10:
            hint = " Cold may be weakening the battery."
        elif self.state.battery and self.state.battery[0] <= -0.5:
            hint = " The signal has been falling for days: the battery may be low."
        result = Result(
            True, "Tag offline",
            f"No good packet for {silence / 60:.1f} min. The last reading was "
            f"{temp:.1f} °C.{hint}",
            "critical" if silence >= critical else "warning", "high",
            {"silence_s": silence, "last_temp_c": temp},
        )
        if self.alerts.get_active(self.source, "tag_offline"):
            self.alerts.escalate(self.source, "tag_offline", result, now)
        else:
            self.alerts.open(self.source, "tag_offline", result, last, now)
        if silence >= critical:
            self._close_all(last, "the tag went offline")
