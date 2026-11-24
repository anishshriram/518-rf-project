"""Tests for collector.collector and collector.spool."""

import json
import sys
import time

import httpx
import pytest

from collector import collector as module
from collector.collector import Pipeline, Recorder, Sender, read_child, read_replay
from collector.parser import Packet
from collector.spool import Spool
from server.config import CollectorConfig


def line(n, crc="OK"):
    return (
        f"TS:{1795100000000 + n * 1000}\tDrop:0\tRX:191\tTX:3378\tRSSI:-62.50\t{crc}\t"
        "Data: 0d 32  | light: 140 temp: 21.35 humidity: 45\n"
    )


class FakeServer:
    """Records delivered timestamps; `status` selects the response."""

    def __init__(self, status=200):
        self.status = status
        self.bodies = []

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def _handle(self, request):
        if self.status == "down":
            raise httpx.ConnectError("refused")
        if self.status == 200:
            self.bodies.append(json.loads(request.content))
        return httpx.Response(self.status, json={})

    @property
    def timestamps(self):
        return [p["ts_ms"] for body in self.bodies for p in body["packets"]]


def wait_for(condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@pytest.fixture
def config(tmp_path):
    return CollectorConfig(
        spool_path=str(tmp_path / "spool.jsonl"), batch_max=5, batch_interval_s=0.05
    )


def make_sender(config, server, **kwargs):
    return Sender(config, "t", "simulator", client=server.client(), **kwargs)


def expected(n):
    return [1795100000000 + i * 1000 for i in range(n)]


# Spool


def test_spool_is_fifo_and_survives_restart(tmp_path):
    path = str(tmp_path / "s.jsonl")
    spool = Spool(path)
    for n in range(3):
        spool.append({"n": n})
    assert spool.peek() == {"n": 0}
    spool.pop()
    reopened = Spool(path)
    assert len(reopened) == 2
    assert reopened.peek() == {"n": 1}
    reopened.pop()
    reopened.append({"n": 3})
    reopened.pop()
    assert reopened.peek() == {"n": 3}
    reopened.pop()
    assert reopened.peek() is None and len(reopened) == 0
    assert (tmp_path / "s.jsonl").stat().st_size == 0


def test_spool_skips_corrupt_lines(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('garbage\n{"n": 1}\n{"n": ')
    spool = Spool(str(path))
    spool.append({"n": 2})
    assert spool.peek() == {"n": 1}
    spool.pop()
    assert spool.peek() == {"n": 2}


# Sender


def test_everything_is_delivered_in_order(config):
    server = FakeServer()
    sender = make_sender(config, server)
    sender.start()
    pipeline = Pipeline(sender, True)
    for n in range(23):
        pipeline.handle(line(n))
    sender.close()
    assert server.timestamps == expected(23)
    assert all(len(b["packets"]) <= 5 for b in server.bodies)
    assert all(b["source"] == "simulator" for b in server.bodies)
    assert len(sender.spool) == 0


def test_partial_batch_is_sent_after_the_interval(config):
    server = FakeServer()
    sender = make_sender(config, server)
    sender.start()
    Pipeline(sender, True).handle(line(0))
    wait_for(lambda: server.timestamps == expected(1), timeout=1)
    sender.close()


@pytest.mark.parametrize("failure", ["down", 500, 401])
def test_outage_spools_then_drains_in_order(config, failure):
    server = FakeServer(failure)
    sender = make_sender(config, server)
    sender.start()
    pipeline = Pipeline(sender, True)
    for n in range(12):
        pipeline.handle(line(n))
    wait_for(lambda: len(sender.spool) >= 2)
    server.status = 200
    for n in range(12, 20):
        pipeline.handle(line(n))
    wait_for(lambda: len(server.timestamps) == 20)
    sender.close()
    assert server.timestamps == expected(20)
    assert len(sender.spool) == 0


def test_spool_is_delivered_by_the_next_run(config):
    server = FakeServer("down")
    sender = make_sender(config, server)
    sender.start()
    for n in range(7):
        Pipeline(sender, True).handle(line(n))
    sender.close()
    assert len(sender.spool) > 0

    server.status = 200
    second = make_sender(config, server)
    second.start()
    second.close()
    assert server.timestamps == expected(7)


def test_delay_is_reported_for_spooled_batches(config):
    server = FakeServer()
    sender = make_sender(config, server)
    sender.spool.append(
        {"source": "hardware", "packets": [], "summaries": [],
         "created_ms": module.now_ms() - 60_000}
    )
    sender.start()
    sender.close()
    (body,) = server.bodies
    assert 60_000 <= body["delay_ms"] < 70_000
    assert "created_ms" not in body


def test_refused_batch_is_set_aside(config, tmp_path):
    server = FakeServer(422)
    sender = make_sender(config, server)
    sender.start()
    Pipeline(sender, True).handle(line(0))
    sender.close()
    assert len(sender.spool) == 0
    assert (tmp_path / "spool.jsonl.rejected").read_text().count("\n") == 1


def test_full_queue_never_blocks_and_loses_nothing(config):
    server = FakeServer()
    sender = make_sender(config, server, queue_max=10)
    pipeline = Pipeline(sender, True)  # sender not running yet
    for n in range(100):
        pipeline.handle(line(n))
    assert len(sender.spool) > 0
    sender.start()
    sender.close()
    assert server.timestamps == expected(100)


# Pipeline


class ListSender:
    def __init__(self):
        self.items = []
        self.spool = []

    def put(self, item):
        self.items.append(item)


def test_pipeline_filters_and_counts():
    sender = ListSender()
    pipeline = Pipeline(sender, True)
    for text in ["banner\n", line(0), line(1, "BAD CRC"), "\n",
                 "#### Received 2 packets in 1 seconds. (50.00% OK) ####\n"]:
        pipeline.handle(text)
    assert len(sender.items) == 3
    assert (pipeline.lines, pipeline.good, pipeline.bad_crc) == (5, 1, 1)


def test_bad_crc_can_be_dropped():
    sender = ListSender()
    Pipeline(sender, False).handle(line(0, "BAD CRC"))
    assert sender.items == []


def test_unterminated_and_unstorable_lines_are_discarded():
    sender = ListSender()
    pipeline = Pipeline(sender, True)
    pipeline.handle(line(0).rstrip("\n"))
    pipeline.handle(line(0).replace("temp: 21.35", "temp: " + "9" * 400))
    pipeline.handle(line(0).replace("TX:3378", "TX:" + "9" * 30))
    assert sender.items == []


def test_recorder_keeps_raw_lines(tmp_path):
    recorder = Recorder(str(tmp_path / "logs"))
    pipeline = Pipeline(ListSender(), True, recorder)
    lines = ["banner\r\n", line(0), "cut"]
    for text in lines:
        pipeline.handle(text)
    recorder.close()
    (log,) = (tmp_path / "logs").iterdir()
    assert log.read_bytes().decode() == "".join(lines)


# Sources


def test_replay_is_paced(tmp_path):
    log = tmp_path / "a.log"
    log.write_text("banner\n" + "".join(line(n) for n in range(4)))
    sender = ListSender()
    started = time.monotonic()
    read_replay(str(log), 10, Pipeline(sender, True))
    assert 0.25 < time.monotonic() - started < 2
    assert len(sender.items) == 4


def test_child_sees_a_terminal_and_is_not_buffered():
    code = (
        "import sys, time; print(sys.stdout.isatty()); time.sleep(30)"
    )
    started = time.monotonic()
    child = read_child([sys.executable, "-c", code])
    assert next(child).strip() == "True"
    child.close()
    assert time.monotonic() - started < 10


def test_spawn_restarts_then_gives_up(monkeypatch, tmp_path):
    monkeypatch.setattr(module, "RESTART_DELAY_S", 0)
    marker = tmp_path / "runs"
    command = [sys.executable, "-c", f"open({str(marker)!r}, 'a').write('x')"]
    assert module.run_spawn(command, Pipeline(ListSender(), True)) == 1
    assert marker.read_text() == "x" * module.MAX_SPAWN_FAILURES


def test_spawn_with_missing_program_gives_up(monkeypatch):
    monkeypatch.setattr(module, "RESTART_DELAY_S", 0)
    assert module.run_spawn(["/no/such/program"], Pipeline(ListSender(), True)) == 1


def test_spawned_output_is_parsed(monkeypatch):
    monkeypatch.setattr(module, "MAX_SPAWN_FAILURES", 1)
    sender = ListSender()
    command = [sys.executable, "-c", f"print({line(0)!r}, end='')"]
    module.run_spawn(command, Pipeline(sender, True))
    assert isinstance(sender.items[0], Packet)
    assert not sender.items[0].raw_line.endswith("\r")


def test_default_source():
    assert module.parse_args([]).source == "hardware"
    assert module.parse_args(["--replay", "x"]).source == "replay"
    assert module.parse_args(["--source", "simulator"]).source == "simulator"
