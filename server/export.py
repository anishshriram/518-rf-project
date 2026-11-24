"""CSV export of readings (spec section 19.9)."""

import csv
import io
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

from server import history
from server.calibration import Calibration
from server.config import Config

RAW_COLUMNS = (
    "temp_c",
    "rh_pct",
    "dew_point_c",
    "heat_index_c",
    "abs_humidity_gm3",
    "light_raw",
    "sun_elev_deg",
    "rssi_dbm",
)
CHUNK_ROWS = 1000


def _to_csv(rows: Iterator[list]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for number, row in enumerate(rows, 1):
        writer.writerow(row)
        if number % CHUNK_ROWS == 0:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()


def _time(ms: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).isoformat(timespec="seconds")


def raw_rows(
    conn: sqlite3.Connection, config: Config, source: str, start: int, end: int
) -> Iterator[list]:
    """Every reading, rejected ones included and marked."""
    tz = ZoneInfo(config.station.timezone)
    columns = ", ".join(f"r.{c}" for c in RAW_COLUMNS)
    yield ["time", "ts_ms", "source", "tx_id", "quality", "reject_reason", *RAW_COLUMNS]
    for row in conn.execute(
        f"""SELECT r.ts_ms, p.source, r.tx_id, r.quality, r.reject_reason, {columns}
            FROM readings r JOIN raw_packets p ON p.id = r.raw_id
            WHERE r.ts_ms BETWEEN ? AND ? AND p.source = ? ORDER BY r.ts_ms""",
        (start, end, source),
    ):
        yield [_time(row["ts_ms"], tz), *row]


def bucket_rows(
    conn: sqlite3.Connection,
    config: Config,
    calibration: Calibration,
    source: str,
    start: int,
    end: int,
    resolution: str,
) -> Iterator[list]:
    """Averages, with minimum and maximum where they exist."""
    tz = ZoneInfo(config.station.timezone)
    data = history.query(
        conn,
        source=source,
        start=start,
        end=end,
        metrics=list(history.METRICS),
        resolution=resolution,
        config=config,
        calibration=calibration,
    )
    columns = [
        (metric, kind, values[kind])
        for metric, values in data["series"].items()
        for kind in ("avg", "min", "max")
        if kind in values
    ]
    yield ["time", "ts_ms", "source", "n", *(f"{m}_{kind}" for m, kind, _ in columns)]
    times = data["series"]["temp_c"]["t"]
    for i, t in enumerate(times):
        cells = [values[i] for _, _, values in columns]
        rounded = [round(v, 4) if isinstance(v, float) else v for v in cells]
        yield [_time(t, tz), t, source, data["n"][i], *rounded]


def stream(conn, config, calibration, source, start, end, resolution) -> Iterator[str]:
    """CSV text in chunks; closes the connection at the end."""
    try:
        if resolution == "raw":
            rows = raw_rows(conn, config, source, start, end)
        else:
            rows = bucket_rows(
                conn, config, calibration, source, start, end, resolution
            )
        yield from _to_csv(rows)
    finally:
        conn.close()
