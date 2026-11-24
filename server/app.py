"""FastAPI app: routes and startup (spec section 12).

Run with `python -m server.app`.
"""

import hmac
import sys
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from server import db, history
from server.config import Config, ConfigError, load_config
from server.ingest import IngestRequest, ingest
from server.stream import Broadcaster

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
REPLAY_MAX = 2000
DEFAULT_SPAN_MS = 15 * history.MINUTE
Resolution = Literal["auto", "raw", "5s", "1m", "5m", "15m", "1h", "1d"]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def create_app(config: Config) -> FastAPI:
    db_lock = threading.Lock()
    broadcaster = Broadcaster()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db.connect(config.server.db_path)
        broadcaster.start()
        yield
        with db_lock:
            app.state.db.close()

    app = FastAPI(title="PIP Weather Station", version="0.1.0", lifespan=lifespan)
    app.state.broadcaster = broadcaster

    def reader():
        return closing(db.open_reader(config.server.db_path))

    def require_token(token: str | None) -> None:
        expected = config.server.ingest_token.encode()
        if not hmac.compare_digest((token or "").encode(), expected):
            raise HTTPException(status_code=401, detail="invalid ingest token")

    # Plain functions: FastAPI runs them in worker threads, so SQLite does not
    # block the event loop.
    @app.post("/api/ingest", summary="Receive packets from the collector")
    def post_ingest(
        request: IngestRequest,
        x_ingest_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, int]:
        require_token(x_ingest_token)
        with db_lock:
            counts, readings = ingest(app.state.db, config, request)
            # Inside the lock, so clients see readings in id order.
            broadcaster.publish_readings(readings)
        return counts

    @app.get("/api/current", summary="Latest reading and tag status")
    def get_current() -> dict:
        with reader() as conn:
            return current(conn, config, now_ms())

    @app.get("/api/history", summary="Time series for the charts")
    def get_history(
        from_: Annotated[int | None, Query(alias="from", ge=0, lt=2**53)] = None,
        to: Annotated[int | None, Query(ge=0, lt=2**53)] = None,
        metrics: str = "temp_c,rh_pct,light_raw",
        resolution: Resolution = "auto",
    ) -> dict:
        names = list(dict.fromkeys(m.strip() for m in metrics.split(",")))
        unknown = [m for m in names if m not in history.METRICS]
        if unknown:
            raise HTTPException(422, f"unknown metrics: {', '.join(unknown)}")
        with reader() as conn:
            latest = db.latest_reading(conn)
            # Simulated time can run ahead of the clock.
            end = to if to is not None else now_ms()
            if to is None and latest:
                end = max(end, latest["ts_ms"])
            start = from_ if from_ is not None else end - DEFAULT_SPAN_MS
            if start > end:
                raise HTTPException(422, "'from' must not be after 'to'")
            if resolution == "raw" and end - start > history.MAX_RAW_SPAN:
                raise HTTPException(422, "raw resolution is limited to 6 hours")
            return history.query(
                conn,
                source=latest["source"] if latest else "hardware",
                start=start,
                end=end,
                metrics=names,
                resolution=resolution,
                beacon_s=config.tag.expected_beacon_interval_s,
            )

    @app.get(
        "/api/stream",
        summary="Live readings as Server-Sent Events",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def get_stream(
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        valid = last_event_id and last_event_id.isascii() and last_event_id.isdigit()
        last_id = int(last_event_id[:18]) if valid else None

        def missed_since(reading_id: int) -> list[dict]:
            with reader() as conn:
                return db.readings_after(conn, reading_id, REPLAY_MAX)

        return StreamingResponse(
            broadcaster.events(last_id, missed_since),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def current(conn, config: Config, now: int) -> dict:
    """Build the GET /api/current response."""
    latest = db.latest_reading(conn)
    tag = config.tag
    age_s = None
    status = "never_seen"
    if latest:
        # Simulated and replayed timestamps say nothing about freshness.
        seen = latest["ts_ms" if latest["source"] == "hardware" else "received_ms"]
        age_s = max(0.0, (now - seen) / 1000)
        status = "online"
        if age_s >= tag.offline_critical_s:
            status = "offline_critical"
        elif age_s >= tag.offline_warning_s:
            status = "offline_warning"
    value: Callable = (latest or {}).get
    return {
        "station": {"name": config.station.name, "timezone": config.station.timezone},
        "mode": value("source"),
        "server_ms": now,
        "last_reading_ms": value("ts_ms"),
        "age_s": age_s,
        "status": status,
        "tag": {
            "allowed_tx_ids": tag.allowed_tx_ids,
            "offline_warning_s": tag.offline_warning_s,
            "offline_critical_s": tag.offline_critical_s,
        },
        "bad_crc_1h": db.bad_crc_since(conn, now - history.HOUR),
        "temp_c": value("temp_c"),
        "rh_pct": value("rh_pct"),
        "light_raw": value("light_raw"),
        "rssi_dbm": value("rssi_dbm"),
        "quality": value("quality"),
    }


def _load_or_exit() -> Config:
    try:
        return load_config()
    except ConfigError as exc:
        sys.exit(f"Configuration error: {exc}")


def __getattr__(name: str):
    # Built on first access so that importing this module (as tests do) does not
    # read the user's config.toml.
    if name == "app":
        app = create_app(_load_or_exit())
        globals()["app"] = app
        return app
    raise AttributeError(name)


def main() -> None:
    import uvicorn

    config = _load_or_exit()
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        # Open event streams never end by themselves; do not wait for them.
        timeout_graceful_shutdown=1,
    )


if __name__ == "__main__":
    main()
