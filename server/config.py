"""Load and validate config.toml (spec section 8).

Every key has a default here; the file only overrides. Used by both the server
and the collector.
"""

import math
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_PATH = "config.toml"


class ConfigError(Exception):
    """The configuration file is missing or invalid."""


@dataclass
class StationConfig:
    name: str = "Backyard"
    latitude: float = 40.5008
    longitude: float = -74.4474
    timezone: str = "America/New_York"
    elevation_m: float = 30.0


@dataclass
class TagConfig:
    allowed_tx_ids: list = field(default_factory=lambda: [3378])
    expected_beacon_interval_s: float = 1.0
    offline_warning_s: float = 30.0
    offline_critical_s: float = 600.0


@dataclass
class UnitsConfig:
    display_temperature: str = "C"
    temp_input_unit: str = "C"
    humidity_scale: float = 1.0
    temp_offset_c: float = 0.0
    humidity_offset_pct: float = 0.0


@dataclass
class ValidationConfig:
    temp_min_c: float = -40.0
    temp_max_c: float = 85.0
    rh_min: float = 0.0
    rh_max: float = 100.0
    max_temp_step_c_per_s: float = 2.0
    max_rh_step_per_s: float = 10.0
    hampel_window: int = 7
    hampel_sigma: float = 3.0


@dataclass
class SmoothingConfig:
    ema_alpha: float = 0.2
    aggregate_minute: bool = True


@dataclass
class LightConfig:
    auto_calibrate: bool = True
    calibration_days: int = 7
    saturation_detect: bool = True
    night_light_sigma: float = 5.0


@dataclass
class HomeConfig:
    indoor_setpoint_c: float = 21.0
    indoor_rh_assumed: float = 40.0
    window_u_value: float = 2.8


@dataclass
class ThresholdsConfig:
    freeze_c: float = 0.0
    frost_watch_c: float = 3.0
    heat_caution_hi_c: float = 26.7
    heat_extreme_caution_hi_c: float = 32.2
    heat_danger_hi_c: float = 39.4
    heat_extreme_danger_hi_c: float = 51.7
    fog_spread_c: float = 1.5
    fog_rh: float = 95.0
    open_window_dewpoint_max_c: float = 16.0
    degree_day_base_c: float = 18.0
    gdd_base_c: float = 10.0


@dataclass
class AlertsConfig:
    cooldown_minutes: float = 60.0
    email_enabled: bool = False
    email_to: str = ""
    browser_notifications: bool = True


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    ingest_token: str = "change-me"
    db_path: str = "data/weather.db"
    raw_retention_days: int = 90


@dataclass
class CollectorConfig:
    server_url: str = "http://127.0.0.1:8000"
    batch_max: int = 20
    batch_interval_s: float = 1.0
    spool_path: str = "data/spool.jsonl"
    forward_bad_crc: bool = True


@dataclass
class Config:
    station: StationConfig = field(default_factory=StationConfig)
    tag: TagConfig = field(default_factory=TagConfig)
    units: UnitsConfig = field(default_factory=UnitsConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    smoothing: SmoothingConfig = field(default_factory=SmoothingConfig)
    light: LightConfig = field(default_factory=LightConfig)
    home: HomeConfig = field(default_factory=HomeConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)


def load_config(path: str | None = None) -> Config:
    """Load configuration.

    An explicit path (argument or PIP_CONFIG) must exist. Otherwise config.toml
    is used if present, and the defaults if not.
    """
    explicit = path or os.environ.get("PIP_CONFIG")
    file = Path(explicit or DEFAULT_PATH)
    if not file.exists():
        if explicit:
            raise ConfigError(f"config file not found: {file}")
        return _apply_environment(Config())
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ConfigError(f"cannot read {file}: {exc}") from exc
    return _apply_environment(parse_config(data))


def _apply_environment(config: Config) -> Config:
    """Let scripts choose data files without editing the config file."""
    config.server.db_path = os.environ.get("PIP_DB_PATH", config.server.db_path)
    config.collector.spool_path = os.environ.get(
        "PIP_SPOOL_PATH", config.collector.spool_path
    )
    return config


def parse_config(data: dict) -> Config:
    """Build a Config from parsed TOML, reporting every problem at once."""
    config = Config()
    errors: list[str] = []
    for section_name, values in data.items():
        section = getattr(config, section_name, None)
        if section_name not in {f.name for f in fields(Config)}:
            errors.append(f"unknown section [{section_name}]")
        elif not isinstance(values, dict):
            errors.append(f"[{section_name}] must be a table")
        else:
            _apply_section(section_name, section, values, errors)
    if not errors:
        _check_ranges(config, errors)
    if errors:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(errors))
    return config


def _apply_section(name: str, section, values: dict, errors: list[str]) -> None:
    types = {f.name: f.type for f in fields(section)}
    for key, value in values.items():
        label = f"{name}.{key}"
        if key not in types:
            errors.append(f"unknown key {label}")
            continue
        expected = types[key]
        if expected is float and type(value) is int:
            value = float(value)
        if type(value) is not expected:
            errors.append(
                f"{label} must be {expected.__name__}, got {type(value).__name__}"
            )
        elif expected is float and not math.isfinite(value):
            errors.append(f"{label} must be finite")
        else:
            setattr(section, key, value)


def _check_ranges(c: Config, errors: list[str]) -> None:
    def check(ok: bool, message: str) -> None:
        if not ok:
            errors.append(message)

    check(-90 <= c.station.latitude <= 90, "station.latitude must be -90..90")
    check(-180 <= c.station.longitude <= 180, "station.longitude must be -180..180")
    try:
        ZoneInfo(c.station.timezone)
    except Exception:
        errors.append(f"station.timezone is not valid: {c.station.timezone!r}")

    check(
        all(type(i) is int and i >= 0 for i in c.tag.allowed_tx_ids),
        "tag.allowed_tx_ids must be a list of non-negative integers",
    )
    check(
        c.tag.offline_critical_s >= c.tag.offline_warning_s,
        "tag.offline_critical_s must be >= tag.offline_warning_s",
    )
    for key in ("display_temperature", "temp_input_unit"):
        check(getattr(c.units, key) in ("C", "F"), f'units.{key} must be "C" or "F"')

    check(
        c.validation.temp_min_c < c.validation.temp_max_c,
        "validation.temp_min_c must be < temp_max_c",
    )
    check(
        c.validation.rh_min < c.validation.rh_max,
        "validation.rh_min must be < rh_max",
    )
    check(c.validation.hampel_window >= 3, "validation.hampel_window must be >= 3")
    check(0 < c.smoothing.ema_alpha <= 1, "smoothing.ema_alpha must be in (0, 1]")
    check(0 <= c.home.indoor_rh_assumed <= 100, "home.indoor_rh_assumed must be 0..100")
    check(1 <= c.server.port <= 65535, "server.port must be 1..65535")
    check(bool(c.server.ingest_token), "server.ingest_token must not be empty")
    check(c.server.ingest_token.isascii(), "server.ingest_token must be ASCII")
    check(1 <= c.collector.batch_max <= 1000, "collector.batch_max must be 1..1000")
    check(
        c.collector.server_url.startswith(("http://", "https://")),
        "collector.server_url must start with http:// or https://",
    )

    for label, value in (
        ("tag.expected_beacon_interval_s", c.tag.expected_beacon_interval_s),
        ("tag.offline_warning_s", c.tag.offline_warning_s),
        ("units.humidity_scale", c.units.humidity_scale),
        ("validation.max_temp_step_c_per_s", c.validation.max_temp_step_c_per_s),
        ("validation.max_rh_step_per_s", c.validation.max_rh_step_per_s),
        ("validation.hampel_sigma", c.validation.hampel_sigma),
        ("light.calibration_days", c.light.calibration_days),
        ("light.night_light_sigma", c.light.night_light_sigma),
        ("home.window_u_value", c.home.window_u_value),
        ("server.raw_retention_days", c.server.raw_retention_days),
        ("collector.batch_interval_s", c.collector.batch_interval_s),
    ):
        check(value > 0, f"{label} must be > 0")
    check(c.alerts.cooldown_minutes >= 0, "alerts.cooldown_minutes must be >= 0")
