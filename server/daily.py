"""Summaries per local calendar day (spec sections 13, 15.6, 16.5 and 16.6)."""

import sqlite3
from datetime import date, datetime, timedelta
from statistics import fmean
from zoneinfo import ZoneInfo

from server import aggregates, alerts, derive, health, sun
from server.calibration import Calibration
from server.config import Config

MINUTE = 60_000
LIGHT_ON, LIGHT_OFF = 0.05, 0.02
HYSTERESIS_MINUTES = 5
BRIGHT_SUN = 0.7
MIN_SKY_MINUTES = 60


def day_bounds(day: date, timezone: str) -> tuple[int, int]:
    """Start and end of a local day in epoch ms (23 to 25 hours apart)."""
    tz = ZoneInfo(timezone)
    return tuple(
        round(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp() * 1000)
        for d in (day, day + timedelta(days=1))
    )


def light_changes(
    config: Config, light: Calibration, minutes: list
) -> tuple[int | None, int | None]:
    """First switch to light and last switch to dark, with hysteresis.

    Minutes in full night are skipped, so lamps do not count. A switch only counts
    if the opposite state was seen before it.
    """
    state = None
    run: list[tuple[str, int]] = []
    sunrise = sunset = None
    for row in minutes:
        if sun.elevation(config.station, row["t"]) < sun.NIGHT_BELOW:
            run = []
            if state is None:
                state = "off"  # data from before dawn
            continue
        norm = light.light_norm(row["light_raw"])
        level = "on" if norm > LIGHT_ON else "off" if norm < LIGHT_OFF else "between"
        if run and (run[-1][0] != level or row["t"] - run[-1][1] > MINUTE):
            run = []
        run.append((level, row["t"]))
        if level == "between" or len(run) < HYSTERESIS_MINUTES or level == state:
            continue
        if state is not None:
            if level == "on":
                sunrise = run[0][1] if sunrise is None else sunrise
            else:
                sunset = run[0][1]
        state = level
    return sunrise, sunset


def sky_summary(values: list[float]) -> str | None:
    if len(values) < MIN_SKY_MINUTES:
        return None
    mean = fmean(values)
    if mean >= 0.75:
        return "mostly clear"
    if mean >= 0.55:
        return "partly cloudy"
    return "mostly cloudy" if mean >= 0.35 else "overcast"


def summarise(
    conn: sqlite3.Connection,
    config: Config,
    source: str,
    day: date,
    light: Calibration,
    now: int,
) -> dict | None:
    """Summary of one day up to `now`, or None if it has no good readings."""
    start, end = day_bounds(day, config.station.timezone)
    minutes = aggregates.minute_averages(conn, source, start, end - 1)
    if not minutes:
        return None
    low = min(minutes, key=lambda r: r["temp_min"])
    high = max(minutes, key=lambda r: r["temp_max"])
    temps = [r["temp_c"] for r in minutes]
    dews = [r["dew_point_c"] for r in minutes if r["dew_point_c"] is not None]
    feels = [derive.instant(r["temp_c"], r["rh_pct"])["feels_like_c"] for r in minutes]
    base = config.thresholds.degree_day_base_c
    sunrise, sunset = sun.sun_times(config.station, day)

    measured = bright = rise_seen = set_seen = sky = None
    if light.calibrated:
        clearness = [
            kc
            for r in minutes
            if (
                kc := light.clearness(
                    r["light_raw"], sun.elevation(config.station, r["t"])
                )
            )
            is not None
        ]
        lit = sum(
            light.light_norm(r["light_raw"]) > LIGHT_ON
            and sun.elevation(config.station, r["t"]) >= sun.NIGHT_BELOW
            for r in minutes
        )
        measured = lit / 60
        bright = sum(kc >= BRIGHT_SUN for kc in clearness) / 60
        rise_seen, set_seen = light_changes(config, light, minutes)
        sky = sky_summary(clearness)

    elapsed = max(MINUTE, min(end, now) - start)
    return {
        "source": source,
        "local_date": day.isoformat(),
        "temp_min_c": low["temp_min"],
        "temp_min_time_ms": low["t"],
        "temp_max_c": high["temp_max"],
        "temp_max_time_ms": high["t"],
        "temp_avg_c": fmean(temps),
        "rh_min": min(r["rh_pct"] for r in minutes),
        "rh_max": max(r["rh_pct"] for r in minutes),
        "rh_avg": fmean(r["rh_pct"] for r in minutes),
        "dew_point_avg_c": fmean(dews) if dews else None,
        "heat_index_max_c": max(feels),
        "hdd": derive.heating_degree_days(temps, base),
        "cdd": derive.cooling_degree_days(temps, base),
        "gdd": derive.growing_degree_days(
            low["temp_min"], high["temp_max"], config.thresholds.gdd_base_c
        ),
        "daylight_hours_measured": measured,
        "daylight_hours_astronomical": (sunset - sunrise) / 3_600_000
        if sunrise is not None and sunset is not None
        else None,
        "bright_sun_hours": bright,
        "sunrise_detected_ms": rise_seen,
        "sunset_detected_ms": set_seen,
        "sunrise_astro_ms": sunrise,
        "sunset_astro_ms": sunset,
        "sky_summary": sky,
        "packet_success_pct": health.packet_success(conn, config, source, start, end - 1),
        "data_coverage_pct": min(100.0, 100 * len(minutes) * MINUTE / elapsed),
        "notes": _notes(conn, config, source, start, end),
    }


def _notes(conn, config, source, start, end) -> str:
    """Titles of the alerts and fronts that began on the day."""
    tz = ZoneInfo(config.station.timezone)
    notes = []
    for event in reversed(alerts.query_events(conn, source, start, end - 1)):
        relevant = alerts.is_alert(event) or event["type"] == "front_passage"
        if relevant and event["start_ms"] >= start:
            at = datetime.fromtimestamp(event["start_ms"] / 1000, tz)
            notes.append(f"{event['title']} {at:%H:%M}")
    return "; ".join(notes)


def store(conn: sqlite3.Connection, summary: dict) -> None:
    marks = ", ".join("?" * len(summary))
    with conn:
        conn.execute(
            f"INSERT OR REPLACE INTO daily_summary ({', '.join(summary)}) VALUES ({marks})",
            tuple(summary.values()),
        )


def missing_days(
    conn: sqlite3.Connection, config: Config, source: str, today: date, limit: int
) -> list[date]:
    """Finished days with readings but no stored summary, oldest first."""
    first = conn.execute(
        """SELECT MIN(r.ts_ms) FROM readings r JOIN raw_packets p ON p.id = r.raw_id
           WHERE r.quality = 'good' AND p.source = ?""",
        (source,),
    ).fetchone()[0]
    if first is None:
        return []
    stored = {
        row[0]
        for row in conn.execute(
            "SELECT local_date FROM daily_summary WHERE source = ?", (source,)
        )
    }
    days = []
    day = sun.local_date(config.station, first)
    while day < today and len(days) < limit:
        if day.isoformat() not in stored and _has_data(conn, config, source, day):
            days.append(day)
        day += timedelta(days=1)
    return days


def _has_data(conn, config, source, day) -> bool:
    start, end = day_bounds(day, config.station.timezone)
    return (
        conn.execute(
            """SELECT 1 FROM readings r JOIN raw_packets p ON p.id = r.raw_id
               WHERE r.ts_ms >= ? AND r.ts_ms < ? AND r.quality = 'good'
                 AND p.source = ? LIMIT 1""",
            (start, end, source),
        ).fetchone()
        is not None
    )


def forget(conn: sqlite3.Connection, source: str, days: set[date] | None = None) -> None:
    """Drop stored summaries so that they are computed again."""
    with conn:
        if days is None:
            conn.execute("DELETE FROM daily_summary WHERE source = ?", (source,))
        for day in days or ():
            conn.execute(
                "DELETE FROM daily_summary WHERE source = ? AND local_date = ?",
                (source, day.isoformat()),
            )


def load(conn: sqlite3.Connection, source: str, first: date, last: date) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM daily_summary
               WHERE source = ? AND local_date BETWEEN ? AND ? ORDER BY local_date""",
            (source, first.isoformat(), last.isoformat()),
        )
    ]


def totals(rows: list[dict], today: date) -> dict:
    """Degree-day totals for the month and the season up to today.

    Heating seasons start in July; cooling and growing seasons in January.
    """
    month = today.replace(day=1)
    heating = date(today.year if today.month >= 7 else today.year - 1, 7, 1)
    growing = date(today.year, 1, 1)

    def total(key: str, since: date) -> float:
        return sum(r[key] for r in rows if r["local_date"] >= since.isoformat())

    return {
        "month": {"since": month.isoformat()}
        | {key: total(key, month) for key in ("hdd", "cdd", "gdd")},
        "season": {
            "hdd": total("hdd", heating),
            "hdd_since": heating.isoformat(),
            "cdd": total("cdd", growing),
            "gdd": total("gdd", growing),
            "cdd_gdd_since": growing.isoformat(),
        },
    }
