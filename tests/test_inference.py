"""Scenario matrix and rule behaviour (spec sections 10.3 and 17)."""

import threading

import pytest

from collector.parser import Packet, parse_line
from collector.simulator import Simulator, parse_args
from server import db, sun
from server.alerts import AlertManager, query_events
from server.calibration import Calibration
from server.config import Config
from server.inference import Engine, State, pearson
from server.ingest import IngestRequest, ingest

MINUTE, HOUR, DAY = 60_000, 3_600_000, 86_400_000
BASE = 1795104000000  # 2026-11-19 11:00 in New York
MIDNIGHT = BASE + 13 * HOUR
CALIBRATED = Calibration(light_k=0.8)


class Station:
    """Database, alert manager and engine, without a web server."""

    def __init__(self, path, source="simulator"):
        self.config = Config()
        self.source = source
        self.conn = db.connect(str(path))
        self.published = []
        self.emails = []
        self.light = Calibration()
        self.restart()

    def restart(self):
        self.alerts = AlertManager(
            self.conn, threading.Lock(), self.config,
            lambda name, data: self.published.append((name, dict(data))),
            self.emails.append,
        )
        self.engine = Engine(self.config, self.source, self.alerts)

    def send(self, packets):
        # Small hardware batches: the clock check allows 10 min.
        size = 5 if self.source == "hardware" else 100
        for i in range(0, len(packets), size):
            request = IngestRequest(source=self.source, packets=packets[i : i + size])
            ingest(self.conn, self.config, request, packets[i]["ts_ms"])
            self.tick()

    def tick(self, now=0):
        self.engine.tick(self.conn, self.light, now)

    def simulate(self, *argv):
        args = parse_args(["--seed", "1", *argv])
        self.send([
            vars(p) for _, line in Simulator(args, self.config).lines()
            if isinstance(p := parse_line(line), Packet)
        ])

    def feed(self, start, minutes, **values):
        """One reading a minute; values may be functions of the minute."""
        self.send([self.packet(start, i, **values) for i in range(minutes)])

    @staticmethod
    def packet(start, i, temp=15, rh=50, light=0, rssi=-60, tx=3378):
        def get(v):
            return v(i) if callable(v) else v

        ts = start + i * MINUTE
        values = dict(
            ts_ms=ts, drop=0, rx_id=1, tx_id=tx, rssi_dbm=get(rssi), crc_ok=True,
            crc_text="OK", data_hex="", data_len=0, light_raw=get(light),
            temp_raw=get(temp), humidity_raw=get(rh),
        )
        return {**values, "raw_line": repr(values)}

    def events(self, **kwargs):
        return query_events(self.conn, self.source, **kwargs)

    def types(self):
        return {e["type"] for e in self.events()}

    def one(self, kind):
        (event,) = self.events(kind=kind)
        return event


@pytest.fixture
def station(tmp_path):
    s = Station(tmp_path / "i.db")
    yield s
    s.conn.close()


# Every scenario fires what it should and nothing unrelated.

NOV, JULY = "2026-11-20T", "2026-07-20T"


@pytest.mark.parametrize(
    ("scenario", "start", "hours", "interval", "expected", "allowed"),
    [
        ("normal", NOV + "00:00", 36, 20, set(), set()),
        ("cold_front", NOV + "08:00", 10, 10, {"front_passage"}, set()),
        ("frost_night", NOV + "14:00", 17, 10,
         {"frost_risk_tonight", "freeze", "frost_likely"}, set()),
        ("heat_wave", JULY + "06:00", 14, 10, {"heat"}, set()),
        ("storm", NOV + "10:00", 8, 10, {"rain_onset", "storm_darkening"}, {"fog_dew"}),
        ("fog_morning", NOV + "00:00", 12, 10, {"fog_dew"}, set()),
        ("night_activity", NOV + "18:00", 10, 5, {"night_light"}, set()),
        ("tag_dies", NOV + "10:00", 2, 5, {"tag_offline"}, set()),
        ("sun_exposed", NOV + "07:00", 9, 10, {"solar_heating"}, set()),
        ("covered", NOV + "07:00", 9, 10, {"sensor_covered"}, {"storm_darkening"}),
        ("rssi_shift", NOV + "09:00", 6, 10, {"tag_moved"}, set()),
    ],
)
def test_scenarios(station, scenario, start, hours, interval, expected, allowed):
    station.simulate(
        "--scenario", scenario, "--start", start, "--interval", str(interval),
        "--duration", str(hours * 3600),
    )
    types = station.types()
    assert expected <= types
    assert types - expected <= allowed
    for event in station.events():
        assert any(c.isdigit() for c in event["explanation"]), event


def test_night_activity_is_classified(station):
    station.simulate("--scenario", "night_activity", "--start", NOV + "18:00",
                     "--interval", "5", "--duration", str(10 * 3600))
    sustained, brief = station.events(kind="night_light")
    assert "brief flash" in brief["explanation"]
    assert "Sustained" in sustained["explanation"]


def test_tag_dies_becomes_critical(station):
    station.simulate("--scenario", "tag_dies", "--start", NOV + "10:00",
                     "--interval", "5", "--duration", "7200", "--noise-rate", "0.1")
    assert station.one("tag_offline")["severity"] == "critical"
    assert [n for n, _ in station.published].count("alert") == 2


# Persistence and hysteresis


def test_freeze_needs_five_minutes(station):
    station.feed(MIDNIGHT, 10)
    station.feed(MIDNIGHT + 10 * MINUTE, 4, temp=lambda i: -0.5)
    station.feed(MIDNIGHT + 14 * MINUTE, 10, temp=5)
    assert "freeze" not in station.types()


def test_freeze_lifecycle(station):
    station.feed(MIDNIGHT, 20, temp=-1)
    assert station.one("freeze")["end_ms"] is None
    station.feed(MIDNIGHT + 20 * MINUTE, 30, temp=0.3)  # thawed, within hysteresis
    assert station.one("freeze")["end_ms"] is None
    station.feed(MIDNIGHT + 50 * MINUTE, 30, temp=1)
    event = station.one("freeze")
    assert event["start_ms"] == MIDNIGHT
    assert event["end_ms"] == MIDNIGHT + 65 * MINUTE
    names = [n for n, d in station.published if d["type"] == "freeze"]
    assert names == ["alert", "alert_cleared"]


def test_heat_escalates_but_does_not_step_down(station):
    station.feed(MIDNIGHT, 30, temp=29, rh=50)
    assert station.one("heat")["severity"] == "watch"
    station.feed(MIDNIGHT + 30 * MINUTE, 10, temp=36, rh=60)
    assert station.one("heat")["severity"] == "critical"
    station.feed(MIDNIGHT + 40 * MINUTE, 20, temp=29, rh=50)
    assert station.one("heat")["severity"] == "critical"


def test_unknown_for_an_hour_ends_an_event(station):
    station.feed(MIDNIGHT, 20, temp=-1)
    station.feed(MIDNIGHT + 3 * HOUR, 5, temp=10)
    assert station.one("freeze")["end_ms"] is not None


def test_long_gap_ends_events(station):
    station.feed(MIDNIGHT, 20, temp=-1)
    station.feed(MIDNIGHT + 3 * DAY, 5, temp=-1)
    freezes = station.events(kind="freeze")
    assert freezes[-1]["end_ms"] is not None


def test_front_fires_once(station):
    station.feed(MIDNIGHT, 120, temp=15, rh=50)
    station.feed(MIDNIGHT + 120 * MINUTE, 120,
                 temp=lambda i: 15 - min(i, 60) / 10, rh=50)
    assert len(station.events(kind="front_passage")) == 1


def test_rain_needs_all_conditions(station):
    station.feed(MIDNIGHT, 60, temp=15, rh=78)
    station.feed(MIDNIGHT + HOUR, 30, temp=15, rh=lambda i: 78 + min(i, 18))
    assert "rain_onset" not in station.types()  # no temperature drop


def test_rain_at_night(station):
    station.feed(MIDNIGHT, 60, temp=15, rh=78)
    station.feed(MIDNIGHT + HOUR, 20, temp=lambda i: 15 - min(i, 16) / 10,
                 rh=lambda i: 78 + min(i, 15))
    event = station.one("rain_onset")
    assert event["confidence"] == "medium"
    assert "Light data unavailable" in event["explanation"]


def test_sensor_stuck(station):
    station.feed(BASE, 40, temp=15, rh=50, light=lambda i: 100 + 10 * i)
    assert station.one("sensor_stuck")["end_ms"] is None
    station.feed(BASE + 40 * MINUTE, 5, temp=15.1, rh=50, light=500)
    assert station.one("sensor_stuck")["end_ms"] is not None


def test_constant_light_is_not_stuck(station):
    station.feed(BASE, 60, temp=15, rh=50, light=100)
    assert "sensor_stuck" not in station.types()


def test_covered_calibrated(station):
    station.light = CALIBRATED
    station.feed(BASE, 180, temp=5, light=lambda i: i % 2)
    event = station.one("sensor_covered")
    assert event["confidence"] == "medium"
    assert "snow, leaves" in event["title"]


def test_dark_overcast_is_not_covered(station):
    station.light = CALIBRATED
    station.feed(BASE, 180, temp=5, light=60)
    assert "sensor_covered" not in station.types()


def test_moved_tag_becomes_the_new_normal(station):
    station.feed(MIDNIGHT, 90, temp=lambda i: 15 + i % 3 / 10, rssi=-60)
    station.feed(MIDNIGHT + 90 * MINUTE, 90, temp=lambda i: 15 + i % 3 / 10, rssi=-72)
    assert station.one("tag_moved")["end_ms"] is not None


def test_foreign_tags(station):
    station.send([Station.packet(BASE, 5, tx=77)])
    station.feed(BASE, 30)
    event = station.one("foreign_tags")
    assert "77" in event["explanation"] and event["end_ms"] is None
    station.feed(BASE + 30 * MINUTE, 60)
    assert station.one("foreign_tags")["end_ms"] is not None


def test_battery_trend(station):
    midnight = MIDNIGHT
    for day in range(8):
        station.feed(midnight + day * DAY, 60 - day, temp=lambda i: 15 + i % 3 / 10,
                     rssi=-60 - day)
    assert "battery_trend" in station.types()


def test_steady_battery(station):
    for day in range(8):
        station.feed(MIDNIGHT + day * DAY, 60, temp=lambda i: 15 + i % 3 / 10)
    assert "battery_trend" not in station.types()


def test_open_windows_blocked_by_heat(station):
    station.feed(BASE, 30, temp=18, rh=50)
    assert "open_windows" in station.types()
    assert station.one("open_windows")["severity"] == "info"


def test_window_condensation(station):
    station.config.home.window_u_value = 5.8
    station.feed(BASE, 60, temp=-5, rh=60)
    assert "window_condensation" in station.types()


def test_frost_risk_needs_a_clear_sky(tmp_path):
    for factor, expected in ((1.0, True), (0.3, False)):
        s = Station(tmp_path / f"f{factor}.db")
        s.light = CALIBRATED
        s.feed(
            BASE, 10 * 60, rh=45, temp=lambda i: 9 - i / 60,
            light=lambda i: factor * 0.8 * sun.clear_sky(
                sun.elevation(s.config.station, BASE + i * MINUTE)),
        )
        assert ("frost_risk_tonight" in s.types()) is expected
        s.conn.close()


# Offline


def test_hardware_offline_and_recovery(tmp_path):
    s = Station(tmp_path / "o.db", source="hardware")
    s.feed(BASE, 30, temp=-12)
    last = BASE + 29 * MINUTE
    s.tick(now=last + 60_000)
    assert "tag_offline" not in s.types()  # one packet a minute is its normal rate
    s.tick(now=last + 310_000)
    assert s.one("tag_offline")["severity"] == "warning"
    assert "Cold" in s.one("tag_offline")["explanation"]
    s.tick(now=last + 700_000)
    assert s.one("tag_offline")["severity"] == "critical"
    assert {e["type"] for e in s.events(active=True)} == {"tag_offline"}
    ingest(s.conn, s.config,
           IngestRequest(source="hardware", packets=[s.packet(last, 12)]),
           last + 12 * MINUTE)
    s.tick(now=last + 12 * MINUTE)
    assert s.one("tag_offline")["end_ms"] is not None
    s.conn.close()


# Restarts and backlogs


def test_restart_continues_without_duplicates(station):
    station.feed(MIDNIGHT, 20, temp=-1)
    station.restart()
    station.feed(MIDNIGHT + 20 * MINUTE, 20, temp=-1)
    event = station.one("freeze")
    station.feed(MIDNIGHT + 40 * MINUTE, 20, temp=5)
    station.restart()
    station.feed(MIDNIGHT + 60 * MINUTE, 10, temp=5)
    assert station.one("freeze")["id"] == event["id"]
    assert station.one("freeze")["end_ms"] == MIDNIGHT + 55 * MINUTE


def test_persistence_survives_a_restart(station):
    station.feed(MIDNIGHT, 20, temp=5)
    station.feed(MIDNIGHT + 20 * MINUTE, 4, temp=-1)
    station.restart()
    station.feed(MIDNIGHT + 24 * MINUTE, 4, temp=-1)
    assert station.one("freeze")["start_ms"] == MIDNIGHT + 20 * MINUTE


def test_data_stored_while_the_engine_was_down_is_evaluated(station):
    station.feed(MIDNIGHT, 20, temp=5)
    for i in range(20, 80):
        ingest(station.conn, station.config, IngestRequest(
            source="simulator",
            packets=[station.packet(MIDNIGHT, i, temp=-1 if i < 50 else 8)]))
    station.restart()
    station.tick()
    event = station.one("freeze")
    assert event["start_ms"] == MIDNIGHT + 20 * MINUTE
    assert event["end_ms"] is not None


# Building blocks


def test_pearson():
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1)
    assert pearson([1, 2, 3], [3, 2, 1]) == pytest.approx(-1)
    assert pearson([1, 1, 1], [1, 2, 3]) is None


def test_state_windows():
    s = State(Config(), Calibration())
    s.now = 10 * MINUTE
    s.minutes.extend({"t": i * MINUTE, "x": i} for i in range(11))
    assert [r["x"] for r in s.window(3)] == [8, 9, 10]
    assert [r["x"] for r in s.window(3, before=2)] == [6, 7, 8]
    assert s.covered(100) is None
    assert s.mean("x", 3) == 9
