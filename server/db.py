"""SQLite connection, schema creation and queries."""

import sqlite3
from pathlib import Path

from server import derive

SCHEMA_VERSION = 2
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(path: str) -> sqlite3.Connection:
    """Open the database and make sure the schema exists.

    The connection may be shared between threads; callers must serialise access.
    """
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate(conn)
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()
    elif row["version"] != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(
            f"database schema version {row['version']} is not {SCHEMA_VERSION}"
        )
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older database up to the current schema."""
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    if "schema_version" not in tables:
        return
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row and row["version"] == 1:
        # Version 1 never filled daily_summary; version 2 adds its source.
        with conn:
            conn.execute("DROP TABLE IF EXISTS daily_summary")
            conn.execute("UPDATE schema_version SET version = 2")


def _insert(conn: sqlite3.Connection, table: str, values: dict) -> int:
    # Table and column names come from this codebase, never from input.
    columns = ", ".join(values)
    marks = ", ".join("?" * len(values))
    cursor = conn.execute(
        f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values())
    )
    return cursor.lastrowid


def insert_raw(conn: sqlite3.Connection, values: dict) -> int:
    return _insert(conn, "raw_packets", values)


def insert_reading(conn: sqlite3.Connection, values: dict) -> int:
    return _insert(conn, "readings", values)


def insert_receiver_stats(conn: sqlite3.Connection, values: dict) -> int:
    return _insert(conn, "receiver_stats", values)


def raw_exists(conn, ts_ms: int, raw_line: str, source: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM raw_packets WHERE ts_ms = ? AND raw_line = ? AND source = ?",
        (ts_ms, raw_line, source),
    ).fetchone()
    return row is not None


def reading_exists(conn, tx_id: int, ts_ms: int, source: str) -> bool:
    row = conn.execute(
        """SELECT 1 FROM readings r JOIN raw_packets p ON p.id = r.raw_id
           WHERE r.ts_ms = ? AND r.tx_id = ? AND p.source = ?
             AND r.quality != 'rejected'""",
        (ts_ms, tx_id, source),
    ).fetchone()
    return row is not None


def previous_good(conn, tx_id: int, ts_ms: int, source: str, window_ms: int):
    """Latest good reading before ts_ms, no older than window_ms."""
    return conn.execute(
        """SELECT r.ts_ms, r.temp_c, r.rh_pct
           FROM readings r JOIN raw_packets p ON p.id = r.raw_id
           WHERE r.ts_ms < ? AND r.ts_ms >= ? AND r.tx_id = ?
             AND r.quality = 'good' AND p.source = ?
           ORDER BY r.ts_ms DESC LIMIT 1""",
        (ts_ms, ts_ms - window_ms, tx_id, source),
    ).fetchone()


_READING_COLUMNS = """r.id, r.ts_ms, r.tx_id, r.temp_c, r.rh_pct, r.dew_point_c,
    r.heat_index_c, r.abs_humidity_gm3, r.light_raw, r.light_norm, r.rssi_dbm, r.quality, p.source, p.received_ms"""


def latest_reading(conn: sqlite3.Connection) -> dict | None:
    """Most recently stored reading that was not rejected."""
    row = conn.execute(
        f"""SELECT {_READING_COLUMNS}
            FROM readings r JOIN raw_packets p ON p.id = r.raw_id
            WHERE r.quality != 'rejected' ORDER BY r.id DESC LIMIT 1"""
    ).fetchone()
    return _with_derived(row) if row else None


def _with_derived(row: sqlite3.Row) -> dict:
    """Add the derived values that are not stored."""
    return {**dict(row), **derive.instant(row["temp_c"], row["rh_pct"])}


def readings_after(conn: sqlite3.Connection, last_id: int, limit: int) -> list[dict]:
    """The newest `limit` readings with a higher id, oldest first."""
    rows = conn.execute(
        f"""SELECT {_READING_COLUMNS}
            FROM readings r JOIN raw_packets p ON p.id = r.raw_id
            WHERE r.id > ? AND r.quality != 'rejected'
            ORDER BY r.id DESC LIMIT ?""",
        (last_id, limit),
    ).fetchall()
    return [_with_derived(row) for row in reversed(rows)]


def bad_crc_since(conn: sqlite3.Connection, ts_ms: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM raw_packets WHERE ts_ms >= ? AND NOT crc_ok", (ts_ms,)
    ).fetchone()[0]


def open_reader(path: str) -> sqlite3.Connection:
    """A separate connection for queries, so they never hold up ingest."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def size_mb(path: str) -> float:
    """Size of the database on disk, including its write-ahead log."""
    files = [Path(path), Path(f"{path}-wal")]
    return sum(f.stat().st_size for f in files if f.exists()) / 1e6
