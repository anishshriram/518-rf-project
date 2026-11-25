"""Viewers elsewhere may look but not change anything."""

import pytest

from server.app import create_app
from server.config import Config
from tests.clients import local_client, remote_client

VIEWERS = {
    "wifi": dict(),
    "tunnel": dict(host="x.ts.net", ip="127.0.0.1"),
    "tunnel_headers": dict(
        host="127.0.0.1:8000", ip="127.0.0.1", headers={"X-Forwarded-For": "127.0.0.1"}
    ),
    "forged_host": dict(host="127.0.0.1:8000", ip="203.0.113.7"),
    "rebinding": dict(host="evil.example:8000", ip="127.0.0.1"),
}


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.server.db_path = str(tmp_path / "a.db")
    return c


@pytest.fixture(params=VIEWERS)
def viewer(request, config):
    with remote_client(create_app(config), **VIEWERS[request.param]) as c:
        yield c


def test_viewer_can_look(viewer):
    for path in ("/", "/history.html", "/api/history", "/api/events", "/api/daily",
                 "/api/health", "/api/export.csv"):
        assert viewer.get(path).status_code == 200, path
    body = viewer.get("/api/current").json()
    assert body["can_edit"] is False
    assert body["station"]["latitude"] is body["station"]["longitude"] is None


def test_viewer_cannot_change_anything(viewer, config):
    assert viewer.get("/api/settings").status_code == 403
    reply = viewer.put("/api/settings", json={"station": {"name": "Hacked"}})
    assert reply.status_code == 403
    assert config.station.name == "Backyard"
    assert viewer.post("/api/events/1/ack").status_code == 403
    assert viewer.post("/api/alerts/test").status_code == 403


def test_default_token_is_refused_from_elsewhere(viewer, config):
    body = {"source": "hardware", "packets": []}
    headers = {"X-Ingest-Token": "change-me"}
    assert viewer.post("/api/ingest", json=body, headers=headers).status_code == 403
    config.server.ingest_token = "private"
    headers = {"X-Ingest-Token": "private"}
    assert viewer.post("/api/ingest", json=body, headers=headers).status_code == 200


@pytest.mark.parametrize("host", ["localhost:8000", "127.0.0.1:8000", "[::1]:8000"])
def test_station_computer_can_edit(config, host):
    with local_client(create_app(config)) as client:
        body = client.get("/api/current", headers={"Host": host}).json()
        assert body["can_edit"] is True
        assert body["station"]["latitude"] == 40.5008


def test_too_many_viewers(config, monkeypatch):
    monkeypatch.setattr("server.app.STREAMS_MAX", 0)
    with local_client(create_app(config)) as client:
        assert client.get("/api/stream").status_code == 503
