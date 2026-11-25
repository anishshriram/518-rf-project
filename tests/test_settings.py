"""Tests for server.settings and the settings API."""

import pytest
from tests.clients import local_client

from server import db, settings
from server.alerts import ALERT_FROM, channel_enabled
from server.app import create_app
from server.config import ALERT_TYPES, Config, ConfigError
from server.inference import RULES


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.server.db_path = str(tmp_path / "s.db")
    c.server.ingest_token = "very-secret-token"
    return c


@pytest.fixture
def client(config):
    with local_client(create_app(config)) as c:
        yield c


def put(client, body, status=200):
    response = client.put("/api/settings", json=body)
    assert response.status_code == status, response.text
    return response.json()


def test_secrets_are_never_shown(client, monkeypatch):
    monkeypatch.setenv("SMTP_PASS", "sekrit")
    text = client.get("/api/settings").text
    assert "very-secret-token" not in text and "sekrit" not in text
    assert "server" not in client.get("/api/settings").json()["values"]


def test_change_applies_at_once(client, config):
    reply = put(client, {"station": {"name": "Roof"}, "tag": {"allowed_tx_ids": [5]}})
    assert reply["values"]["station"]["name"] == "Roof"
    assert reply["defaults"]["station"]["name"] == "Backyard"
    assert client.get("/api/current").json()["station"]["name"] == "Roof"
    assert config.tag.allowed_tx_ids == [5]


def test_change_survives_a_restart(config, tmp_path):
    with local_client(create_app(config)) as client:
        put(client, {"home": {"indoor_setpoint_c": 19}})
    fresh = Config()
    fresh.server.db_path = config.server.db_path
    with local_client(create_app(fresh)) as client:
        values = client.get("/api/settings").json()
    assert values["values"]["home"]["indoor_setpoint_c"] == 19
    assert values["defaults"]["home"]["indoor_setpoint_c"] == 21


def test_null_restores_the_default(client, config):
    put(client, {"home": {"indoor_setpoint_c": 19}})
    put(client, {"home": {"indoor_setpoint_c": None}})
    assert config.home.indoor_setpoint_c == 21


@pytest.mark.parametrize(
    "body",
    [
        {"server": {"ingest_token": "x"}},
        {"collector": {"batch_max": 5}},
        {"nonsense": {}},
        {"nonsense": {"a": 1}},
        {"station": {"nonsense": 1}},
        {"station": {"latitude": 91}},
        {"station": {"latitude": "40"}},
        {"station": {"timezone": "Mars/Base"}},
        {"station": {"name": "a\r\nBcc: x@y"}},
        {"tag": {"allowed_tx_ids": [1, "2"]}},
        {"tag": {"offline_warning_s": 900}},
        {"tag": {"battery_installed": "yesterday"}},
        {"alerts": {"email_to": "a@b\r\nBcc: c@d"}},
        {"alerts": {"email_enabled": True}},
        {"alerts": {"types": {"nonsense": {"email": False}}}},
        {"alerts": {"types": {"heat": {"email": 0}}}},
        {"alerts": {"types": {"heat": {"pager": False}}}},
        {"station": 5},
        [],
    ],
)
def test_bad_changes_are_refused_completely(client, config, body):
    before = settings.values(config)
    reply = client.put("/api/settings", json=body)
    assert reply.status_code == (200 if body == {"nonsense": {}} else 422)
    assert settings.values(config) == before
    conn = db.open_reader(config.server.db_path)
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    conn.close()


def test_one_bad_value_rejects_the_whole_request(client, config):
    put(client, {"station": {"name": "Roof", "latitude": 500}}, 422)
    assert config.station.name == "Backyard"


def test_infinite_numbers_are_refused(client):
    response = client.put(
        "/api/settings",
        content='{"home": {"indoor_setpoint_c": 1e999}}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_cross_site_changes_are_refused(client):
    response = client.put(
        "/api/settings",
        json={"station": {"name": "x"}},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_invalid_stored_setting_is_ignored(config):
    conn = db.connect(config.server.db_path)
    with conn:
        conn.execute("INSERT INTO settings VALUES ('station.latitude', '500', 1)")
        conn.execute("INSERT INTO settings VALUES ('station.name', '\"Roof\"', 1)")
        conn.execute("INSERT INTO settings VALUES ('server.port', '1', 1)")
    settings.apply_stored(conn, config)
    conn.close()
    assert config.station.name == "Roof"
    assert config.station.latitude == 40.5008
    assert config.server.port == 8000


def test_merge_does_not_touch_the_base():
    base = Config()
    merged = settings.merge(base, {"tag": {"allowed_tx_ids": [9]}})
    assert merged.tag.allowed_tx_ids == [9] and base.tag.allowed_tx_ids == [3378]
    with pytest.raises(ConfigError):
        settings.merge(base, {"server": {}})


def test_alert_types_match_the_rules():
    types = {rule.type for rule in RULES} | {"night_light", "tag_offline"}
    assert types == set(ALERT_TYPES)
    assert ALERT_FROM == 1


def test_channels_default_to_on():
    config = Config()
    config.alerts.types = {"heat": {"email": False}}
    assert channel_enabled(config, "heat", "email") is False
    assert channel_enabled(config, "heat", "browser") is True
    assert channel_enabled(config, "freeze", "email") is True


def test_new_tag_list_applies_to_ingest(client):
    packet = {"ts_ms": 1795100000000, "drop": 0, "rx_id": 1, "tx_id": 5,
              "rssi_dbm": -60, "crc_ok": True, "crc_text": "OK", "data_hex": "",
              "data_len": 0, "light_raw": 5, "temp_raw": 20, "humidity_raw": 50,
              "raw_line": "a"}

    def ingest(raw_line):
        body = {"source": "simulator", "packets": [{**packet, "raw_line": raw_line}]}
        return client.post("/api/ingest", json=body,
                           headers={"X-Ingest-Token": "very-secret-token"}).json()

    assert ingest("a")["foreign"] == 1
    put(client, {"tag": {"allowed_tx_ids": [5]}})
    assert ingest("b")["accepted"] == 1


def test_battery_date_reaches_health(client):
    put(client, {"tag": {"battery_installed": "2026-11-30"}})
    assert client.get("/api/health").json()["tag"]["battery_installed"] == "2026-11-30"
