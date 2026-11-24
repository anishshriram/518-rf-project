"""Recompute readings from raw_packets: python -m server.rebuild

Raw lines are parsed again, so improved parsing, changed units or a changed tag
list apply to old data too. Everything happens in one transaction.
"""

import sqlite3
import sys

from collector.parser import Packet, parse_line
from server import db, settings
from server.config import Config, ConfigError, load_config
from server.ingest import PacketIn, store_reading


def rebuild(conn: sqlite3.Connection, config: Config) -> int:
    """Replace all readings that still have a raw packet; return the new count."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        # How long a packet waited before delivery is not stored, so keep the
        # clock verdict reached on arrival.
        skewed = {
            row["raw_id"]
            for row in conn.execute(
                "SELECT raw_id FROM readings WHERE reject_reason LIKE '%clock_skew%'"
            )
        }
        conn.execute(
            "DELETE FROM readings WHERE raw_id IN (SELECT id FROM raw_packets)"
        )
        conn.execute("DELETE FROM daily_summary")  # recomputed by the server
        count = 0
        # Arrival order, as at ingest. Fetched up front: rows change on the way.
        for row in conn.execute("SELECT * FROM raw_packets ORDER BY id").fetchall():
            packet = _reparse(conn, row)
            _, reading = store_reading(
                conn,
                config,
                packet,
                raw_id=row["id"],
                source=row["source"],
                received_ms=row["received_ms"],
                clock_skew=row["id"] in skewed,
            )
            count += reading is not None
    return count


def _reparse(conn: sqlite3.Connection, row: sqlite3.Row) -> PacketIn:
    parsed = parse_line(row["raw_line"] or "")
    if isinstance(parsed, Packet):
        packet = PacketIn.model_validate(vars(parsed))
        conn.execute(
            """UPDATE raw_packets SET ts_ms = ?, drop_count = ?, rx_id = ?, tx_id = ?,
                 rssi_dbm = ?, crc_ok = ?, crc_text = ?, data_hex = ?, data_len = ?,
                 light_raw = ?, temp_raw = ?, humidity_raw = ? WHERE id = ?""",
            (
                packet.ts_ms, packet.drop, packet.rx_id, packet.tx_id,
                packet.rssi_dbm, packet.crc_ok, packet.crc_text, packet.data_hex,
                packet.data_len, packet.light_raw, packet.temp_raw,
                packet.humidity_raw, row["id"],
            ),
        )
        return packet
    values = {key: row[key] for key in PacketIn.model_fields if key != "drop"}
    return PacketIn(**values, drop=row["drop_count"])


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    conn = db.connect(config.server.db_path)
    try:
        settings.apply_stored(conn, config)
        print(f"{rebuild(conn, config)} readings rebuilt in {config.server.db_path}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
