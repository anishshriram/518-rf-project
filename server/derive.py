"""Derived weather metrics (spec section 15). Pure functions, °C unless noted."""

import math

WATER = (17.62, 243.12)  # Magnus constants, Sonntag 1990
ICE = (22.46, 272.62)
INTERIOR_SURFACE_RESISTANCE = 0.13  # m2K/W
FEELS_LIKE_MIN_F = 80.0


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


def clamp(x: float, low: float, high: float) -> float:
    return min(high, max(low, x))


def _saturation_temperature(temp_c, rh_pct, constants) -> float | None:
    b, c = constants
    if rh_pct <= 0:
        return None
    gamma = math.log(rh_pct / 100) + b * temp_c / (c + temp_c)
    return c * gamma / (b - gamma)


def dew_point(temp_c: float, rh_pct: float) -> float | None:
    return _saturation_temperature(temp_c, rh_pct, WATER)


def frost_point(temp_c: float, rh_pct: float) -> float | None:
    return _saturation_temperature(temp_c, rh_pct, ICE)


def rh_from_dew_point(temp_c: float, dew_point_c: float) -> float:
    b, c = WATER
    return 100 * math.exp(
        b * dew_point_c / (c + dew_point_c) - b * temp_c / (c + temp_c)
    )


def heat_index(temp_c: float, rh_pct: float) -> float:
    """NWS heat index."""
    t, r = c_to_f(temp_c), rh_pct
    simple = 0.5 * (t + 61 + (t - 68) * 1.2 + r * 0.094)
    if (simple + t) / 2 < 80:
        return f_to_c(simple)
    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * r
        - 0.22475541 * t * r
        - 0.00683783 * t * t
        - 0.05481717 * r * r
        + 0.00122874 * t * t * r
        + 0.00085282 * t * r * r
        - 0.00000199 * t * t * r * r
    )
    if r < 13 and 80 <= t <= 112:
        hi -= (13 - r) / 4 * math.sqrt((17 - abs(t - 95)) / 17)
    elif r > 85 and 80 <= t <= 87:
        hi += (r - 85) / 10 * (87 - t) / 5
    return f_to_c(hi)


def absolute_humidity(temp_c: float, rh_pct: float) -> float:
    """Water vapour in g/m3."""
    saturation = 6.112 * math.exp(17.67 * temp_c / (temp_c + 243.5))
    return saturation * rh_pct * 2.1674 / (273.15 + temp_c)


def vapour_pressure_deficit(temp_c: float, rh_pct: float) -> float:
    """VPD in kPa."""
    saturation = 0.6108 * math.exp(17.27 * temp_c / (temp_c + 237.3))
    return saturation * (1 - rh_pct / 100)


def instant(temp_c: float, rh_pct: float) -> dict[str, float | None]:
    """Everything derived from a single reading.

    The heat index is None below 80 °F, where feels-like equals the temperature
    (there is no wind sensor, so no wind chill).
    """
    hot = c_to_f(temp_c) >= FEELS_LIKE_MIN_F
    index = heat_index(temp_c, rh_pct) if hot else None
    return {
        "dew_point_c": dew_point(temp_c, rh_pct),
        "frost_point_c": frost_point(temp_c, rh_pct) if temp_c < 0 else None,
        "heat_index_c": index,
        "feels_like_c": temp_c if index is None else index,
        "abs_humidity_gm3": absolute_humidity(temp_c, rh_pct),
        "vpd_kpa": vapour_pressure_deficit(temp_c, rh_pct),
    }


def heating_degree_days(minute_temps: list[float], base_c: float) -> float:
    return sum(max(0.0, base_c - t) for t in minute_temps) / 1440


def cooling_degree_days(minute_temps: list[float], base_c: float) -> float:
    return sum(max(0.0, t - base_c) for t in minute_temps) / 1440


def growing_degree_days(t_min: float, t_max: float, base_c: float) -> float:
    return max(0.0, (t_max + t_min) / 2 - base_c)


def slope_per_hour(points: list[tuple[int, float]]) -> float | None:
    """Least-squares slope of (epoch ms, value) points, per hour."""
    if len(points) < 2:
        return None
    hours = [(t - points[0][0]) / 3_600_000 for t, _ in points]
    mean_x = sum(hours) / len(hours)
    mean_y = sum(v for _, v in points) / len(points)
    spread = sum((x - mean_x) ** 2 for x in hours)
    if spread == 0:
        return None
    return (
        sum((x - mean_x) * (v - mean_y) for x, (_, v) in zip(hours, points)) / spread
    )


def glass_temperature(temp_in: float, temp_out: float, u_value: float) -> float:
    return temp_in - (temp_in - temp_out) * u_value * INTERIOR_SURFACE_RESISTANCE


def condensation_risk(
    temp_in: float, rh_in: float, temp_out: float, u_value: float
) -> str | None:
    """Indoor window condensation: 'high', 'medium' or 'low' (an estimate)."""
    dew = dew_point(temp_in, rh_in)
    if dew is None:
        return "low"
    margin = glass_temperature(temp_in, temp_out, u_value) - dew
    if margin <= 0:
        return "high"
    return "medium" if margin <= 2 else "low"


def laundry_score(
    temp_c: float, rh_pct: float, clearness: float, rain_likely: bool = False
) -> float:
    """Drying conditions from 0 to 100; pass clearness 0 at night."""
    if rain_likely or rh_pct >= 95:
        return 0.0
    f_vpd = clamp(vapour_pressure_deficit(temp_c, rh_pct) / 1.5, 0, 1)
    f_temp = clamp((temp_c - 5) / 20, 0, 1)
    f_sun = clamp(clearness, 0, 1)
    return 100 * clamp(0.45 * f_vpd + 0.25 * f_temp + 0.30 * f_sun, 0, 1)


def should_open_windows(
    temp_out: float,
    dew_point_out: float,
    setpoint: float,
    dew_point_max: float,
    alert_active: bool,
    local_hour: float,
    sun_is_up: bool,
) -> bool:
    """Alerts are rain, fog or heat; evenings count until 23:00."""
    return (
        setpoint - 4 <= temp_out <= setpoint - 0.5
        and dew_point_out <= dew_point_max
        and not alert_active
        and (sun_is_up or 12 <= local_hour < 23)
    )
