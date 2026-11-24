"""Integration tests: HTTP API, and simulator -> collector -> server."""

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.config import Config

HEADERS = {"X-Ingest-Token": "change-me"}


def packet(**changes):
    return {
        "ts_ms": 1795100000000, "drop": 0, "rx_id": 191, "tx_id": 3378,
        "rssi_dbm": -62.5, "crc_ok": True, "crc_text": "OK", "data_hex": "0d",
        "data_len": 1, "light_raw": 140, "temp_raw": 21.35, "humidity_raw": 45,
        "raw_line": "TS:1795100000000\t...", **changes,
    }


@pytest.fixture
def client(tmp_path):
    config = Config()
    config.server.db_path = str(tmp_path / "api.db")
    with TestClient(create_app(config)) as c:
        yield c


def test_ingest(client):
    body = {
        "source": "simulator",
        "packets": [packet()],
        "summaries": [{"packets": 60, "ok_pct": 98.3, "raw_line": "#### ..."}],
    }
    response = client.post("/api/ingest", json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "rejected": 0, "bad_crc": 0, "foreign": 0}


@pytest.mark.parametrize("headers", [{}, {"X-Ingest-Token": "wrong"}])
def test_bad_token(client, headers):
    body = {"source": "simulator", "packets": []}
    assert client.post("/api/ingest", json=body, headers=headers).status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"source": "other", "packets": []},
        {"source": "simulator", "packets": [{"ts_ms": 1}]},
        {"source": "simulator", "packets": [packet(tx_id=2**63)]},
    ],
)
def test_schema_error(client, body):
    response = client.post("/api/ingest", json=body, headers=HEADERS)
    assert response.status_code == 422


def test_docs_list_the_endpoint(client):
    assert "/api/ingest" in client.get("/openapi.json").json()["paths"]
    assert client.get("/docs").status_code == 200


# End to end, with real processes


@pytest.fixture
def station(tmp_path):
    """Config file plus helpers to start and stop the real server."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = tmp_path / "config.toml"
    config.write_text(
        f'[server]\nport = {port}\ndb_path = "{tmp_path}/e2e.db"\n'
        f'[collector]\nserver_url = "http://127.0.0.1:{port}"\n'
        f'spool_path = "{tmp_path}/spool.jsonl"\n'
    )
    env = {**os.environ, "PIP_CONFIG": str(config)}
    processes = []

    class Station:
        db_path = tmp_path / "e2e.db"
        spool_path = tmp_path / "spool.jsonl"
        url = f"http://127.0.0.1:{port}"

        def spawn(self, *args, **kwargs):
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", *args], env=env, **kwargs
            )
            processes.append(process)
            return process

        def start_server(self):
            process = self.spawn("server.app", stderr=subprocess.DEVNULL)
            self.wait(self._up, 20)
            return process

        def _up(self):
            try:
                return httpx.get(f"http://127.0.0.1:{port}/docs").status_code == 200
            except httpx.HTTPError:
                return False

        def wait(self, condition, timeout):
            deadline = time.monotonic() + timeout
            while not condition():
                assert time.monotonic() < deadline, "timed out"
                time.sleep(0.05)

        def simulate(self, *args):
            return subprocess.run(
                [sys.executable, "-m", "collector.simulator", "--seed", "1",
                 "--speed", "1e9", *args],
                env=env, capture_output=True, check=True,
            ).stdout

        def query(self, sql):
            conn = sqlite3.connect(self.db_path)
            try:
                return conn.execute(sql).fetchone()[0]
            finally:
                conn.close()

    yield Station()
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_simulator_fills_database(station):
    station.start_server()
    output = station.simulate("--duration", "300", "--noise-rate", "0.1")
    collector = station.spawn(
        "collector.collector", "--source", "simulator", stdin=subprocess.PIPE
    )
    collector.communicate(output, timeout=60)
    assert collector.returncode == 0

    lines = output.splitlines()
    bad = sum(b"BAD CRC" in line for line in lines)
    good = sum(b"\tOK\t" in line for line in lines)
    assert bad > 0
    assert station.query("SELECT COUNT(*) FROM raw_packets") == good + bad
    assert station.query("SELECT COUNT(*) FROM readings") == good
    assert station.query("SELECT COUNT(*) FROM readings WHERE quality='good'") == good
    assert station.query(
        "SELECT COUNT(*) FROM readings r JOIN raw_packets p ON p.id=r.raw_id"
        " WHERE NOT p.crc_ok"
    ) == 0
    assert station.query("SELECT COUNT(*) FROM receiver_stats") > 0


def test_server_restart_loses_nothing(station):
    server = station.start_server()
    output = station.simulate("--duration", "200", "--noise-rate", "0")
    lines = output.splitlines(keepends=True)
    total = sum(b"\tOK\t" in line for line in lines)
    collector = station.spawn(
        "collector.collector", "--source", "simulator", stdin=subprocess.PIPE
    )

    def feed(chunk):
        collector.stdin.write(b"".join(chunk))
        collector.stdin.flush()

    def count():
        return station.query("SELECT COUNT(*) FROM readings")

    feed(lines[:80])
    station.wait(lambda: count() > 0, 15)
    server.terminate()
    server.wait(10)
    feed(lines[80:150])
    station.wait(lambda: station.spool_path.stat().st_size > 0, 15)
    station.start_server()
    feed(lines[150:])
    station.wait(lambda: count() == total, 40)
    station.wait(lambda: station.spool_path.stat().st_size == 0, 10)

    collector.send_signal(signal.SIGTERM)
    assert collector.wait(15) == 0
    assert station.query("SELECT COUNT(*) FROM raw_packets") == total
    assert station.query(
        "SELECT COUNT(*) FROM (SELECT DISTINCT ts_ms FROM readings)"
    ) == total


def test_stream_delivers_and_server_still_stops(station):
    server = station.start_server()
    output = station.simulate("--duration", "5", "--noise-rate", "0", "--drop-rate", "0")
    seen = []
    with httpx.stream("GET", f"{station.url}/api/stream", timeout=15) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        lines = response.iter_lines()
        assert next(lines) == "event: heartbeat"
        collector = station.spawn(
            "collector.collector", "--source", "simulator", stdin=subprocess.PIPE
        )
        collector.communicate(output, timeout=30)
        for line in lines:
            if line.startswith("id: "):
                seen.append(int(line[4:]))
            if len(seen) == 5:
                break
        server.terminate()  # with the stream still open
        server.wait(10)
    assert seen == [1, 2, 3, 4, 5]
