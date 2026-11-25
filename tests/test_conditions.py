"""Tests for derived values in ingest, /api/current and server.rebuild."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from tests.clients import local_client

from server import aggregates, db, rebuild
from server.app import create_app
from server.config import Config

HEADERS = {"X-Ingest-Token": "change-me"}
MINUTE = 60_000
NY = ZoneInfo("America/New_York")


def ms(*args):
    return round(datetime(*args, tzinfo=NY).timestamp() * 1000)


NOON = ms(2026, 11, 20, 12)


@pytest.fixture
def config(tmp_path):
    c = Config()
    c.server.db_path = str(tmp_path / "c.db")
    return c


@pytest.fixture
def client(config):
    with local_client(create_app(config)) as c:
        yield c


def line(ts, temp, rh, crc="OK", tx=3378):
    return (
        f"TS:{ts}\tDrop:0\tRX:191\tTX:{tx}\tRSSI:-60.00\t{crc}\tData: 00  | "
        f"light: 5 temp: {temp:.2f} humidity: {rh}"
    )


def packet(ts, temp, rh, **kwargs):
    from collector.parser import parse_line

    return dict(vars(parse_line(line(ts, temp, rh, **kwargs))))


def send(client, *packets, source="simulator"):
    for i in range(0, len(packets), 500):
        body = {"source": source, "packets": packets[i : i + 500]}
        r = client.post("/api/ingest", json=body, headers=HEADERS)
        assert r.status_code == 200, r.text


def test_current_has_derived_values(client):
    send(client, packet(NOON, 20, 50))
    data = client.get("/api/current").json()
    assert data["dew_point_c"] == pytest.approx(9.26, abs=0.01)
    assert data["abs_humidity_gm3"] == pytest.approx(8.64, abs=0.02)
    assert data["feels_like_c"] == 20
    assert data["heat_index_c"] is None and data["frost_point_c"] is None
    assert data["recommendations"]["window_condensation_risk"] == "low"


def test_derived_values_reach_the_history(client):
    send(client, packet(NOON, 32, 60))
    data = client.get(
        "/api/history",
        params={"from": NOON, "to": NOON, "metrics": "dew_point_c,heat_index_c"},
    ).json()
    assert data["series"]["heat_index_c"]["avg"][0] == pytest.approx(37.1, abs=0.1)
    assert data["series"]["dew_point_c"]["avg"][0] is not None


def test_rejected_readings_have_no_derived_values(client, config):
    send(client, packet(NOON, 200, 50), packet(NOON + 1000, 20, 0))
    conn = db.open_reader(config.server.db_path)
    rows = conn.execute("SELECT quality, dew_point_c FROM readings").fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [("rejected", None), ("good", None)]


def test_trends(client):
    send(client, *[packet(NOON + i * MINUTE, 15 - i / 60, 50 + i / 30 * 0)
                   for i in range(61)])
    data = client.get("/api/current").json()
    assert data["trends"]["temp_c_per_h"] == pytest.approx(-1, abs=0.05)
    assert data["trends"]["rh_per_h"] == pytest.approx(0, abs=0.01)
    assert data["trends"]["dew_point_c_per_h"] < -0.5
    assert data["trends_60m"]["temp_c_per_h"] == pytest.approx(-1, abs=0.05)


def test_trends_need_enough_data(client):
    send(client, *[packet(NOON + i * MINUTE, 15 - i / 60, 50) for i in range(10)])
    trends = client.get("/api/current").json()["trends"]
    assert set(trends.values()) == {None}


def test_today_uses_the_local_day(client):
    midnight = ms(2026, 11, 20)
    send(
        client,
        packet(midnight - MINUTE, 1, 50),
        packet(midnight, 8, 50),
        packet(midnight + MINUTE, 10, 50),
    )
    today = client.get("/api/current").json()["today"]
    assert (today["temp_min_c"], today["temp_max_c"]) == (8, 10)
    assert today["hdd_so_far"] == pytest.approx((10 + 8) / 1440)
    assert today["gdd_so_far"] == 0


@pytest.mark.parametrize(
    "day", [(2026, 3, 8), (2026, 11, 1), (2026, 7, 4)]  # DST start, DST end, normal
)
def test_local_midnight(day):
    assert aggregates.local_midnight(ms(*day, 15), "America/New_York") == ms(*day)


def test_other_sources_are_ignored(client):
    send(client, packet(NOON, 1, 50), source="replay")
    send(client, packet(NOON + MINUTE, 8, 50))
    assert client.get("/api/current").json()["today"]["temp_min_c"] == 8


def run_rebuild(config):
    conn = db.connect(config.server.db_path)
    try:
        count = rebuild.rebuild(conn, config)
        rows = [dict(r) for r in conn.execute("SELECT * FROM readings ORDER BY id")]
        raw = [dict(r) for r in conn.execute("SELECT * FROM raw_packets ORDER BY id")]
        return count, rows, raw
    finally:
        conn.close()


def strip(rows):
    return [{k: v for k, v in r.items() if k != "id"} for r in rows]


def test_rebuild_reproduces_ingest(client, config):
    send(
        client,
        packet(NOON, 20, 50), packet(NOON + 1000, 60, 50), packet(NOON + 2000, 20.1, 50),
        packet(NOON + 3000, 5, 5, crc="BAD CRC"), packet(NOON + 4000, 5, 5, tx=9),
    )
    conn = db.open_reader(config.server.db_path)
    before = [dict(r) for r in conn.execute("SELECT * FROM readings ORDER BY id")]
    conn.close()
    count, after, raw = run_rebuild(config)
    assert count == 3 and len(raw) == 5
    assert strip(after) == strip(before)


def test_rebuild_fills_old_rows_and_applies_new_config(client, config):
    send(client, packet(NOON, 20, 50), packet(NOON + 1000, 5, 5, tx=9))
    conn = db.connect(config.server.db_path)
    with conn:
        conn.execute("UPDATE readings SET dew_point_c = NULL")
    conn.close()
    config.units.temp_offset_c = 1.0
    config.tag.allowed_tx_ids = [3378, 9]
    _, rows, _ = run_rebuild(config)
    assert [r["temp_c"] for r in rows] == [21.0, 6.0]
    assert rows[0]["dew_point_c"] is not None


def test_rebuild_applies_a_changed_crc_rule(client, config, monkeypatch):
    send(client, packet(NOON, 20, 50, crc="WEIRD"))
    monkeypatch.setattr("collector.parser.is_crc_ok", lambda text: text == "OK")
    count, _, raw = run_rebuild(config)
    assert count == 0
    assert raw[0]["crc_ok"] == 0


def test_rebuild_keeps_clock_verdicts(client, config):
    send(client, packet(NOON, 20, 50), source="hardware")  # far from the clock
    _, rows, _ = run_rebuild(config)
    assert (rows[0]["quality"], rows[0]["reject_reason"]) == ("suspect", "clock_skew")


def test_change_over_the_past_hour(client):
    send(client, *[packet(NOON + i * MINUTE, 15 - i / 60, 50) for i in range(40)])
    assert client.get("/api/current").json()["change_1h"]["temp_c"] is None
    send(client, *[packet(NOON + i * MINUTE, 15 - i / 60, 50) for i in range(40, 70)])
    change = client.get("/api/current").json()["change_1h"]
    assert change["temp_c"] == pytest.approx(-1, abs=0.05)
    assert change["rh_pct"] == pytest.approx(0)
