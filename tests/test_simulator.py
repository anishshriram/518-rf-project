"""Tests for collector.simulator."""

import pytest

from collector.parser import Packet, Summary, parse_line
from collector.simulator import (
    BANNER,
    SCENARIOS,
    Simulator,
    parse_args,
    rh_from_dew_point,
)
from server.config import Config

DAY = 86_400
CLEAN = ["--noise-rate", "0", "--drop-rate", "0"]


def run(*argv):
    args = parse_args(["--seed", "1", "--start", "2026-11-20T00:00", *argv])
    return [line for _, line in Simulator(args, Config()).lines()]


def day(scenario, interval=60):
    """One simulated day of clean packets, as (hour, packet) pairs."""
    lines = run(
        "--scenario", scenario, "--interval", str(interval), "--duration", str(DAY),
        *CLEAN,
    )
    packets = [p for p in map(parse_line, lines) if isinstance(p, Packet)]
    first = packets[0].ts_ms
    return [((p.ts_ms - first) / 3_600_000, p) for p in packets]


def between(data, start, end):
    return [p for hour, p in data if start <= hour < end]


def test_inverse_magnus():
    assert rh_from_dew_point(20, 9.26) == pytest.approx(50, abs=0.1)
    assert rh_from_dew_point(5, 5) == pytest.approx(100)


def test_banner_comes_first():
    assert run("--duration", "1")[:5] == BANNER


def test_every_line_parses():
    for line in run("--duration", "600", "--noise-rate", "0.3")[5:]:
        assert isinstance(parse_line(line), (Packet, Summary)), line


def test_good_line_format():
    line = run("--duration", "1", *CLEAN)[5]
    packet = parse_line(line)
    assert packet.crc_ok and packet.tx_id == 3378 and packet.rx_id == 191
    assert packet.data_len == 10
    assert line.count("\t") == 6


def test_noise_and_drop_rates():
    packets = [
        p
        for p in map(
            parse_line,
            run("--duration", "5000", "--noise-rate", "0.1", "--drop-rate", "0.2"),
        )
        if isinstance(p, Packet)
    ]
    bad = [p for p in packets if not p.crc_ok]
    assert 400 < len(bad) < 600
    assert 3800 < len(packets) - len(bad) < 4200
    assert all(p.data_len == 20 and -110 <= p.rssi_dbm <= -105 for p in bad)


def test_summary_every_minute():
    results = [parse_line(line) for line in run("--duration", "301", *CLEAN)]
    summaries = [r for r in results if isinstance(r, Summary)]
    assert [(s.packets, s.ok_pct) for s in summaries] == [(60, 100.0)] * 5


def test_timestamps_increase():
    results = [
        parse_line(line) for line in run("--duration", "500", "--noise-rate", "0.5")
    ]
    ts = [p.ts_ms for p in results if isinstance(p, Packet)]
    assert ts == sorted(ts)


def test_same_seed_gives_same_output():
    assert run("--duration", "100") == run("--duration", "100")


def test_light_cap():
    data = run("--interval", "600", "--duration", str(DAY), "--light-cap", "100")
    lights = [
        p.light_raw for p in map(parse_line, data) if isinstance(p, Packet) and p.crc_ok
    ]
    assert max(lights) == 100


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_values_stay_physical(scenario):
    for _, p in day(scenario, 300):
        assert -40 <= p.temp_raw <= 85
        assert 0 <= p.humidity_raw <= 100
        assert p.light_raw >= 0


def test_normal_has_day_and_night():
    data = day("normal")
    assert max(p.light_raw for p in between(data, 0, 5)) < 15
    assert max(p.light_raw for p in between(data, 10, 14)) > 150


def test_cold_front():
    data = day("cold_front")
    before, after = between(data, 13.9, 14)[-1], between(data, 15.05, 15.1)[0]
    assert 4 < before.temp_raw - after.temp_raw < 6.5


def test_frost_night():
    p = between(day("frost_night"), 5, 5.02)[0]
    assert p.temp_raw == pytest.approx(-2, abs=0.4)


def test_heat_wave():
    assert max(p.temp_raw for _, p in day("heat_wave")) > 37


def test_storm():
    data = day("storm")
    before, after = between(data, 14.9, 15)[-1], between(data, 15.25, 15.3)[0]
    assert after.light_raw < 0.3 * before.light_raw
    assert after.humidity_raw - before.humidity_raw >= 10
    assert 2.5 < before.temp_raw - after.temp_raw < 3.5


def test_fog_morning():
    data = day("fog_morning")
    assert min(p.humidity_raw for p in between(data, 4.1, 8.9)) >= 95
    assert max(p.humidity_raw for p in between(data, 11, 16)) < 90


def test_night_activity():
    data = day("night_activity", 5)
    assert len([p for p in between(data, 23, 23.5) if p.light_raw > 200]) == 4
    assert len([p for p in between(data, 1.5, 2) if p.light_raw > 40]) == 60


def test_tag_dies():
    lines = run("--scenario", "tag_dies", "--duration", "7200", "--drop-rate", "0")
    packets = [p for p in map(parse_line, lines) if isinstance(p, Packet)]
    assert len([p for p in packets if p.crc_ok]) == 1800
    assert not packets[-1].crc_ok


def test_sun_exposed():
    data = day("sun_exposed")
    assert between(data, 10, 10.1)[0].temp_raw - between(data, 9.9, 10)[0].temp_raw > 2


def test_covered():
    data = day("covered")
    assert max(p.light_raw for p in between(data, 9.1, 23)) <= 2
    assert all(abs(p.temp_raw - 0.5) < 0.8 for _, p in data)


def test_rssi_shift():
    data = day("rssi_shift")

    def median(packets):
        return sorted(p.rssi_dbm for p in packets)[len(packets) // 2]

    shift = median(between(data, 8, 12)) - median(between(data, 12, 16))
    assert shift == pytest.approx(10, abs=1)
