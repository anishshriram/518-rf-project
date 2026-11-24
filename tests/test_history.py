"""Tests for server.history and the read endpoints."""

import time

import pytest
from fastapi.testclient import TestClient

from server import history
from server.app import create_app
from server.config import Config

BASE = 1795100400000  # a whole hour
HEADERS = {"X-Ingest-Token": "change-me"}


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.server.db_path = str(tmp_path / "h.db")
    return c


@pytest.fixture
def client(config):
    with TestClient(create_app(config)) as c:
        yield c


def send(client, *packets, source="simulator"):
    body = {
        "source": source,
        "packets": [
            {
                "ts_ms": ts, "drop": 0, "rx_id": 191, "tx_id": 3378,
                "rssi_dbm": -60.0, "crc_ok": True, "crc_text": "OK", "data_hex": "",
                "data_len": 0, "light_raw": 100, "temp_raw": temp,
                "humidity_raw": rh, "raw_line": f"{ts} {temp} {rh}",
            }
            for ts, temp, rh in packets
        ],
    }
    assert client.post("/api/ingest", json=body, headers=HEADERS).status_code == 200


def get(client, **params):
    response = client.get("/api/history", params=params)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("span", "expected"),
    [
        (15 * history.MINUTE, "raw"),
        (history.HOUR, "5s"),
        (6 * history.HOUR, "1m"),
        (history.DAY, "1m"),
        (7 * history.DAY, "15m"),
        (30 * history.DAY, "1h"),
        (365 * history.DAY, "1d"),
    ],
)
def test_auto_resolution(span, expected):
    assert history.auto_resolution(span) == expected


def test_raw_series(client):
    send(client, *[(BASE + i * 1000, 20 + i / 10, 45) for i in range(5)])
    data = get(client, **{"from": BASE, "to": BASE + 10_000})
    assert data["resolution"] == "raw"
    assert data["series"]["temp_c"]["t"] == [BASE + i * 1000 for i in range(5)]
    assert data["series"]["temp_c"]["avg"] == pytest.approx([20, 20.1, 20.2, 20.3, 20.4])
    assert data["gaps"] == []
    assert data["last_id"] == 5


def test_bucketed_series(client):
    send(client, *[(BASE + i * 1000, 20 + i / 10, 45) for i in range(10)])
    data = get(client, **{"from": BASE + 1, "to": BASE + 9999, "resolution": "5s"})
    series = data["series"]["temp_c"]
    assert series["t"] == [BASE, BASE + 5000]
    assert series["avg"] == pytest.approx([20.2, 20.7])
    assert series["min"] == pytest.approx([20.0, 20.5])
    assert series["max"] == pytest.approx([20.4, 20.9])
    assert data["n"] == [5, 5]


def test_gap_is_reported(client):
    times = [0, 1, 2, 3, 20, 21, 22]
    send(client, *[(BASE + t * 1000, 20, 45) for t in times])
    data = get(client, **{"from": BASE, "to": BASE + 30_000})
    assert data["gaps"] == [[BASE + 3000, BASE + 20_000]]


def test_slow_beacons_are_not_gaps(client):
    send(client, *[(BASE + i * 10_000, 20, 45) for i in range(10)])
    for resolution in ("raw", "5s"):
        data = get(
            client, **{"from": BASE, "to": BASE + 100_000, "resolution": resolution}
        )
        assert data["gaps"] == []


def test_rejected_is_hidden_and_suspect_is_marked(client):
    send(
        client,
        (BASE, 20, 99), (BASE + 1000, 60, 99), (BASE + 2000, 20, 101),
    )
    data = get(client, **{"from": BASE, "to": BASE + 5000})
    assert data["series"]["temp_c"]["t"] == [BASE, BASE + 2000]
    assert data["suspect"] == [False, True]
    data = get(client, **{"from": BASE, "to": BASE + 5000, "resolution": "5s"})
    assert data["n"] == [1]


def test_sources_are_never_mixed(client):
    send(client, (BASE, 20, 45), source="simulator")
    send(client, (BASE + 1000, 5, 45), source="replay")
    data = get(client, **{"from": BASE, "to": BASE + 5000})
    assert data["source"] == "replay"
    assert data["series"]["temp_c"]["avg"] == [5]


def test_default_range_reaches_simulated_future(client):
    future = int(time.time() * 1000) + 10 * history.DAY
    send(client, (future, 20, 45))
    assert get(client)["series"]["temp_c"]["t"] == [future]


def test_empty_database(client):
    assert get(client)["series"]["temp_c"] == {"t": [], "avg": []}
    data = client.get("/api/current").json()
    assert data["status"] == "never_seen"
    assert data["mode"] is None


@pytest.mark.parametrize(
    "params",
    [
        {"metrics": "temp_c; DROP TABLE readings"},
        {"metrics": ""},
        {"resolution": "2s"},
        {"from": 10, "to": 5},
        {"from": 0, "to": BASE, "resolution": "raw"},
        {"from": -1},
    ],
)
def test_bad_history_requests(client, params):
    assert client.get("/api/history", params=params).status_code == 422


def test_current(client):
    send(client, (BASE, 20, 45))
    data = client.get("/api/current").json()
    assert (data["mode"], data["status"]) == ("simulator", "online")
    assert (data["temp_c"], data["rh_pct"], data["light_raw"]) == (20, 45, 100)
    assert "ingest_token" not in str(data)


@pytest.mark.parametrize(
    ("age_s", "status"),
    [(1, "online"), (60, "offline_warning"), (1200, "offline_critical")],
)
def test_hardware_age(client, age_s, status):
    send(client, (int(time.time() * 1000) - age_s * 1000, 20, 45), source="hardware")
    assert client.get("/api/current").json()["status"] == status


def test_dashboard_files_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "PIP Weather" in page.text
    for path in ("js/app.js", "css/styles.css", "vendor/chart.umd.js"):
        assert client.get(f"/{path}").status_code == 200


def test_all_endpoints_are_documented(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/api/ingest", "/api/current", "/api/history", "/api/stream"} <= set(paths)
