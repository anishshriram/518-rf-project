"""Minute averages, trends and today's figures (spec sections 13 and 15).

Computed from `readings` on request. The rollup tables follow with the daily
summaries (M6).
"""

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from server import derive
from server.calibration import Calibration, sky_condition
from server.config import Config

MINUTE = 60_000
TREND_METRICS = {
    "temp_c_per_h": "temp_c",
    "rh_per_h": "rh_pct",
    "dew_point_c_per_h": "dew_point_c",
}
TREND_MIN_COVERAGE = 0.5  # fraction of the window's minutes that need data


def minute_averages(
    conn: sqlite3.Connection, source: str, start: int, end: int
) -> list[sqlite3.Row]:
    """Good readings of one source averaged per minute, oldest first."""
    return conn.execute(
        f"""SELECT r.ts_ms / {MINUTE} * {MINUTE} AS t,
                   AVG(r.temp_c) AS temp_c, MIN(r.temp_c) AS temp_min,
                   MAX(r.temp_c) AS temp_max,
                   AVG(r.light_raw) AS light_raw,
                   AVG(r.rh_pct) AS rh_pct, AVG(r.dew_point_c) AS dew_point_c
            FROM readings r JOIN raw_packets p ON p.id = r.raw_id
            WHERE r.ts_ms BETWEEN ? AND ? AND r.quality = 'good' AND p.source = ?
            GROUP BY t ORDER BY t""",
        (start, end, source),
    ).fetchall()


def trends(minutes: list[sqlite3.Row], end: int, window_min: int) -> dict:
    """Slopes per hour over the last window_min minutes, or None."""
    recent = [row for row in minutes if row["t"] > end - window_min * MINUTE]
    result = {}
    for name, column in TREND_METRICS.items():
        points = [(r["t"], r[column]) for r in recent if r[column] is not None]
        enough = len(points) >= window_min * TREND_MIN_COVERAGE
        result[name] = derive.slope_per_hour(points) if enough else None
    return result


def local_midnight(ts_ms: int, timezone: str) -> int:
    """Start of the local calendar day containing ts_ms."""
    local = datetime.fromtimestamp(ts_ms / 1000, ZoneInfo(timezone))
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0, fold=0)
    return round(midnight.timestamp() * 1000)


def conditions(
    conn: sqlite3.Connection,
    config: Config,
    source: str,
    end: int,
    calibration: Calibration,
) -> dict:
    """Sky, trends and today's figures as of `end`."""
    midnight = local_midnight(end, config.station.timezone)
    start = min(midnight, end - 60 * MINUTE)
    minutes = minute_averages(conn, source, start, end)
    today = [row for row in minutes if row["t"] >= midnight]
    temps = [row["temp_c"] for row in today]
    low = min((row["temp_min"] for row in today), default=None)
    high = max((row["temp_max"] for row in today), default=None)
    base = config.thresholds.degree_day_base_c
    sky, clearness = sky_condition(config, calibration, minutes, end)
    return {
        "sky": sky,
        "clearness": clearness,
        "trends": trends(minutes, end, 30),
        "trends_60m": trends(minutes, end, 60),
        "today": {
            "temp_min_c": low,
            "temp_max_c": high,
            "hdd_so_far": derive.heating_degree_days(temps, base),
            "cdd_so_far": derive.cooling_degree_days(temps, base),
            "gdd_so_far": None
            if low is None
            else derive.growing_degree_days(low, high, config.thresholds.gdd_base_c),
        },
    }
