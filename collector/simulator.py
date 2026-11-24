"""Emit synthetic lines in the pip_sense output format (spec section 10)."""

import argparse
import math
import os
import random
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from astral import Observer
from astral.sun import elevation, sunrise

from server.config import Config, ConfigError, load_config
from server.derive import rh_from_dew_point
from server.sun import clear_sky

BANNER = [
    "parameters are ac:3",
    "server ipl port 0",
    "Connected to USB Tag Reader.",
    "New pipsqueak opened.",
    "Connected to the GRAIL aggregation server.",
]
RX_ID = 191
SUMMARY_INTERVAL_S = 60.0
NOON_LIGHT = 800.0
TAG_DIES_AFTER_S = 30 * 60


@dataclass(frozen=True)
class Scenario:
    mean_c: float = 8.0
    amplitude_c: float = 5.0
    dew_point_c: float = 0.0
    min_hour: float | None = None  # hour of the daily minimum; None = sunrise
    cloud: float | None = None  # fixed cloud factor; None = varying
    drift: bool = True


SCENARIOS = {
    "normal": Scenario(),
    "cold_front": Scenario(drift=False),
    "frost_night": Scenario(
        mean_c=3, dew_point_c=-3, min_hour=5, cloud=1.0, drift=False
    ),
    "heat_wave": Scenario(mean_c=33, dew_point_c=24, drift=False),
    "storm": Scenario(
        mean_c=14, amplitude_c=1, dew_point_c=12, cloud=0.9, drift=False
    ),
    "fog_morning": Scenario(drift=False),
    "night_activity": Scenario(),
    "tag_dies": Scenario(),
    "sun_exposed": Scenario(cloud=1.0, drift=False),
    "covered": Scenario(mean_c=0.5, amplitude_c=0.3, dew_point_c=-2, drift=False),
    "rssi_shift": Scenario(),
}


def ramp(x: float, start: float, length: float) -> float:
    """0 before start, rising linearly to 1 over length."""
    return min(1.0, max(0.0, (x - start) / length))


class Weather:
    """Evolving physical state; call sample() with increasing times."""

    def __init__(self, scenario: str, start: datetime, config: Config, rng):
        self._name = scenario
        self._params = SCENARIOS[scenario]
        self._start = start
        self._tz = start.tzinfo
        self._rng = rng
        self._observer = Observer(
            config.station.latitude,
            config.station.longitude,
            config.station.elevation_m,
        )
        self._light_per_wm2 = NOON_LIGHT / max(1.0, clear_sky(self._noon_elevation()))
        self._last_ts: float | None = None
        self._noise_t = 0.0
        self._drift_t = 0.0
        self._drift_td = 0.0
        self._cloud = 0.85
        self._cloud_target = 0.85
        self._minute = None
        self._sky = 0.0
        self._sunrise_cache: dict = {}

    def _noon_elevation(self) -> float:
        noon = self._start.replace(hour=12, minute=0, second=0, microsecond=0)
        return max(
            elevation(self._observer, noon + timedelta(minutes=m))
            for m in range(-120, 121, 10)
        )

    def _min_hour(self, date) -> float:
        if self._params.min_hour is not None:
            return self._params.min_hour
        if date not in self._sunrise_cache:
            try:
                rise = sunrise(self._observer, date, tzinfo=self._tz)
                hour = rise.hour + rise.minute / 60
            except ValueError:  # no sunrise at high latitudes
                hour = 6.0
            self._sunrise_cache[date] = min(max(hour, 3.0), 12.0)
        return self._sunrise_cache[date]

    def _diurnal(self, local: datetime, hour: float) -> float:
        low, high = self._min_hour(local.date()), 15.0
        if low <= hour <= high:
            shape = -math.cos(math.pi * (hour - low) / (high - low))
        else:
            since_high = (hour - high) % 24
            shape = math.cos(math.pi * since_high / (low + 24 - high))
        return self._params.mean_c + self._params.amplitude_c * shape

    def _advance(self, ts: float) -> None:
        dt = 0.0 if self._last_ts is None else max(0.0, ts - self._last_ts)
        self._last_ts = ts
        gauss = self._rng.gauss
        # AR(1) noise with a 60 s memory and a standard deviation of 0.05 C.
        rho = math.exp(-dt / 60)
        self._noise_t = rho * self._noise_t + gauss(0, 0.05 * math.sqrt(1 - rho**2))
        if self._params.drift:
            self._drift_t += gauss(0, 0.3 * math.sqrt(dt / 3600))
            self._drift_td += gauss(0, 0.15 * math.sqrt(dt / 3600))
        if self._params.cloud is not None:
            self._cloud = self._params.cloud
        else:
            if self._rng.random() < dt / 1800:
                self._cloud_target = self._rng.uniform(0.2, 1.0)
            self._cloud += (self._cloud_target - self._cloud) * (1 - math.exp(-dt / 300))

    def _event_hours(self, local: datetime, event_hour: float) -> float:
        """Hours since the first occurrence of event_hour at or after the start."""
        event = self._start.replace(
            hour=int(event_hour), minute=0, second=0, microsecond=0
        )
        if event < self._start:
            event += timedelta(days=1)
        return (local.timestamp() - event.timestamp()) / 3600

    def sample(self, ts: float) -> tuple[float, float, float]:
        """Return (temp_c, rh_pct, light_raw) at epoch second ts."""
        self._advance(ts)
        local = datetime.fromtimestamp(ts, self._tz)
        hour = local.hour + local.minute / 60 + local.second / 3600
        name = self._name

        minute = int(ts // 60)
        if minute != self._minute:
            self._minute = minute
            self._sky = clear_sky(elevation(self._observer, local))
        light = self._light_per_wm2 * self._sky * self._cloud

        temp = self._diurnal(local, hour) + self._drift_t + self._noise_t
        dew = self._params.dew_point_c + self._drift_td
        storm = 0.0

        if name == "cold_front":
            change = ramp(self._event_hours(local, 14), 0, 1)
            temp -= 5 * change
            dew -= 4 * change
        elif name == "storm":
            since = self._event_hours(local, 15)
            storm = ramp(since, 0, 1 / 6) - ramp(since, 2, 1)
            temp -= 3 * storm
            light *= 1 - 0.8 * storm
        elif name == "sun_exposed" and 10 <= hour < 14:
            temp += 6 * min(1.0, light / NOON_LIGHT)

        # Keep some spread so that only the fog scenario looks foggy.
        dew = min(dew, temp - 0.8)
        if name == "fog_morning":
            fog = ramp(hour, 3, 1) - ramp(hour, 9, 1)
            dew += (temp - 0.3 - dew) * fog
        rh = rh_from_dew_point(temp, dew)
        rh += (97 - rh) * storm
        rh = min(100.0, max(0.0, rh + self._rng.gauss(0, 0.5)))

        light += self._rng.gauss(0, 1.5)
        if name == "night_activity":
            second = local.hour * 3600 + local.minute * 60 + local.second
            if 0 <= second - (23 * 3600 + 12 * 60) < 20:
                light = 300 + self._rng.gauss(0, 5)
            elif 0 <= second - (1 * 3600 + 40 * 60) < 300:
                light = 60 + self._rng.gauss(0, 2)
        elif name == "covered":
            since = self._event_hours(local, 9)
            if 0 <= since < 15:
                light = self._rng.uniform(0, 2)
        return temp, rh, max(0.0, light)


def format_packet(ts_ms, tx, rssi, status, data, light, temp, humidity) -> str:
    return (
        f"TS:{ts_ms}\tDrop:0\tRX:{RX_ID}\tTX:{tx}\tRSSI:{rssi:.2f}\t{status}\t"
        f"Data: {' '.join(f'{b:02x}' for b in data)}  | "
        f"light: {light} temp: {temp:.2f} humidity: {humidity}"
    )


class Simulator:
    """Generates (simulated epoch seconds, line) pairs in time order."""

    def __init__(self, args: argparse.Namespace, config: Config):
        self._args = args
        self._rng = random.Random(args.seed)
        self._start = resolve_start(args.start, config)
        self._weather = Weather(args.scenario, self._start, config, self._rng)

    def lines(self) -> Iterator[tuple[float, str]]:
        args, rng = self._args, self._rng
        start = self._start.timestamp()
        for line in BANNER:
            yield start, line

        received = good = step = 0
        next_summary = start + SUMMARY_INTERVAL_S
        while args.duration is None or step * args.interval < args.duration:
            ts = start + step * args.interval
            step += 1
            if ts >= next_summary:
                pct = 100 * good / received if received else 0.0
                yield ts, (
                    f"#### Received {received:03d} packets in {int(ts)} seconds. "
                    f"({pct:.2f}% OK) ####"
                )
                received = good = 0
                while next_summary <= ts:
                    next_summary += SUMMARY_INTERVAL_S
            temp, rh, light = self._weather.sample(ts)
            dead = args.scenario == "tag_dies" and ts - start >= TAG_DIES_AFTER_S
            if not dead and rng.random() >= args.drop_rate:
                received += 1
                good += 1
                yield ts, self._good_line(ts, temp, rh, light)
            if rng.random() < args.noise_rate:
                received += 1
                noise_ts = ts + rng.uniform(0.1, 0.9) * args.interval
                yield noise_ts, self._noise_line(noise_ts)

    def _good_line(self, ts, temp, rh, light) -> str:
        rng = self._rng
        rssi = -65 + rng.gauss(0, 2)
        if rng.random() < 0.01:
            rssi -= rng.uniform(5, 12)  # multipath dip
        if self._args.scenario == "rssi_shift":
            local = datetime.fromtimestamp(ts, self._start.tzinfo)
            if self._weather._event_hours(local, 12) >= 0:
                rssi -= 10
        light_int = round(light)
        if self._args.light_cap is not None:
            light_int = min(light_int, self._args.light_cap)
        humidity = round(rh)
        tx = self._args.tx
        data = b"".join(
            (value & 0xFFFF).to_bytes(2, "big")
            for value in (tx, light_int, round(temp * 100), humidity * 100, 0)
        )
        return format_packet(
            round(ts * 1000), tx, round(rssi * 2) / 2, "OK", data,
            light_int, temp, humidity,
        )

    def _noise_line(self, ts) -> str:
        rng = self._rng
        return format_packet(
            round(ts * 1000),
            rng.randrange(1 << 20, 1 << 24),
            rng.randint(-220, -210) / 2,
            "BAD CRC",
            rng.randbytes(20),
            rng.randrange(0, 1024),
            rng.uniform(100, 900),
            rng.randrange(200, 9000),
        )


def resolve_start(value: str | None, config: Config) -> datetime:
    tz = ZoneInfo(config.station.timezone)
    if value is None:
        return datetime.now(tz)
    start = datetime.fromisoformat(value)
    return start.replace(tzinfo=tz) if start.tzinfo is None else start.astimezone(tz)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="simulator", description=__doc__)
    parser.add_argument("--tx", type=int, default=3378)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--start", help="local start time, e.g. 2026-11-20T06:00")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="normal")
    parser.add_argument("--noise-rate", type=float, default=0.02)
    parser.add_argument("--drop-rate", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--duration", type=float, help="stop after N simulated seconds")
    parser.add_argument("--light-cap", type=int, help="saturate light at this value")
    parser.add_argument("--config", help="default: config.toml if it exists")
    args = parser.parse_args(argv)
    if not (args.interval > 0 and args.speed > 0):
        parser.error("--interval and --speed must be positive")
    for name in ("noise_rate", "drop_rate"):
        if not 0 <= getattr(args, name) <= 1:
            parser.error(f"--{name.replace('_', '-')} must be between 0 and 1")
    if args.start is not None:
        try:
            datetime.fromisoformat(args.start)
        except ValueError:
            parser.error("--start must look like 2026-11-20T06:00")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        simulator = Simulator(args, load_config(args.config))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    real_start = time.monotonic()
    sim_start = None
    try:
        for ts, line in simulator.lines():
            if sim_start is None:
                sim_start = ts
            wait = real_start + (ts - sim_start) / args.speed - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            print(line, flush=True)
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        # Stop Python from reporting the closed pipe again at exit.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


if __name__ == "__main__":
    sys.exit(main())
