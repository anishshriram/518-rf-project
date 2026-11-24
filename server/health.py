"""Link, packet and tag health (spec section 19.6)."""

import sqlite3
from statistics import median

from server.config import Config

HOUR = 3_600_000
SAMPLE = 300


def beacon_interval(
    conn: sqlite3.Connection, source: str, before: int | None = None
) -> float | None:
    """Median seconds between recent packets of the tag."""
    times = sorted(
        row[0]
        for row in conn.execute(
            """SELECT r.ts_ms FROM readings r JOIN raw_packets p ON p.id = r.raw_id
               WHERE p.source = ? AND r.ts_ms <= ? ORDER BY r.ts_ms DESC LIMIT ?""",
            (source, 2**53 if before is None else before, SAMPLE),
        )
    )
    gaps = [b - a for a, b in zip(times, times[1:]) if b > a]
    return median(gaps) / 1000 if len(gaps) >= 5 else None


def packet_success(
    conn: sqlite3.Connection,
    config: Config,
    source: str,
    start: int,
    end: int,
    until: int | None = None,
) -> float | None:
    """Share of expected beacons received in [start, end], in percent.

    Counted from the first packet to the last, or to `until` if given, which also
    counts the silence since.
    """
    first, last, count = conn.execute(
        """SELECT MIN(r.ts_ms), MAX(r.ts_ms), COUNT(*)
           FROM readings r JOIN raw_packets p ON p.id = r.raw_id
           WHERE r.ts_ms BETWEEN ? AND ? AND p.source = ?""",
        (start, end, source),
    ).fetchone()
    if not count:
        return None
    interval = beacon_interval(conn, source, end)
    if interval is None:
        interval = config.tag.expected_beacon_interval_s
    if until is not None:
        last = max(last, until)
        earlier = conn.execute(
            """SELECT 1 FROM readings r JOIN raw_packets p ON p.id = r.raw_id
               WHERE r.ts_ms < ? AND p.source = ? LIMIT 1""",
            (start, source),
        ).fetchone()
        first = start if earlier else first
    expected = (last - first) / 1000 / interval + 1
    return min(100.0, 100 * count / expected)


def data_clock(conn: sqlite3.Connection, source: str, now: int) -> int:
    """Current time of a source: the clock, or the newest packet of any kind."""
    if source == "hardware":
        return now
    return conn.execute(
        "SELECT ts_ms FROM raw_packets WHERE source = ? ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()[0]


def tag_status(
    conn: sqlite3.Connection, config: Config, latest: dict | None, now: int
) -> dict:
    """Seconds since the last good packet, limits in use, and the status."""
    tag = config.tag
    limits = {
        "offline_warning_s": tag.offline_warning_s,
        "offline_critical_s": tag.offline_critical_s,
    }
    if latest is None:
        return {"status": "never_seen", "age_s": None, **limits}
    source = latest["source"]
    # A tag that beacons slowly is not offline between two packets.
    warning = max(tag.offline_warning_s, 5 * (beacon_interval(conn, source) or 0))
    critical = max(tag.offline_critical_s, warning)
    age = (data_clock(conn, source, now) - latest["ts_ms"]) / 1000
    if source != "hardware":
        # Simulated time may stand still because the simulator stopped.
        age = max(age, (now - latest["received_ms"]) / 1000)
    age = max(0.0, age)
    status = "online"
    if age >= critical:
        status = "offline_critical"
    elif age >= warning:
        status = "offline_warning"
    return {
        "status": status,
        "age_s": age,
        "offline_warning_s": warning,
        "offline_critical_s": critical,
    }


def tag_health(
    conn: sqlite3.Connection, config: Config, latest: dict | None, now: int
) -> dict:
    """The `tag` part of GET /api/health."""
    ids = config.tag.allowed_tx_ids
    status = tag_status(conn, config, latest, now)
    source = latest["source"] if latest else "hardware"
    clock = data_clock(conn, source, now) if latest else now
    start = clock - HOUR
    rssi = [
        row[0]
        for row in conn.execute(
            """SELECT r.rssi_dbm FROM readings r JOIN raw_packets p ON p.id = r.raw_id
               WHERE r.ts_ms BETWEEN ? AND ? AND p.source = ?""",
            (start, clock, source),
        )
    ]
    foreign = []
    if ids:
        marks = ",".join("?" * len(ids))
        foreign = [
            row[0]
            for row in conn.execute(
                f"""SELECT DISTINCT tx_id FROM raw_packets
                    WHERE ts_ms BETWEEN ? AND ? AND crc_ok AND source = ?
                      AND tx_id NOT IN ({marks}) ORDER BY tx_id LIMIT 50""",
                (start, clock, source, *ids),
            )
        ]
    bad, drops = conn.execute(
        """SELECT SUM(NOT crc_ok), MAX(drop_count) FROM raw_packets
           WHERE ts_ms BETWEEN ? AND ? AND source = ?""",
        (start, clock, source),
    ).fetchone()
    return {
        "tx_id": latest["tx_id"] if latest else (ids[0] if ids else None),
        "status": status["status"],
        "age_s": status["age_s"],
        "last_seen_ms": latest["ts_ms"] if latest else None,
        "rssi_dbm_now": latest["rssi_dbm"] if latest else None,
        "rssi_dbm_1h_median": median(rssi) if rssi else None,
        "beacon_interval_s_measured": beacon_interval(conn, source),
        "packet_success_pct_1h": packet_success(
            conn, config, source, start, clock, until=clock
        ),
        "bad_crc_per_h": bad or 0,
        "drop_max_1h": drops,
        "battery_installed": config.tag.battery_installed or None,
        "foreign_tags_1h": foreign,
    }


def receiver_health(conn: sqlite3.Connection) -> dict:
    raw = conn.execute(
        "SELECT rx_id FROM raw_packets WHERE crc_ok ORDER BY id DESC LIMIT 1"
    ).fetchone()
    summary = conn.execute(
        "SELECT packets, ok_pct, received_ms FROM receiver_stats ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "rx_id": raw["rx_id"] if raw else None,
        "last_summary": dict(summary) if summary else None,
    }


def collector_health(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT received_ms, source FROM raw_packets ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "last_ingest_ms": row["received_ms"] if row else None,
        "source": row["source"] if row else None,
    }
