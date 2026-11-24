"""Tests for server.config."""

import pytest

from server.config import Config, ConfigError, load_config, parse_config


def test_example_file_matches_defaults():
    assert load_config("config.example.toml") == Config()


def test_file_overrides_defaults(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("[tag]\nallowed_tx_ids = [7]\n[station]\nelevation_m = 5\n")
    config = load_config(str(path))
    assert config.tag.allowed_tx_ids == [7]
    assert config.station.elevation_m == 5.0
    assert config.server.port == 8000


def test_explicit_missing_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "none.toml"))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"servr": {}}, "unknown section"),
        ({"server": {"prt": 1}}, "unknown key server.prt"),
        ({"server": {"port": "8000"}}, "server.port must be int"),
        ({"server": {"port": True}}, "server.port must be int"),
        ({"server": {"port": 70000}}, "server.port"),
        ({"server": {"ingest_token": ""}}, "ingest_token"),
        ({"station": {"latitude": 91.0}}, "latitude"),
        ({"station": {"latitude": float("nan")}}, "finite"),
        ({"station": {"timezone": "Mars/Base"}}, "timezone"),
        ({"units": {"temp_input_unit": "K"}}, "temp_input_unit"),
        ({"tag": {"allowed_tx_ids": ["3378"]}}, "allowed_tx_ids"),
    ],
)
def test_invalid_values_are_reported(data, message):
    with pytest.raises(ConfigError, match=message):
        parse_config(data)
