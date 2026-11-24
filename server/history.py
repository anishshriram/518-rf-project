"""Time-series queries behind GET /api/history (spec section 19.3)."""

import sqlite3
from statistics import median

# Columns of `readings` that can be charted.
METRICS = (
    "temp_c",
    "rh_pct",
    "dew_point_c",
    "heat_index_c",
    "light_norm",
    "light_raw",
    "rssi_dbm",
)
MINUTE, HOUR, DAY = 60_000, 3_600_000, 86_400_000
RESOLUTIONS = {
    "raw": 0,
    "5s": 5_000,
    "1m": MINUTE,
    "5m": 5 * MINUTE,
    "15m": 15 * MINUTE,
    "1h": HOUR,
    "1d": DAY,
}
MAX_RAW_SPAN = 6 * HOUR


def auto_resolution(span_ms: int) -> str:
    """Finest resolution that keeps a 1 Hz series to about 2000 points."""
    for limit, name in (
        (30 * MINUTE, "raw"),
        (2 * HOUR, "5s"),
        (2 * DAY, "1m"),
        (14 * DAY, "15m"),
        (90 * DAY, "1h"),
    ):
        if span_ms <= limit:
            return name
    return "1d"


def query(
    conn: sqlite3.Connection,
    *,
    source: str,
    start: int,
    end: int,
    metrics: list[str],
    resolution: str,
    beacon_s: float,
) -> dict:
    """Return series of one source only; `metrics` must come from METRICS."""
    if resolution == "auto":
        resolution = auto_resolution(end - start)
    step = RESOLUTIONS[resolution]
    if step:
        rows = _bucketed(conn, source, start, end, metrics, step)
    else:
        rows = _raw(conn, source, start, end, metrics)

    times = [row["t"] for row in rows]
    series = {}
    for metric in metrics:
        series[metric] = {"t": times, "avg": [row[metric] for row in rows]}
        if step:
            series[metric]["min"] = [row[f"{metric}_min"] for row in rows]
            series[metric]["max"] = [row[f"{metric}_max"] for row in rows]
    threshold = gap_threshold(times, step, beacon_s)
    return {
        "resolution": resolution,
        "resolution_ms": step,
        "source": source,
        "series": series,
        "n": [row["n"] for row in rows],
        "suspect": [bool(row["suspect"]) for row in rows],
        "last_id": max((row["last_id"] for row in rows), default=0),
        "gap_threshold_ms": threshold,
        "gaps": [
            [a, b] for a, b in zip(times, times[1:]) if b - a > threshold
        ],
        "night_intervals": [],
    }


def gap_threshold(times: list[int], step: int, beacon_s: float) -> float:
    """Longest step between points that still counts as continuous."""
    beacon = 3 * beacon_s * 1000
    spacing = median(b - a for a, b in zip(times, times[1:])) if times[1:] else 0
    # Points further apart than the buckets mean the tag beacons more slowly.
    if spacing > step:
        beacon = max(beacon, 3 * spacing)
    return max(step, beacon)


def _raw(conn, source, start, end, metrics):
    columns = ", ".join(f"r.{m}" for m in metrics)
    return conn.execute(
        f"""SELECT r.ts_ms AS t, 1 AS n, r.quality = 'suspect' AS suspect,
                   r.id AS last_id, {columns}
            FROM readings r JOIN raw_packets p ON p.id = r.raw_id
            WHERE r.ts_ms BETWEEN ? AND ? AND r.quality != 'rejected'
              AND p.source = ?
            ORDER BY r.ts_ms""",
        (start, end, source),
    ).fetchall()


def _bucketed(conn, source, start, end, metrics, step):
    columns = ", ".join(
        f"AVG(r.{m}) AS {m}, MIN(r.{m}) AS {m}_min, MAX(r.{m}) AS {m}_max"
        for m in metrics
    )
    return conn.execute(
        f"""SELECT r.ts_ms / {step} * {step} AS t, COUNT(*) AS n, 0 AS suspect,
                   MAX(r.id) AS last_id, {columns}
            FROM readings r JOIN raw_packets p ON p.id = r.raw_id
            WHERE r.ts_ms BETWEEN ? AND ? AND r.quality = 'good' AND p.source = ?
            GROUP BY t ORDER BY t""",
        # Whole buckets, so the first one is not computed from part of its data.
        (start // step * step, end, source),
    ).fetchall()
