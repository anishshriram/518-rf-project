"""Tests for daily summaries, health and CSV export."""

import csv
import io
import sqlite3
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from collector.parser import Packet, parse_line
from collector.simulator import Simulator, parse_args
from server import daily, db, health, rebuild, sun
from server.app import create_app
from server.calibration import Calibration
from server.config import Config
from server.ingest import IngestRequest, ingest

MINUTE, HOUR, DAY = 60_000, 3_600_000, 86_400_000
NY = ZoneInfo("America/New_York")
LIGHT = Calibration(light_k=0.8)


def ms(*args):
    return round(datetime(*args, tzinfo=NY).timestamp() * 1000)


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.server.db_path = str(tmp_path / "d.db")
    return c


@pytest.fixture
def conn(config):
    connection = db.connect(config.server.db_path)
    yield connection
    connection.close()


def packets(start, count, step=MINUTE, temp=10, light=0, rh=50, skip=(), source=""):
    def get(v, i):
        return v(i) if callable(v) else v

    return [
        dict(
            ts_ms=start + i * step, drop=0, rx_id=191, tx_id=3378, rssi_dbm=-60,
            crc_ok=True, crc_text="OK", data_hex="", data_len=0,
            light_raw=get(light, i), temp_raw=get(temp, i), humidity_raw=get(rh, i),
            raw_line=f"{start} {i} {source}",
        )
        for i in range(count)
        if i not in skip
    ]


def fill(conn, config, items, source="simulator"):
    for i in range(0, len(items), 1000):
        ingest(conn, config, IngestRequest(source=source, packets=items[i : i + 1000]))


def sunny(config, start, factor=1.0):
    return lambda i: factor * 0.8 * sun.clear_sky(
        sun.elevation(config.station, start + i * MINUTE)
    )


DAY1 = date(2026, 11, 20)
START = ms(2026, 11, 20)


def summary(conn, config, day=DAY1, light=LIGHT, now=START + 2 * DAY):
    return daily.summarise(conn, config, "simulator", day, light, now)


@pytest.mark.parametrize(
    ("day", "hours"),
    [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25), (date(2026, 7, 4), 24)],
)
def test_day_length_follows_daylight_saving(day, hours):
    start, end = daily.day_bounds(day, "America/New_York")
    assert end - start == hours * HOUR


def test_clear_day(conn, config):
    fill(conn, config, packets(START, 1440, temp=lambda i: 5 + i / 144,
                               light=sunny(config, START)))
    s = summary(conn, config)
    assert (s["temp_min_c"], s["temp_min_time_ms"]) == (5, START)
    assert s["temp_max_time_ms"] == START + 1439 * MINUTE
    assert s["hdd"] == pytest.approx(8, abs=0.02)
    assert s["cdd"] == 0
    assert s["gdd"] == pytest.approx(0, abs=0.01)
    assert s["sky_summary"] == "mostly clear"
    assert s["daylight_hours_astronomical"] == pytest.approx(9.74, abs=0.05)
    assert 7.5 < s["bright_sun_hours"] < 9
    assert 8 < s["daylight_hours_measured"] < 9.8
    assert 0 < s["sunrise_detected_ms"] - s["sunrise_astro_ms"] < 45 * MINUTE
    assert -45 * MINUTE < s["sunset_detected_ms"] - s["sunset_astro_ms"] < 5 * MINUTE
    assert s["data_coverage_pct"] == pytest.approx(100)
    assert s["packet_success_pct"] == pytest.approx(100)


@pytest.mark.parametrize(
    ("factor", "sky"),
    [(1.0, "mostly clear"), (0.6, "partly cloudy"), (0.4, "mostly cloudy"),
     (0.2, "overcast")],
)
def test_sky_summary(conn, config, factor, sky):
    fill(conn, config, packets(START, 1440, light=sunny(config, START, factor)))
    assert summary(conn, config)["sky_summary"] == sky


def test_shade_delays_detected_sunrise(conn, config):
    light = sunny(config, START)
    fill(conn, config, packets(
        START, 1440, light=lambda i: light(i) if i > 9 * 60 else 0))
    s = summary(conn, config)
    assert s["sunrise_detected_ms"] == START + (9 * 60 + 1) * MINUTE


def test_lamp_at_night_is_not_sunrise(conn, config):
    light = sunny(config, START)
    fill(conn, config, packets(
        START, 1440, light=lambda i: 500 if 120 <= i < 150 else light(i)))
    s = summary(conn, config)
    assert s["sunrise_detected_ms"] > START + 6 * HOUR
    assert s["daylight_hours_measured"] < 9.8


def test_no_sunrise_if_data_begins_in_daylight(conn, config):
    noon = START + 12 * HOUR
    fill(conn, config, packets(noon, 700, light=sunny(config, noon)))
    s = summary(conn, config)
    assert s["sunrise_detected_ms"] is None
    assert s["sunset_detected_ms"] is not None
    assert s["data_coverage_pct"] == pytest.approx(48.6, abs=0.1)


def test_uncalibrated_day_has_no_sun_figures(conn, config):
    fill(conn, config, packets(START, 1440, light=sunny(config, START)))
    s = summary(conn, config, light=Calibration())
    assert s["bright_sun_hours"] is None and s["sky_summary"] is None
    assert s["sunrise_astro_ms"] is not None


def test_unfinished_day_coverage(conn, config):
    fill(conn, config, packets(START, 60))
    s = summary(conn, config, now=START + 2 * HOUR)
    assert s["data_coverage_pct"] == pytest.approx(50)


def test_empty_day(conn, config):
    assert summary(conn, config) is None


def test_missing_days_skips_gaps(conn, config):
    fill(conn, config, packets(START, 10) + packets(START + 2 * DAY, 10))
    args = (conn, config, "simulator", date(2026, 11, 24), 9)
    assert daily.missing_days(*args) == [DAY1, date(2026, 11, 22)]
    daily.store(conn, summary(conn, config))
    assert daily.missing_days(*args) == [date(2026, 11, 22)]


def test_totals():
    rows = [
        {"local_date": d, "hdd": 1.0, "cdd": 2.0, "gdd": 3.0}
        for d in ("2025-12-31", "2026-01-01", "2026-06-30", "2026-07-01", "2026-11-05")
    ]
    t = daily.totals(rows, date(2026, 11, 20))
    assert t["month"] == {"since": "2026-11-01", "hdd": 1, "cdd": 2, "gdd": 3}
    assert t["season"]["hdd"] == 2 and t["season"]["hdd_since"] == "2026-07-01"
    assert t["season"]["gdd"] == 12
    assert daily.totals(rows, date(2026, 3, 1))["season"]["hdd"] == 5


# Health


def test_beacon_and_success(conn, config):
    skip = {i for i in range(600) if i % 10 == 5}
    fill(conn, config, packets(START, 600, step=2000, skip=skip))
    assert health.beacon_interval(conn, "simulator") == 2
    got = health.packet_success(conn, config, "simulator", START, START + HOUR)
    assert got == pytest.approx(90, abs=0.5)


def test_success_counts_silence(conn, config):
    fill(conn, config, packets(START, 600, step=1000))
    got = health.packet_success(
        conn, config, "simulator", START, START + HOUR, until=START + 1200_000
    )
    assert got == pytest.approx(50, abs=0.5)


def test_health_in_tag_dies(config):
    conn = db.connect(config.server.db_path)
    args = parse_args(["--scenario", "tag_dies", "--start", "2026-11-20T10:00",
                       "--duration", "3600", "--noise-rate", "0.1", "--seed", "1"])
    items = [vars(p) for _, line in Simulator(args, config).lines()
             if isinstance(p := parse_line(line), Packet)]
    fill(conn, config, items)
    bad = sum(not p["crc_ok"] for p in items)
    conn.close()
    with TestClient(create_app(config)) as client:
        data = client.get("/api/health").json()
        current = client.get("/api/current").json()
    tag = data["tag"]
    assert tag["status"] == current["status"] == "offline_critical"
    assert tag["last_seen_ms"] == max(p["ts_ms"] for p in items if p["crc_ok"])
    assert tag["packet_success_pct_1h"] == pytest.approx(49.5, abs=1.5)
    assert tag["bad_crc_per_h"] == bad
    assert tag["beacon_interval_s_measured"] == pytest.approx(1)
    assert data["receiver"]["rx_id"] == 191
    assert data["collector"]["source"] == "simulator"
    assert data["server"]["db_size_mb"] > 0


def test_health_of_empty_database(config):
    with TestClient(create_app(config)) as client:
        data = client.get("/api/health").json()
    assert data["tag"]["status"] == "never_seen" and data["tag"]["tx_id"] == 3378


def test_slow_beacon_is_online(conn, config):
    now = int(time.time() * 1000)
    fill(conn, config, packets(now - 600_000, 10, step=MINUTE), source="hardware")
    latest = db.latest_reading(conn)
    assert health.tag_status(conn, config, latest, now)["status"] == "online"


# API


def wait_for(get, timeout=15):
    end = time.monotonic() + timeout
    while not (value := get()):
        assert time.monotonic() < end, "timed out"
        time.sleep(0.1)
    return value


def test_daily_api(config):
    conn = db.connect(config.server.db_path)
    fill(conn, config, packets(START, 1440 * 2 + 600, light=sunny(config, START)))
    conn.close()
    with TestClient(create_app(config)) as client:
        def ready():
            rows = client.get("/api/daily").json()
            return rows if len(rows) == 3 and rows[0]["bright_sun_hours"] else None

        rows = wait_for(ready, 30)
        assert [r["local_date"] for r in rows] == [
            "2026-11-20", "2026-11-21", "2026-11-22"]
        assert [r["in_progress"] for r in rows] == [False, False, True]
        only = client.get("/api/daily?from=2026-11-21&to=2026-11-21").json()
        assert [r["local_date"] for r in only] == ["2026-11-21"]
        totals = client.get("/api/daily/totals").json()
        assert totals["month"]["hdd"] == pytest.approx(sum(r["hdd"] for r in rows))
        assert client.get("/api/daily?from=nonsense").status_code == 422


def test_late_data_updates_a_stored_day(config):
    conn = db.connect(config.server.db_path)
    fill(conn, config, packets(START, 600) + packets(START + DAY, 600))
    conn.close()
    with TestClient(create_app(config)) as client:
        def first_max():
            rows = client.get("/api/daily?to=2026-11-20").json()
            return rows and rows[0]["temp_max_c"]

        assert wait_for(first_max) == 10
        late = packets(START + 15 * HOUR, 5, temp=17, source="late")
        newest = packets(START + DAY + 11 * HOUR, 5, source="newest")
        for batch in (late, newest):
            body = {"source": "simulator", "packets": batch}
            client.post("/api/ingest", json=body, headers={"X-Ingest-Token": "change-me"})
        wait_for(lambda: first_max() == 17, 25)


def read_csv(client, **params):
    response = client.get("/api/export.csv", params=params)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "pip-weather-simulator-" in response.headers["content-disposition"]
    return list(csv.DictReader(io.StringIO(response.text)))


def test_csv(config):
    conn = db.connect(config.server.db_path)
    items = packets(START, 2500, step=1000, temp=lambda i: 99 if i == 5 else 10)
    fill(conn, config, items)
    conn.close()
    with TestClient(create_app(config)) as client:
        raw = read_csv(client, resolution="raw")
        assert len(raw) == 2500
        assert raw[0]["time"] == "2026-11-20T00:00:00-05:00"
        assert raw[0]["source"] == "simulator"
        assert raw[5]["quality"] == "rejected"
        minute = read_csv(client, resolution="1m")
        assert len(minute) == 42
        assert float(minute[0]["temp_c_avg"]) == 10 and minute[0]["n"] == "59"
        one = read_csv(client, **{"from": START, "to": START, "resolution": "raw"})
        assert len(one) == 1
        bad = client.get("/api/export.csv", params={"from": 5, "to": 1})
        assert bad.status_code == 422


def test_csv_of_empty_database(config):
    with TestClient(create_app(config)) as client:
        text = client.get("/api/export.csv?resolution=1m").text
    assert text.startswith("time,ts_ms,source,n,") and text.count("\n") == 1


# Database


def test_version_1_database_is_upgraded(tmp_path, config):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE schema_version (version INTEGER NOT NULL);
           INSERT INTO schema_version VALUES (1);
           CREATE TABLE daily_summary (local_date TEXT PRIMARY KEY);
           CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT,
             updated_ms INTEGER);
           INSERT INTO settings VALUES ('kept', '1', 1);"""
    )
    old.close()
    for _ in range(2):
        conn = db.connect(path)
        columns = [r["name"] for r in conn.execute("PRAGMA table_info(daily_summary)")]
        assert "source" in columns
        assert conn.execute("SELECT version FROM schema_version").fetchall()[0][0] == 2
        assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1
        conn.close()


def test_rebuild_drops_summaries(conn, config):
    fill(conn, config, packets(START, 100))
    daily.store(conn, summary(conn, config))
    rebuild.rebuild(conn, config)
    assert daily.load(conn, "simulator", DAY1, DAY1) == []
