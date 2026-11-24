"""Read pip_sense lines, parse them and POST batches to the server (section 11).

The main thread only reads and parses. A background thread does all HTTP, and
moves batches to an on-disk spool whenever the server is unavailable.
"""

import argparse
import logging
import math
import os
import pty
import queue
import shlex
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx

from collector.parser import Packet, Summary, parse_line
from collector.spool import Spool
from server.config import CollectorConfig, ConfigError, load_config

logger = logging.getLogger("collector")

HTTP_TIMEOUT_S = 3.0
BACKOFF_MAX_S = 30.0
QUEUE_MAX = 10_000
STATUS_INTERVAL_S = 30.0
SHUTDOWN_LIMIT_S = 10.0
RESTART_DELAY_S = 5.0
MAX_SPAWN_FAILURES = 5
PERMANENT_ERRORS = {400, 413, 422}  # resending the same batch cannot succeed


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Sender:
    """Batches items and delivers them in order from a background thread."""

    def __init__(
        self,
        config: CollectorConfig,
        token: str,
        source: str,
        client: httpx.Client | None = None,
        queue_max: int = QUEUE_MAX,
    ):
        self.spool = Spool(config.spool_path)
        self._config = config
        self._source = source
        self._url = config.server_url.rstrip("/") + "/api/ingest"
        self._headers = {"X-Ingest-Token": token}
        self._client = client or httpx.Client(timeout=HTTP_TIMEOUT_S)
        self._queue: queue.Queue[Packet | Summary] = queue.Queue(queue_max)
        self._pending: list[Packet | Summary] = []
        self._pending_since = 0.0
        self._backoff = 0.0
        self._next_try = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sender", daemon=True)
        self.on_tick = lambda: None

    def start(self) -> None:
        self._thread.start()

    def put(self, item: Packet | Summary) -> None:
        """Queue an item without ever waiting for HTTP."""
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except queue.Full:
                self._spill_oldest()

    def close(self) -> None:
        """Deliver or spool everything that was queued, then stop."""
        self._stop.set()
        self._thread.join()
        self._client.close()

    def _spill_oldest(self) -> None:
        items = []
        while len(items) < self._config.batch_max:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if items:
            self.spool.append(self._make_batch(items))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._collect(self._wait_time())
                self._step(force=False)
                self.on_tick()
            except Exception:
                logger.exception("Sender error")
                time.sleep(1)
        self._finish()

    def _wait_time(self) -> float:
        if len(self.spool) and time.monotonic() >= self._next_try:
            return 0.0
        return min(0.1, self._config.batch_interval_s)

    def _collect(self, timeout: float) -> None:
        """Move queued items into the pending batch."""
        while len(self._pending) < self._config.batch_max:
            try:
                item = self._queue.get(timeout=timeout) if timeout else (
                    self._queue.get_nowait()
                )
            except queue.Empty:
                return
            if not self._pending:
                self._pending_since = time.monotonic()
            self._pending.append(item)
            timeout = 0.0

    def _step(self, force: bool) -> None:
        now = time.monotonic()
        if self._pending and (
            force
            or len(self._pending) >= self._config.batch_max
            or now - self._pending_since >= self._config.batch_interval_s
        ):
            batch = self._make_batch(self._pending)
            # Older data always goes first.
            if len(self.spool) or now < self._next_try or not self._deliver(batch):
                self.spool.append(batch)
            self._pending = []
        if len(self.spool) and time.monotonic() >= self._next_try:
            self._drain_one()

    def _drain_one(self) -> bool:
        batch = self.spool.peek()
        if batch is None or not self._deliver(batch):
            return False
        self.spool.pop()
        return True

    def _finish(self) -> None:
        deadline = time.monotonic() + SHUTDOWN_LIMIT_S
        try:
            while True:
                self._collect(0.0)
                if not self._pending:
                    break
                batch = self._make_batch(self._pending)
                late = time.monotonic() > deadline
                blocked = len(self.spool) or late or self._backoff
                if blocked or not self._deliver(batch):
                    self.spool.append(batch)
                self._pending = []
            while len(self.spool) and time.monotonic() < deadline:
                if not self._drain_one():
                    break
        except Exception:
            logger.exception("Could not flush all data on shutdown")
        if len(self.spool):
            logger.warning("%d batches left in the spool", len(self.spool))

    def _make_batch(self, items: list[Packet | Summary]) -> dict:
        return {
            "source": self._source,
            "packets": [asdict(i) for i in items if isinstance(i, Packet)],
            "summaries": [asdict(i) for i in items if isinstance(i, Summary)],
            "created_ms": now_ms(),
        }

    def _deliver(self, batch: dict) -> bool:
        """POST a batch. True means it needs no further attempt."""
        body = {k: v for k, v in batch.items() if k != "created_ms"}
        body["delay_ms"] = max(0, now_ms() - batch.get("created_ms", now_ms()))
        try:
            response = self._client.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:
            return self._failed(f"{type(exc).__name__}: {exc}")
        if response.status_code == 200:
            if self._backoff:
                logger.info("Server reachable again")
            self._backoff = 0.0
            self._next_try = 0.0
            return True
        if response.status_code in PERMANENT_ERRORS:
            logger.error(
                "Server refused a batch (%d); moved to %s.rejected: %s",
                response.status_code,
                self._config.spool_path,
                response.text[:300],
            )
            self.spool.reject(batch)
            return True
        hint = " (check server.ingest_token)" if response.status_code == 401 else ""
        return self._failed(f"HTTP {response.status_code}{hint}")

    def _failed(self, reason: str) -> bool:
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S) if self._backoff else 1.0
        self._next_try = time.monotonic() + self._backoff
        logger.warning("Send failed (%s); retrying in %.0f s", reason, self._backoff)
        return False


class Recorder:
    """Appends raw lines to one log file per local day."""

    def __init__(self, directory: str):
        self._directory = Path(directory)
        self._directory.mkdir(parents=True, exist_ok=True)
        self._date = None
        self._file = None

    def write(self, line: str) -> None:
        date = datetime.now().date()
        if date != self._date:
            self.close()
            self._file = (self._directory / f"{date}.log").open(
                "a", encoding="utf-8", newline=""
            )
            self._date = date
        self._file.write(line)
        self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()


class Pipeline:
    """Turns raw lines into queued items and keeps the status counters."""

    def __init__(self, sender: Sender, forward_bad_crc: bool, recorder=None):
        self._sender = sender
        self._forward_bad_crc = forward_bad_crc
        self._recorder = recorder
        self._last_status = time.monotonic()
        self.lines = self.good = self.bad_crc = 0
        self.last_rssi: float | None = None

    def handle(self, line: str) -> Packet | Summary | None:
        self.lines += 1
        if self._recorder:
            self._recorder.write(line)
        # A last line without a newline may be cut off inside a number.
        if not line.endswith("\n"):
            logger.warning("Discarding unterminated line: %r", line)
            return None
        result = parse_line(line)
        if isinstance(result, Packet):
            if not is_sendable(result):
                logger.warning("Discarding out-of-range packet: %r", line)
                return None
            if result.crc_ok:
                self.good += 1
                self.last_rssi = result.rssi_dbm
            else:
                self.bad_crc += 1
                if not self._forward_bad_crc:
                    return result
        if result is not None:
            self._sender.put(result)
        return result

    def tick(self) -> None:
        if time.monotonic() - self._last_status >= STATUS_INTERVAL_S:
            self.print_status()

    def print_status(self) -> None:
        self._last_status = time.monotonic()
        print(
            f"[collector] lines={self.lines} good={self.good} "
            f"bad_crc={self.bad_crc} last_rssi={self.last_rssi} "
            f"spool={len(self._sender.spool)}",
            file=sys.stderr,
            flush=True,
        )


def is_sendable(packet: Packet) -> bool:
    """False if a value cannot be stored, which would make the server refuse the
    whole batch."""
    values = [v for v in asdict(packet).values() if isinstance(v, (int, float))]
    return (
        0 <= packet.ts_ms < 2**53
        and all(math.isfinite(v) for v in values)
        and all(-(2**63) <= v < 2**63 for v in values if isinstance(v, int))
    )


def read_stdin() -> Iterator[str]:
    for line in sys.stdin.buffer:
        yield line.decode("utf-8", errors="replace")


def read_replay(path: str, speed: float, pipeline: Pipeline) -> None:
    """Feed a recorded log through the pipeline, paced by its timestamps."""
    first_ts = None
    started = time.monotonic()
    with open(path, encoding="utf-8", errors="replace", newline="\n") as f:
        for line in f:
            packet = parse_line(line)
            if isinstance(packet, Packet):
                if first_ts is None:
                    first_ts = packet.ts_ms
                due = started + (packet.ts_ms - first_ts) / 1000 / speed
                time.sleep(max(0.0, due - time.monotonic()))
            pipeline.handle(line)


def read_child(command: list[str]) -> Iterator[str]:
    """Run a program inside a pseudo-terminal, so that it does not buffer its
    output, and yield its lines until it exits."""
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
    except OSError:
        os.close(master)
        raise
    finally:
        os.close(slave)
    buffer = b""
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # Linux reports EIO when the child exits
                chunk = b""
            if not chunk:
                break
            *lines, buffer = (buffer + chunk).split(b"\n")
            for line in lines:
                yield line.decode("utf-8", errors="replace") + "\n"
        if buffer:
            yield buffer.decode("utf-8", errors="replace")
    finally:
        os.close(master)
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        logger.info("Child exited with status %s", process.returncode)


def run_spawn(command: list[str], pipeline: Pipeline) -> int:
    """Keep the child running; give up after repeated failures."""
    failures = 0
    while True:
        child = read_child(command)
        try:
            for line in child:
                if pipeline.handle(line) is not None:
                    failures = 0
        except OSError as exc:
            logger.error("Cannot run %s: %s", command[0], exc)
        finally:
            child.close()
        failures += 1
        if failures >= MAX_SPAWN_FAILURES:
            logger.error(
                "%s failed %d times in a row; giving up. Is the receiver plugged in?",
                command[0],
                failures,
            )
            return 1
        logger.warning("%s exited; restarting in %.0f s", command[0], RESTART_DELAY_S)
        time.sleep(RESTART_DELAY_S)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="collector", description=__doc__)
    parser.add_argument("--config", help="default: config.toml if it exists")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--spawn", metavar="CMD", help="run CMD in a pseudo-terminal")
    mode.add_argument("--replay", metavar="FILE", help="replay a recorded log")
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed factor")
    parser.add_argument("--source", choices=["hardware", "simulator", "replay"])
    parser.add_argument("--record", action="store_true", help="save raw lines to logs/")
    parser.add_argument("--log-dir", default="logs")
    args = parser.parse_args(argv)
    if args.replay and args.record:
        parser.error("--record cannot be combined with --replay")
    if args.spawn is not None and not args.spawn.split():
        parser.error("--spawn needs a command")
    if not (args.speed > 0 and math.isfinite(args.speed)):
        parser.error("--speed must be a positive number")
    if args.source is None:
        args.source = "replay" if args.replay else "hardware"
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    def interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)

    recorder = Recorder(args.log_dir) if args.record else None
    sender = Sender(config.collector, config.server.ingest_token, args.source)
    pipeline = Pipeline(sender, config.collector.forward_bad_crc, recorder)
    sender.on_tick = pipeline.tick
    sender.start()

    status = 0
    try:
        if args.replay:
            read_replay(args.replay, args.speed, pipeline)
        elif args.spawn:
            status = run_spawn(shlex.split(args.spawn), pipeline)
        else:
            for line in read_stdin():
                pipeline.handle(line)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        logger.error("%s", exc)
        status = 1
    finally:
        # A second signal must not interrupt the final flush.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        sender.close()
        if recorder:
            recorder.close()
        pipeline.print_status()
    return status


if __name__ == "__main__":
    sys.exit(main())
