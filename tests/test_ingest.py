"""Tests for server.ingest."""

import pytest

from collector.parser import parse_line
from server import db
from server.config import Config
from server.ingest import IngestRequest, ingest

NOW = 1795100000000


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


@pytest.fixture
def config():
    return Config()


def packet(ts=NOW, **changes):
    values = dict(
        ts_ms=ts, drop=0, rx_id=191, tx_id=3378, rssi_dbm=-62.5, crc_ok=True,
        crc_text="OK", data_hex="00", data_len=1, light_raw=140.0, temp_raw=20.0,
        humidity_raw=45.0,
    )
    values.update(changes)
    values.setdefault("raw_line", repr(values))
    return values


def send(conn, config, *packets, source="hardware", now=NOW, **extra):
    request = IngestRequest(source=source, packets=list(packets), **extra)
    counts, _ = ingest(conn, config, request, now_ms=now)
    return counts


def readings(conn):
    return conn.execute("SELECT * FROM readings ORDER BY id").fetchall()


def raw_count(conn):
    return conn.execute("SELECT COUNT(*) FROM raw_packets").fetchone()[0]


def test_good_packet_is_stored(conn, config):
    counts = send(conn, config, packet())
    assert counts == {"accepted": 1, "rejected": 0, "bad_crc": 0, "foreign": 0}
    (row,) = readings(conn)
    assert (row["temp_c"], row["rh_pct"], row["light_raw"]) == (20.0, 45.0, 140.0)
    assert (row["quality"], row["reject_reason"]) == ("good", None)


def test_real_noise_is_stored_but_not_read(conn, config):
    from dataclasses import asdict

    with open("tests/fixtures/observed_noise.txt", encoding="utf-8") as f:
        packets = [asdict(parse_line(line)) for line in f]
    counts = send(conn, config, *packets)
    assert counts["bad_crc"] == 2
    assert raw_count(conn) == 2
    assert readings(conn) == []


def test_foreign_tag(conn, config):
    assert send(conn, config, packet(tx_id=5))["foreign"] == 1
    assert raw_count(conn) == 1
    assert readings(conn) == []


def test_empty_allowlist_accepts_any_tag(conn, config):
    config.tag.allowed_tx_ids = []
    assert send(conn, config, packet(tx_id=5))["accepted"] == 1


def test_unit_conversion(conn, config):
    config.units.temp_input_unit = "F"
    config.units.temp_offset_c = 0.5
    config.units.humidity_scale = 0.01
    config.units.humidity_offset_pct = 1.0
    send(conn, config, packet(temp_raw=68.0, humidity_raw=4500.0))
    (row,) = readings(conn)
    assert row["temp_c"] == pytest.approx(20.5)
    assert row["rh_pct"] == pytest.approx(46.0)


@pytest.mark.parametrize(
    ("changes", "quality", "reason"),
    [
        ({"temp_raw": 85.0}, "good", None),
        ({"temp_raw": 85.1}, "rejected", "temp_range"),
        ({"temp_raw": -40.1}, "rejected", "temp_range"),
        ({"humidity_raw": -1.0}, "rejected", "rh_range"),
        ({"humidity_raw": 103.1}, "rejected", "rh_range"),
        ({"humidity_raw": 102.0}, "suspect", "rh_over_100"),
        ({"temp_raw": None}, "rejected", "missing_value"),
        ({"humidity_raw": None}, "rejected", "missing_value"),
        ({"light_raw": None}, "rejected", "missing_value"),
    ],
)
def test_quality(conn, config, changes, quality, reason):
    send(conn, config, packet(**changes))
    (row,) = readings(conn)
    assert (row["quality"], row["reject_reason"]) == (quality, reason)


def test_rh_is_clipped(conn, config):
    send(conn, config, packet(humidity_raw=102.0))
    assert readings(conn)[0]["rh_pct"] == 100.0


def test_spike_is_rejected_and_next_reading_recovers(conn, config):
    send(
        conn, config,
        packet(NOW),
        packet(NOW + 1000, temp_raw=30.0),
        packet(NOW + 2000, temp_raw=20.5),
        packet(NOW + 3000, humidity_raw=80.0),
    )
    assert [r["quality"] for r in readings(conn)] == [
        "good", "rejected", "good", "rejected",
    ]
    assert readings(conn)[1]["reject_reason"] == "spike"


def test_large_change_after_a_gap_is_allowed(conn, config):
    send(conn, config, packet(NOW), packet(NOW + 60_000, temp_raw=30.0))
    assert [r["quality"] for r in readings(conn)] == ["good", "good"]


def test_step_check_ignores_other_sources(conn, config):
    send(conn, config, packet(NOW), source="simulator")
    send(conn, config, packet(NOW + 1000, temp_raw=-10.0))
    assert [r["quality"] for r in readings(conn)] == ["good", "good"]


def test_duplicate_timestamp_is_rejected(conn, config):
    counts = send(conn, config, packet(), packet(temp_raw=20.1))
    assert (counts["accepted"], counts["rejected"]) == (1, 1)
    assert readings(conn)[1]["reject_reason"] == "duplicate"


def test_resent_batch_is_not_stored_twice(conn, config):
    send(conn, config, packet())
    assert send(conn, config, packet())["rejected"] == 1
    assert raw_count(conn) == 1
    assert len(readings(conn)) == 1


@pytest.mark.parametrize("offset", [-601_000, 601_000])
def test_clock_skew_applies_to_hardware_only(conn, config, offset):
    send(conn, config, packet(NOW + offset))
    send(conn, config, packet(NOW + offset), source="simulator")
    send(conn, config, packet(NOW + offset), source="replay")
    assert [r["quality"] for r in readings(conn)] == ["suspect", "good", "good"]
    assert readings(conn)[0]["reject_reason"] == "clock_skew"


def test_spooled_backlog_is_not_clock_skew(conn, config):
    hour = 3_600_000
    send(conn, config, packet(NOW - hour), delay_ms=hour)
    assert readings(conn)[0]["quality"] == "good"


def test_summaries_are_stored(conn, config):
    send(conn, config, summaries=[{"packets": 60, "ok_pct": 98.3, "raw_line": "#"}])
    row = conn.execute("SELECT * FROM receiver_stats").fetchone()
    assert (row["packets"], row["ok_pct"]) == (60, 98.3)


def test_database_can_be_reopened(tmp_path):
    path = str(tmp_path / "sub" / "a.db")
    db.connect(path).close()
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.close()
