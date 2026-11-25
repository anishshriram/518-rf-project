"""Tests for server.calibration and the light values in the API."""

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from tests.clients import local_client

from collector.parser import Packet, parse_line
from collector.simulator import Simulator, parse_args
from server import calibration as cal
from server import db, sun
from server.app import create_app
from server.calibration import Calibration
from server.config import Config
from server.ingest import IngestRequest, ingest

MINUTE, DAY = 60_000, 86_400_000
START = round(
    datetime(2026, 6, 20, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000
)


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.server.db_path = str(tmp_path / "cal.db")
    return c


@pytest.fixture
def conn(config):
    connection = db.connect(config.server.db_path)
    yield connection
    connection.close()


def fill(conn, config, days, light, source="simulator", start=START):
    """Store one reading a minute; light(ts, elevation) gives the raw value."""
    packets = []
    for i in range(int(days * 1440)):
        ts = start + i * MINUTE
        value = light(ts, sun.elevation(config.station, ts))
        packets.append(dict(
            ts_ms=ts, drop=0, rx_id=1, tx_id=3378, rssi_dbm=-60, crc_ok=True,
            crc_text="OK", data_hex="", data_len=0, light_raw=value, temp_raw=20,
            humidity_raw=50, raw_line=f"{ts} {source}",
        ))
    for i in range(0, len(packets), 1000):
        ingest(conn, config, IngestRequest(source=source, packets=packets[i : i + 1000]))
    return packets[-1]["ts_ms"]


def clear(k=0.8, base=3):
    return lambda ts, elevation: base + k * sun.clear_sky(elevation)


def test_recovers_a_known_scale(conn, config):
    rng = random.Random(1)
    sky = clear()
    end = fill(conn, config, 2, lambda t, e: sky(t, e) + rng.choice([-1, 0, 1]))
    result = cal.calibrate(conn, config, "simulator", end)
    assert result.light_k == pytest.approx(0.8, rel=0.02)
    assert result.night_baseline == 3
    assert result.night_sigma == pytest.approx(1.4826)


def test_recovers_the_scale_under_mixed_clouds(conn, config):
    rng = random.Random(1)
    end = fill(
        conn, config, 2,
        lambda t, e: 3 + 0.8 * sun.clear_sky(e) * rng.choice([0.3, 0.6, 1.0]),
    )
    result = cal.calibrate(conn, config, "simulator", end)
    assert result.light_k == pytest.approx(0.8, rel=0.02)


def test_needs_a_day_of_data(conn, config):
    end = fill(conn, config, 0.7, clear())
    result = cal.calibrate(conn, config, "simulator", end)
    assert not result.calibrated
    assert result.max_seen > 700
    assert result.light_norm(result.max_seen) == 1
    assert result.clear_sky_norm(50) is None
    assert result.clearness(500, 50) is None


def test_covered_sensor_does_not_calibrate(conn, config):
    rng = random.Random(1)
    end = fill(conn, config, 2, lambda t, e: rng.choice([0, 1, 2]))
    assert not cal.calibrate(conn, config, "simulator", end).calibrated


def test_can_be_switched_off(conn, config):
    config.light.auto_calibrate = False
    end = fill(conn, config, 2, clear())
    assert not cal.calibrate(conn, config, "simulator", end).calibrated


def test_saturation(conn, config):
    sky = clear()
    end = fill(conn, config, 4, lambda t, e: min(500, sky(t, e)))
    result = cal.calibrate(conn, config, "simulator", end)
    assert result.saturation == 500
    assert result.is_saturated(500) and not result.is_saturated(499)
    assert result.light_k == pytest.approx(0.8, rel=0.02)


def test_no_false_saturation(conn, config):
    end = fill(conn, config, 4, clear())
    assert cal.calibrate(conn, config, "simulator", end).saturation is None


def test_sources_are_separate(conn, config):
    fill(conn, config, 2, clear(0.2), source="replay")
    end = fill(conn, config, 2, clear(0.8))
    result = cal.calibrate(conn, config, "simulator", end)
    assert result.light_k == pytest.approx(0.8, rel=0.02)


def test_save_and_load(conn):
    original = Calibration(0.8, 3.0, 1.5, 1023.0, 900.0, START)
    cal.save(conn, "simulator", original)
    cal.save(conn, "hardware", Calibration(updated_ms=START))
    assert cal.load(conn, "simulator") == original
    assert not cal.load(conn, "hardware").calibrated
    assert cal.load(conn, "replay") is None


def test_light_norm_is_clamped():
    c = Calibration(light_k=0.8, night_baseline=3)
    assert c.light_norm(0) == 0
    assert c.light_norm(403) == pytest.approx(0.5)
    assert c.light_norm(5000) == 1.2
    assert c.light_norm(None) is None


def test_clearness_needs_a_high_sun():
    c = Calibration(light_k=0.8)
    assert c.clearness(100, 10) is None
    assert c.clearness(0.4 * sun.clear_sky(40), 40) == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("clearness", "spread", "expected"),
    [
        (0.9, 0.02, "clear"), (0.75, 0.0, "clear"), (0.9, 0.1, "partly_cloudy"),
        (0.9, 0.2, "partly_cloudy"), (0.2, 0.2, "partly_cloudy"),
        (0.5, 0.05, "mostly_cloudy"), (0.35, 0.1, "mostly_cloudy"),
        (0.34, 0.0, "overcast"),
    ],
)
def test_classify_sky(clearness, spread, expected):
    assert cal.classify_sky(clearness, spread) == expected


NOON = START + 13 * 3_600_000


def minutes_of(factors, k=0.8):
    config = Config()
    rows = []
    for i, factor in enumerate(factors):
        t = NOON - (len(factors) - 1 - i) * MINUTE
        light = factor * k * sun.clear_sky(sun.elevation(config.station, t))
        rows.append({"t": t, "light_raw": light})
    return rows


@pytest.mark.parametrize(
    ("factors", "expected"),
    [
        ([1.0] * 15, "clear"), ([0.5] * 15, "mostly_cloudy"), ([0.2] * 15, "overcast"),
        ([0.3, 1.0] * 7, "partly_cloudy"), ([1.0] * 7, None),
    ],
)
def test_sky_condition(factors, expected):
    calibration = Calibration(light_k=0.8)
    sky, _ = cal.sky_condition(Config(), calibration, minutes_of(factors), NOON)
    assert sky == expected


def test_saturated_sky():
    calibration = Calibration(light_k=0.8, saturation=300.0)
    rows = [{**row, "light_raw": 300.0} for row in minutes_of([1.0] * 15)]
    sky, clearness = cal.sky_condition(Config(), calibration, rows, NOON)
    assert sky == "very_bright" and clearness <= 1


def test_night_needs_no_calibration():
    midnight = START + DAY
    assert cal.sky_condition(Config(), Calibration(), [], midnight) == ("night", None)


def test_api_before_and_after_calibration(config, monkeypatch):
    monkeypatch.setattr("server.app.calibration.RECALIBRATE_MS", 3_600_000)
    conn = db.connect(config.server.db_path)
    end = fill(conn, config, 1.5, clear())  # ends at noon
    params = {
        "from": end - 3_600_000, "to": end,
        "metrics": "light_norm,clear_sky_norm,clearness",
    }
    with local_client(create_app(config)) as client:
        for _ in range(100):
            data = client.get("/api/current").json()
            if not data["flags"]["calibrating_light"]:
                break
            import time

            time.sleep(0.05)
        assert data["flags"] == {"calibrating_light": False, "light_saturated": False,
                                 "solar_heating": False}
        assert data["sky"] == "clear"
        assert data["clearness"] == pytest.approx(1, abs=0.03)
        assert data["sun"]["is_daylight"]
        assert data["sun"]["sunrise_ms"] < end < data["sun"]["sunset_ms"]
        history = client.get("/api/history", params=params).json()
        series = history["series"]
        assert history["calibrated"] is True
        assert series["light_norm"]["avg"] == pytest.approx(
            series["clear_sky_norm"]["avg"], rel=0.05
        )
        assert len(history["night_intervals"]) == 2  # the two nights ahead
    stored = conn.execute("SELECT COUNT(*) FROM calibration WHERE key NOT LIKE '%position'").fetchone()[0]
    assert stored == 5
    conn.close()


def test_api_without_calibration(config):
    conn = db.connect(config.server.db_path)
    end = fill(conn, config, 0.5, clear())
    conn.close()
    with local_client(create_app(config)) as client:
        data = client.get("/api/current").json()
        assert data["flags"]["calibrating_light"] is True
        assert data["sky"] is None and 0 <= data["light_norm"] <= 1
        history = client.get(
            "/api/history",
            params={"from": end - 3_600_000, "to": end, "metrics": "clear_sky_norm"},
        ).json()
        assert history["calibrated"] is False
        assert set(history["series"]["clear_sky_norm"]["avg"]) == {None}


def test_empty_database(config):
    with local_client(create_app(config)) as client:
        data = client.get("/api/current").json()
        assert data["flags"]["calibrating_light"] is True
        assert data["sun"]["sunrise_ms"] is not None


def test_storm_scenario_is_seen_as_dark(conn, config):
    args = parse_args(["--scenario", "storm", "--start", "2026-11-20T12:00",
                       "--duration", str(int(1.14 * 86400)), "--interval", "20",
                       "--seed", "1", "--noise-rate", "0"])
    packets = [vars(p) for _, text in Simulator(args, config).lines()
               if isinstance(p := parse_line(text), Packet)]
    for i in range(0, len(packets), 1000):
        ingest(conn, config,
               IngestRequest(source="simulator", packets=packets[i : i + 1000]))
    from server.aggregates import minute_averages

    end = packets[-1]["ts_ms"]
    result = cal.calibrate(conn, config, "simulator", end)
    assert result.calibrated
    storm = packets[0]["ts_ms"] + int(3.25 * 3_600_000)  # 15:15 on the first day
    minutes = minute_averages(conn, "simulator", storm - 20 * MINUTE, storm)
    _, dark = cal.sky_condition(config, result, minutes, storm)
    calm = storm - 30 * MINUTE
    minutes = minute_averages(conn, "simulator", calm - 20 * MINUTE, calm)
    _, bright = cal.sky_condition(config, result, minutes, calm)
    assert bright > 0.75
    assert dark < bright - 0.4
