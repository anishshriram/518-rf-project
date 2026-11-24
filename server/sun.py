"""Sun position, sunrise/sunset and clear-sky light (spec sections 16.1, 16.2)."""

import math
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from astral import Observer
from astral import sun as astral_sun

from server.config import StationConfig

MINUTE = 60_000
NIGHT_BELOW = -6.0  # civil twilight ends
DAY_ABOVE = 10.0  # below this, cloud estimates are unreliable


def clear_sky(elevation_deg: float) -> float:
    """Haurwitz clear-sky irradiance in W/m2."""
    if elevation_deg <= 0:
        return 0.0
    cos_z = math.sin(math.radians(elevation_deg))
    return 1098 * cos_z * math.exp(-0.059 / cos_z)


def elevation(station: StationConfig, ts_ms: int) -> float:
    """Solar elevation in degrees, to the nearest minute."""
    return _elevation(station.latitude, station.longitude, round(ts_ms / MINUTE))


@lru_cache(maxsize=50_000)
def _elevation(latitude: float, longitude: float, minute: int) -> float:
    moment = datetime.fromtimestamp(minute * 60, UTC)
    return astral_sun.elevation(Observer(latitude, longitude, 0), moment)


def phase(station: StationConfig, ts_ms: int) -> str:
    """'night', 'dawn', 'dusk' or 'day'."""
    now = elevation(station, ts_ms)
    if now < NIGHT_BELOW:
        return "night"
    if now > DAY_ABOVE:
        return "day"
    return "dawn" if elevation(station, ts_ms + 2 * MINUTE) > now else "dusk"


def sun_times(station: StationConfig, day: date) -> tuple[int | None, int | None]:
    """Sunrise and sunset of a local date in epoch ms; None if there is none."""
    return _sun_times(station.latitude, station.longitude, station.timezone, day)


@lru_cache(maxsize=2000)
def _sun_times(latitude, longitude, timezone, day):
    observer = Observer(latitude, longitude, 0)
    tz = ZoneInfo(timezone)
    times = []
    for find in (astral_sun.sunrise, astral_sun.sunset):
        try:
            times.append(round(find(observer, day, tzinfo=tz).timestamp() * 1000))
        except ValueError:  # polar day or night
            times.append(None)
    return tuple(times)


def local_date(station: StationConfig, ts_ms: int) -> date:
    return datetime.fromtimestamp(ts_ms / 1000, ZoneInfo(station.timezone)).date()


def night_intervals(station: StationConfig, start: int, end: int) -> list[list[int]]:
    """Periods between sunset and sunrise that overlap [start, end]."""
    day = local_date(station, start) - timedelta(days=1)
    last = local_date(station, end) + timedelta(days=1)
    changes = []  # (time, is_sunset)
    while day <= last:
        rise, fall = sun_times(station, day)
        changes += [(rise, False)] if rise is not None else []
        changes += [(fall, True)] if fall is not None else []
        day += timedelta(days=1)
    changes.sort()
    if not changes:
        # Polar day or night throughout.
        return [[start, end]] if elevation(station, start) < 0 else []

    intervals = []
    dark_since = None if changes[0][1] else start
    for time, is_sunset in changes:
        if is_sunset:
            dark_since = time if dark_since is None else dark_since
        elif dark_since is not None:
            intervals.append([dark_since, time])
            dark_since = None
    if dark_since is not None:
        intervals.append([dark_since, end])
    return [[a, b] for a, b in intervals if b > start and a < end]
