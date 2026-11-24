"""Tests for server.derive, using the values in spec section 15."""

import pytest

from server import derive as d


def close(value, tolerance=0.1):
    return pytest.approx(value, abs=tolerance)


def test_dew_point():
    assert d.dew_point(20, 50) == close(9.26, 0.01)
    assert d.dew_point(0, 100) == close(0.0, 0.001)
    assert d.dew_point(20, 0) is None
    assert d.dew_point(20, -1) is None


@pytest.mark.parametrize(("temp", "rh"), [(-10, 80), (5, 30), (30, 95)])
def test_dew_point_inverse(temp, rh):
    assert d.rh_from_dew_point(temp, d.dew_point(temp, rh)) == close(rh, 0.001)


def test_frost_point_is_above_dew_point_below_freezing():
    assert d.frost_point(0, 100) == close(0.0, 0.001)
    assert d.dew_point(-10, 80) < d.frost_point(-10, 80) < -10


def test_heat_index():
    assert 105 <= d.c_to_f(d.heat_index(d.f_to_c(90), 70)) <= 106.5
    assert d.heat_index(32, 60) == close(37.1)
    assert d.heat_index(20, 50) == close(19.4, 0.2)  # simple formula


def test_heat_index_adjustments():
    # NWS adjustments: lower when very dry, higher when very humid.
    assert d.c_to_f(d.heat_index(d.f_to_c(95), 10)) == close(89.7, 0.5)
    assert d.c_to_f(d.heat_index(d.f_to_c(84), 90)) == close(98.3, 0.5)


def test_absolute_humidity():
    assert d.absolute_humidity(20, 50) == close(8.64, 0.02)


def test_vpd():
    assert d.vapour_pressure_deficit(25, 50) == close(1.58, 0.01)
    assert d.vapour_pressure_deficit(25, 0) == close(3.17, 0.01)


def test_instant_cold_mild_and_hot():
    cold, mild, hot = d.instant(-5, 80), d.instant(20, 50), d.instant(32, 60)
    assert cold["frost_point_c"] is not None
    assert mild["frost_point_c"] is None
    assert mild["heat_index_c"] is None and mild["feels_like_c"] == 20
    assert hot["heat_index_c"] == hot["feels_like_c"] == close(37.1)
    assert d.instant(20, 0)["dew_point_c"] is None


def test_degree_days():
    assert d.heating_degree_days([8.0] * 1440, 18) == close(10)
    assert d.heating_degree_days([8.0] * 720 + [20.0] * 720, 18) == close(5)
    assert d.cooling_degree_days([20.0] * 1440, 18) == close(2)
    assert d.growing_degree_days(10, 20, 10) == 5
    assert d.growing_degree_days(0, 10, 10) == 0


def test_slope():
    minute = 60_000
    assert d.slope_per_hour([(i * minute, 5 - i / 60) for i in range(30)]) == close(-1)
    assert d.slope_per_hour([(0, 1.0)]) is None
    assert d.slope_per_hour([(0, 1.0), (0, 2.0)]) is None


def test_condensation():
    assert d.dew_point(21, 40) == close(6.9)
    assert d.glass_temperature(21, -5, 2.8) == close(11.5)
    assert d.glass_temperature(21, -5, 5.8) == close(1.4)
    assert d.condensation_risk(21, 40, -5, 2.8) == "low"
    assert d.condensation_risk(21, 40, -5, 5.8) == "high"
    assert d.condensation_risk(21, 40, 4, 5.8) == "medium"


def test_laundry():
    assert d.laundry_score(30, 20, 1) == 100
    assert d.laundry_score(20, 96, 1) == 0
    assert d.laundry_score(30, 20, 1, rain_likely=True) == 0
    assert d.laundry_score(0, 90, 0) < 10


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, True),
        ({"temp_out": 16.9}, False),
        ({"temp_out": 20.6}, False),
        ({"dew_point_out": 16.1}, False),
        ({"alert_active": True}, False),
        ({"sun_is_up": False, "local_hour": 22.9}, True),
        ({"sun_is_up": False, "local_hour": 23}, False),
        ({"sun_is_up": False, "local_hour": 2}, False),
    ],
)
def test_open_windows(changes, expected):
    values = dict(
        temp_out=17.8, dew_point_out=9, setpoint=21, dew_point_max=16,
        alert_active=False, local_hour=14, sun_is_up=True,
    )
    assert d.should_open_windows(**{**values, **changes}) is expected
