"""Tests for server.sun."""

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from server import sun
from server.config import StationConfig

RUTGERS = StationConfig()
TZ = ZoneInfo(RUTGERS.timezone)
HOUR = 3_600_000


def ms(*args):
    return round(datetime(*args, tzinfo=TZ).timestamp() * 1000)


def noaa_position(ts_s):
    """Independent check: NOAA's full algorithm, as (declination in radians,
    equation of time in minutes)."""
    t = (ts_s / 86400 + 2440587.5 - 2451545) / 36525
    rad = math.radians
    l0 = (280.46646 + t * (36000.76983 + t * 0.0003032)) % 360
    m = rad(357.52911 + t * (35999.05029 - 0.0001537 * t))
    e = 0.016708634 - t * (0.000042037 + 0.0000001267 * t)
    center = (
        math.sin(m) * (1.914602 - t * (0.004817 + 0.000014 * t))
        + math.sin(2 * m) * (0.019993 - 0.000101 * t)
        + math.sin(3 * m) * 0.000289
    )
    omega = rad(125.04 - 1934.136 * t)
    longitude = rad(l0 + center - 0.00569 - 0.00478 * math.sin(omega))
    tilt = 23 + (26 + (21.448 - t * (46.815 + t * 0.00059)) / 60) / 60
    tilt = rad(tilt + 0.00256 * math.cos(omega))
    y = math.tan(tilt / 2) ** 2
    l0 = rad(l0)
    eqtime = 4 * math.degrees(
        y * math.sin(2 * l0) - 2 * e * math.sin(m)
        + 4 * e * y * math.sin(m) * math.cos(2 * l0)
        - y * y / 2 * math.sin(4 * l0) - 1.25 * e * e * math.sin(2 * m)
    )
    return math.asin(math.sin(tilt) * math.sin(longitude)), eqtime


def noaa_sun_times(day, latitude, longitude):
    midnight = datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("UTC")).timestamp()
    lat = math.radians(latitude)
    times = []
    for side in (1, -1):
        ts = midnight + 12 * 3600 - longitude * 240
        for _ in range(3):  # refine with the sun's position at the event itself
            decl, eqtime = noaa_position(ts)
            ha = math.degrees(math.acos(
                math.cos(math.radians(90.833)) / (math.cos(lat) * math.cos(decl))
                - math.tan(lat) * math.tan(decl)
            ))
            ts = midnight + 60 * (720 - 4 * (longitude + side * ha) - eqtime)
        times.append(ts * 1000)
    return times


@pytest.mark.parametrize(
    ("day", "expected"), [((2026, 6, 21), 73), ((2026, 12, 21), 26)]
)
def test_noon_elevation(day, expected):
    start = ms(*day, 11)
    top = max(sun.elevation(RUTGERS, start + i * 60_000) for i in range(180))
    assert top == pytest.approx(expected, abs=0.5)


@pytest.mark.parametrize(
    "day", [date(2026, 3, 20), date(2026, 6, 21), date(2026, 9, 22), date(2026, 12, 21)]
)
def test_sunrise_and_sunset(day):
    expected = noaa_sun_times(day, RUTGERS.latitude, RUTGERS.longitude)
    actual = sun.sun_times(RUTGERS, day)
    for got, want in zip(actual, expected):
        assert abs(got - want) < 2 * 60_000
        assert sun.local_date(RUTGERS, got) == day


def test_clear_sky():
    assert sun.clear_sky(-1) == sun.clear_sky(0) == 0
    assert sun.clear_sky(90) == pytest.approx(1098 * math.exp(-0.059))
    assert 0 < sun.clear_sky(0.1) < sun.clear_sky(30) < sun.clear_sky(60)


def test_phases():
    phases = [sun.phase(RUTGERS, ms(2026, 11, 20, h)) for h in (3, 7, 12, 16, 22)]
    assert phases == ["night", "dawn", "day", "dusk", "night"]


def test_night_intervals():
    start, end = ms(2026, 11, 20, 12), ms(2026, 11, 22, 12)
    nights = sun.night_intervals(RUTGERS, start, end)
    assert len(nights) == 2
    for a, b in nights:
        assert sun.elevation(RUTGERS, a + HOUR) < 0 > sun.elevation(RUTGERS, b - HOUR)
        assert sun.elevation(RUTGERS, a - HOUR) > 0 < sun.elevation(RUTGERS, b + HOUR)


def test_night_already_under_way():
    nights = sun.night_intervals(RUTGERS, ms(2026, 11, 20, 1), ms(2026, 11, 20, 2))
    assert len(nights) == 1
    assert nights[0][0] < ms(2026, 11, 20, 1) and nights[0][1] > ms(2026, 11, 20, 2)


def test_no_night_at_midday():
    assert sun.night_intervals(RUTGERS, ms(2026, 11, 20, 11), ms(2026, 11, 20, 13)) == []


def test_polar_regions():
    north = StationConfig(latitude=78.2, longitude=15.6, timezone="Europe/Oslo")
    summer, winter = ms(2026, 6, 21), ms(2026, 12, 21)
    assert sun.sun_times(north, date(2026, 6, 21)) == (None, None)
    assert sun.night_intervals(north, summer, summer + HOUR) == []
    assert sun.night_intervals(north, winter, winter + HOUR) == [[winter, winter + HOUR]]
