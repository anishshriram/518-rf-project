"""Tests for alerts, email and the event API."""

import json
import smtplib
import threading
from types import SimpleNamespace

import pytest
from tests.clients import local_client

from server import db, notify_email
from server.alerts import AlertManager, query_events
from server.app import create_app
from server.config import Config
from server.inference import Result

MINUTE = 60_000
T0 = 1795104000000
HEADERS = {"X-Ingest-Token": "change-me"}


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.server.db_path = str(tmp_path / "a.db")
    return c


@pytest.fixture
def conn(config):
    connection = db.connect(config.server.db_path)
    yield connection
    connection.close()


@pytest.fixture
def made(conn, config):
    out = SimpleNamespace(published=[], emails=[], clock=T0)
    out.manager = AlertManager(
        conn, threading.Lock(), config,
        lambda name, data: out.published.append((name, dict(data))),
        out.emails.append, lambda: out.clock,
    )
    return out


def result(severity="warning"):
    return Result(True, "Title", "Because 1.", severity, "high", {"x": 1})


def test_open_and_close(made, conn):
    made.manager.open("simulator", "freeze", result(), T0, T0)
    made.manager.open("simulator", "freeze", result(), T0, T0)  # already active
    made.manager.close("simulator", "freeze", T0 + MINUTE)
    (event,) = query_events(conn, "simulator")
    assert event["end_ms"] == T0 + MINUTE
    assert event["details"] == {"x": 1}
    assert [n for n, _ in made.published] == ["alert", "alert_cleared"]


def test_info_is_not_an_alert(made):
    made.manager.open("simulator", "fog_dew", result("info"), T0, T0)
    made.manager.close("simulator", "fog_dew", T0)
    assert [n for n, _ in made.published] == ["inference", "inference"]
    assert made.emails == []


@pytest.mark.parametrize(("minutes", "expected"), [(30, 1), (61, 2)])
def test_cooldown(made, minutes, expected):
    made.manager.open("simulator", "freeze", result(), T0, T0)
    made.manager.close("simulator", "freeze", T0)
    later = T0 + minutes * MINUTE
    made.manager.open("simulator", "freeze", result(), later, later)
    assert len(made.emails) == expected
    assert [n for n, _ in made.published].count("alert") == 2  # always on the dashboard


def test_escalation_notifies_again(made, conn):
    made.manager.open("simulator", "heat", result("watch"), T0, T0)
    made.manager.acknowledge(query_events(conn, "simulator")[0]["id"])
    made.manager.escalate("simulator", "heat", result("critical"), T0)
    made.manager.escalate("simulator", "heat", result("watch"), T0)
    (event,) = query_events(conn, "simulator")
    assert event["severity"] == "critical" and event["acknowledged"] is False
    assert len(made.emails) == 2


def test_old_hardware_events_do_not_notify(made):
    made.clock = T0 + 2 * 3_600_000
    made.manager.open("hardware", "freeze", result(), T0, T0)
    made.manager.open("simulator", "freeze", result(), T0, T0)
    assert [e["source"] for e in made.emails] == ["simulator"]


def test_active_events_survive_restart(made, conn, config):
    made.manager.open("simulator", "freeze", result(), T0, T0)
    again = AlertManager(conn, threading.Lock(), config)
    assert again.active_types("simulator") == {"freeze"}
    assert again.active_types("hardware") == set()


def test_query_filters(made, conn):
    made.manager.open("simulator", "a", result(), T0, T0)
    made.manager.close("simulator", "a", T0 + 10)
    made.manager.open("simulator", "b", result(), T0 + 100, T0)
    made.manager.open("hardware", "c", result(), T0, T0)

    def types(**kw):
        return [e["type"] for e in query_events(conn, "simulator", **kw)]

    assert types() == ["b", "a"]
    assert types(active=True) == ["b"]
    assert types(active=False) == ["a"]
    assert types(start=T0 + 50) == ["b"]
    assert types(end=T0 + 50) == ["a"]
    assert types(kind="a") == ["a"]


# Email


class FakeSMTP:
    sent = []
    tls = True

    def __init__(self, host, port, **kwargs):
        self.calls = [("connect", host, port)]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def ehlo_or_helo_if_needed(self):
        pass

    def has_extn(self, name):
        return self.tls

    def starttls(self, context):
        if not self.tls:
            raise smtplib.SMTPNotSupportedError("no TLS")
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, message):
        FakeSMTP.sent.append((message, self.calls))


@pytest.fixture
def smtp(monkeypatch, config):
    FakeSMTP.sent, FakeSMTP.tls = [], True
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    for name, value in {
        "SMTP_HOST": "mail.test", "SMTP_PORT": "587", "SMTP_USER": "u",
        "SMTP_PASS": "p", "SMTP_FROM": "me@test",
    }.items():
        monkeypatch.setenv(name, value)
    config.alerts.email_enabled = True
    config.alerts.email_to = "you@test"
    return FakeSMTP


EVENT = {"title": "Freeze", "severity": "warning", "explanation": "It is -1.0 °C.",
         "start_ms": T0, "source": "simulator"}
READING = {"temp_c": -1.0, "rh_pct": 80.0, "light_raw": 0.0}


def test_email_content(config, smtp):
    message = notify_email.build_message(config, EVENT, READING)
    assert message["Subject"] == "[PIP Weather] [SIMULATED] Freeze — 2026-11-19 11:00"
    body = message.get_content()
    assert "It is -1.0 °C." in body and "http://127.0.0.1:8000/" in body
    hardware = notify_email.build_message(config, {**EVENT, "source": "hardware"}, None)
    assert "SIMULATED" not in hardware["Subject"]


def test_email_uses_tls_before_login(config, smtp):
    notify_email.send(notify_email.build_message(config, EVENT, READING))
    assert smtp.sent[0][1] == [("connect", "mail.test", 587), "starttls", ("login", "u")]


def test_password_is_never_sent_without_tls(config, smtp):
    FakeSMTP.tls = False
    with pytest.raises(smtplib.SMTPNotSupportedError):
        notify_email.send(notify_email.build_message(config, EVENT, READING))
    assert smtp.sent == []


def test_implicit_tls(config, smtp, monkeypatch):
    monkeypatch.setenv("SMTP_PORT", "465")
    notify_email.send(notify_email.build_message(config, EVENT, READING))
    assert "starttls" not in smtp.sent[0][1]


def test_sender_thread_survives_errors(config, smtp):
    done = threading.Event()
    sender = notify_email.EmailSender(config, lambda: READING, lambda _: done.set())
    sender.put({**EVENT, "id": 1, "start_ms": "broken"})
    sender.put({**EVENT, "id": 2})
    assert done.wait(5)
    assert len(smtp.sent) == 1


# API


def send(client, minutes, temp):
    packets = [
        {"ts_ms": T0 + i * MINUTE, "drop": 0, "rx_id": 1, "tx_id": 3378,
         "rssi_dbm": -60, "crc_ok": True, "crc_text": "OK", "data_hex": "",
         "data_len": 0, "light_raw": 5, "temp_raw": temp + i % 3 / 10,
         "humidity_raw": 50, "raw_line": f"{i}"}
        for i in range(minutes)
    ]
    body = {"source": "simulator", "packets": packets}
    assert client.post("/api/ingest", json=body, headers=HEADERS).status_code == 200


def wait_for(get, timeout=10):
    import time

    end = time.monotonic() + timeout
    while not (value := get()):
        assert time.monotonic() < end, "timed out"
        time.sleep(0.05)
    return value


def test_events_end_to_end(config, smtp):
    with local_client(create_app(config)) as client:
        assert client.get("/api/events").json() == []
        send(client, 20, -5)
        (event,) = wait_for(lambda: client.get("/api/events?type=freeze").json())
        assert event["severity"] == "warning" and event["source"] == "simulator"
        assert client.post(f"/api/events/{event['id']}/ack").status_code == 200
        (event,) = client.get("/api/events?type=freeze&active=true").json()
        assert event["acknowledged"] is True
        wait_for(lambda: smtp.sent)
        wait_for(lambda: client.get("/api/events?type=freeze").json()[0]["notified_email"])
        data = client.get("/api/current").json()
        assert data["recommendations"]["open_windows"] is False


@pytest.mark.parametrize("event_id", ["999", "-1", str(2**70)])
def test_ack_unknown_event(config, event_id):
    with local_client(create_app(config)) as client:
        assert client.post(f"/api/events/{event_id}/ack").status_code == 404


def test_cross_site_requests_are_refused(config):
    with local_client(create_app(config)) as client:
        headers = {"Sec-Fetch-Site": "cross-site"}
        assert client.post("/api/alerts/test", headers=headers).status_code == 403
        assert client.post("/api/events/1/ack", headers=headers).status_code == 403


def test_test_alert(config, smtp):
    with local_client(create_app(config)) as client:
        assert client.post("/api/alerts/test").json() == {
            "dashboard": "sent", "browser": "sent", "email": "sent",
        }
    assert "Test alert" in smtp.sent[0][0]["Subject"]


def test_test_alert_reports_problems(config, smtp, monkeypatch):
    monkeypatch.delenv("SMTP_HOST")
    with local_client(create_app(config)) as client:
        reply = client.post("/api/alerts/test").json()
        assert reply["email"] == "not configured: SMTP_HOST"
    config.alerts.email_enabled = False
    with local_client(create_app(config)) as client:
        assert client.post("/api/alerts/test").json()["email"] == "disabled"


def test_password_does_not_leak(config, smtp, monkeypatch):
    monkeypatch.setenv("SMTP_PASS", "sekrit")
    with local_client(create_app(config)) as client:
        texts = [client.get("/api/current").text, client.post("/api/alerts/test").text]
    assert "sekrit" not in json.dumps(texts)


def test_channels_per_type(made, config):
    config.alerts.types = {"freeze": {"email": False, "browser": False}}
    made.manager.open("simulator", "freeze", result(), T0, T0)
    made.manager.open("simulator", "heat", result(), T0, T0)
    assert [e["type"] for e in made.emails] == ["heat"]
    assert [d["notify"] for _, d in made.published] == [False, True]


def test_banner_switch(config, smtp):
    config.alerts.types = {"freeze": {"dashboard": False}}
    with local_client(create_app(config)) as client:
        send(client, 20, -5)
        (event,) = wait_for(lambda: client.get("/api/events?type=freeze").json())
        assert event["banner"] is False
