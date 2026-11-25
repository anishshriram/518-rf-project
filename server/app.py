"""FastAPI app: routes and startup (spec section 12).

Run with `python -m server.app`.
"""

import asyncio
import copy
import hmac
import logging
import sys
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, closing
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from server import (
    aggregates,
    calibration,
    daily,
    db,
    derive,
    export,
    health,
    history,
    notify_email,
    settings,
    sun,
)
from server.access import is_local, local_only
from server.alerts import AlertManager, channel_enabled, query_events
from server.calibration import Calibrator
from server.config import Config, ConfigError, load_config
from server.inference import Engine
from server.ingest import IngestRequest, ingest
from server.stream import Broadcaster

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
REPLAY_MAX = 2000
STREAMS_MAX = 100
logger = logging.getLogger("uvicorn.error")
DEFAULT_SPAN_MS = 15 * history.MINUTE
CURRENT_VALUES = (
    "temp_c",
    "rh_pct",
    "dew_point_c",
    "frost_point_c",
    "heat_index_c",
    "feels_like_c",
    "abs_humidity_gm3",
    "vpd_kpa",
    "light_raw",
    "rssi_dbm",
    "quality",
)
Resolution = Literal["auto", "raw", "5s", "1m", "5m", "15m", "1h", "1d"]


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def create_app(config: Config) -> FastAPI:
    db_lock = threading.Lock()
    base_config = copy.deepcopy(config)  # as in the file, without saved settings
    broadcaster = Broadcaster()
    calibrator = Calibrator()
    started = time.monotonic()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db.connect(config.server.db_path)
        settings.apply_stored(app.state.db, config)
        broadcaster.start()
        app.state.alerts = make_alerts(app.state.db)
        tasks = [
            asyncio.create_task(calibration_loop()),
            asyncio.create_task(inference_loop()),
            asyncio.create_task(daily_loop()),
        ]
        yield
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with db_lock:
            app.state.db.close()

    app = FastAPI(title="PIP Weather Station", version="0.1.0", lifespan=lifespan)
    app.state.broadcaster = broadcaster

    def calibrate_if_due() -> None:
        """Recalibrate once the newest data is an hour past the last time."""
        with reader() as conn:
            latest = db.latest_reading(conn)
            if latest is None:
                return
            source, ts_ms = latest["source"], latest["ts_ms"]
            before = calibrator.get(conn, source)
            if not calibrator.is_due(before, ts_ms):
                return
            # Reading takes longest, so it happens outside the write lock.
            result = calibration.calibrate(conn, config, source, ts_ms)
        with db_lock:
            calibration.save(app.state.db, source, result)
            calibrator.put(source, result)
            if result.calibrated and not before.calibrated:
                # Earlier days had no sunshine figures yet.
                forget_daily(source)
        if result.light_k != before.light_k:
            broadcaster.publish_event("calibration", {"calibrated": result.calibrated})

    async def calibration_loop() -> None:
        while True:
            started = time.monotonic()
            try:
                await asyncio.to_thread(calibrate_if_due)
            except Exception:
                logger.exception("Light calibration failed")
            # Stay light on the CPU when simulated hours pass quickly.
            await asyncio.sleep(max(5.0, 10 * (time.monotonic() - started)))

    def make_alerts(conn) -> AlertManager:
        def latest():
            with reader() as connection:
                return db.latest_reading(connection)

        def send(event: dict) -> None:
            # Checked each time: email can be switched on while running.
            if not config.alerts.email_enabled:
                return
            missing = notify_email.missing_settings(config)
            if missing:
                logger.warning("Alert not emailed; missing: %s", ", ".join(missing))
            else:
                sender.put(event)

        sender = notify_email.EmailSender(
            config, latest, lambda event_id: alerts.mark_emailed(event_id)
        )
        alerts = AlertManager(
            conn, db_lock, config, broadcaster.publish_event, send, now_ms
        )
        return alerts

    engines: dict[str, Engine] = {}

    def run_inference() -> None:
        with reader() as conn:
            latest = db.latest_reading(conn)
            if latest is None:
                return
            source = latest["source"]
            if source not in engines:
                engines[source] = Engine(config, source, app.state.alerts)
            engines[source].tick(conn, calibrator.get(conn, source), now_ms())

    async def inference_loop() -> None:
        while True:
            started = time.monotonic()
            try:
                await asyncio.to_thread(run_inference)
            except Exception:
                logger.exception("Inference failed")
                await asyncio.sleep(5)
            await asyncio.sleep(max(1.0, time.monotonic() - started))

    daily_generation = 0

    def forget_daily(source: str, days: set[date] | None = None) -> None:
        """Drop stored summaries; call with db_lock held."""
        nonlocal daily_generation
        daily_generation += 1
        daily.forget(app.state.db, source, days)

    def store_daily() -> bool:
        """Summarise one finished day; False if none is left to do."""
        generation = daily_generation
        with reader() as conn:
            latest = db.latest_reading(conn)
            if latest is None:
                return False
            source = latest["source"]
            today = sun.local_date(config.station, latest["ts_ms"])
            days = daily.missing_days(conn, config, source, today, 1)
            if not days:
                return False
            light = calibrator.get(conn, source)
            summary = daily.summarise(
                conn, config, source, days[0], light, latest["ts_ms"]
            )
        with db_lock:
            # Otherwise its inputs changed meanwhile: compute it again.
            if summary and generation == daily_generation:
                daily.store(app.state.db, summary)
        return True

    async def daily_loop() -> None:
        while True:
            more = False
            try:
                more = await asyncio.to_thread(store_daily)
            except Exception:
                logger.exception("Daily summary failed")
            await asyncio.sleep(0.2 if more else 10)

    def forget_changed_days(readings: list[dict]) -> None:
        """Late data changes days that may already be summarised."""
        if not readings:
            return
        days = {sun.local_date(config.station, r["ts_ms"]) for r in readings}
        source = readings[0]["source"]
        newest = sun.local_date(config.station, max(r["ts_ms"] for r in readings))
        if len(days) > 1 or newest != newest_day.get(source):
            forget_daily(source, days)
            newest_day[source] = newest

    newest_day: dict[str, date] = {}

    def add_light(conn, reading: dict) -> dict:
        """Fill in the values that depend on the light calibration."""
        current_calibration = calibrator.get(conn, reading["source"])
        calibrator.observe(current_calibration, reading["light_raw"])
        elevation = sun.elevation(config.station, reading["ts_ms"])
        return {
            **reading,
            "light_norm": current_calibration.light_norm(reading["light_raw"]),
            "clear_sky_norm": current_calibration.clear_sky_norm(elevation),
        }

    def reader():
        """A connection that sees one unchanging snapshot of the database.

        Otherwise packets that arrive between two queries would make the last
        known reading look old.
        """
        conn = db.open_reader(config.server.db_path)
        conn.execute("BEGIN")
        return closing(conn)

    def require_token(token: str | None) -> None:
        expected = config.server.ingest_token.encode()
        if not hmac.compare_digest((token or "").encode(), expected):
            raise HTTPException(status_code=401, detail="invalid ingest token")

    # Plain functions: FastAPI runs them in worker threads, so SQLite does not
    # block the event loop.
    @app.post("/api/ingest", summary="Receive packets from the collector")
    def post_ingest(
        request: IngestRequest,
        http: Request,
        x_ingest_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, int]:
        require_token(x_ingest_token)
        if config.server.ingest_token == Config().server.ingest_token and (
            not is_local(http)
        ):
            raise HTTPException(403, "set your own ingest_token first")
        with db_lock:
            counts, readings = ingest(app.state.db, config, request)
            forget_changed_days(readings)
            # Inside the lock, so clients see readings in id order.
            broadcaster.publish_readings(
                [add_light(app.state.db, reading) for reading in readings]
            )
        return counts

    @app.get("/api/current", summary="Latest reading and tag status")
    def get_current(http: Request) -> dict:
        with reader() as conn:
            result = current(conn, config, calibrator, now_ms())
        result["can_edit"] = is_local(http)
        if not result["can_edit"]:
            result["station"].update(latitude=None, longitude=None)
        return result

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
            end = to if to is not None else now_ms()
            if to is None and latest:
                # Simulated and replayed time can be far from the clock, either way.
                live = latest["source"] == "hardware"
                end = max(end, latest["ts_ms"]) if live else latest["ts_ms"]
            start = from_ if from_ is not None else end - DEFAULT_SPAN_MS
            if start > end:
                raise HTTPException(422, "'from' must not be after 'to'")
            if resolution == "raw" and end - start > history.MAX_RAW_SPAN:
                raise HTTPException(422, "raw resolution is limited to 6 hours")
            source = latest["source"] if latest else "hardware"
            return history.query(
                conn,
                source=source,
                start=start,
                end=end,
                metrics=names,
                resolution=resolution,
                config=config,
                calibration=calibrator.get(conn, source),
            )

    @app.get("/api/daily", summary="Summaries per local day, oldest first")
    def get_daily(
        from_: Annotated[date | None, Query(alias="from")] = None,
        to: date | None = None,
    ) -> list[dict]:
        with reader() as conn:
            today, rows = daily_rows(conn, from_, to)
            return rows

    @app.get("/api/daily/totals", summary="Degree days for the month and season")
    def get_totals() -> dict:
        with reader() as conn:
            today, rows = daily_rows(conn, None, None, days=400)
            return daily.totals(rows, today)

    def daily_rows(conn, first: date | None, last: date | None, days: int = 30):
        """Stored summaries plus the unfinished day, for the current source."""
        latest = db.latest_reading(conn)
        if latest is None:
            return sun.local_date(config.station, now_ms()), []
        source = latest["source"]
        today = sun.local_date(config.station, latest["ts_ms"])
        last = last or today
        first = first or max(last, date(1971, 1, 1)) - timedelta(days=days - 1)
        rows = [
            {**row, "in_progress": False}
            for row in daily.load(conn, source, first, min(last, today))
            if row["local_date"] != today.isoformat()
        ]
        if first <= today <= last:
            light = calibrator.get(conn, source)
            row = daily.summarise(conn, config, source, today, light, latest["ts_ms"])
            if row:
                rows.append({**row, "in_progress": True})
        return today, rows

    @app.get("/api/health", summary="Tag, receiver, collector and server health")
    def get_health() -> dict:
        with reader() as conn:
            latest = db.latest_reading(conn)
            return {
                "tag": health.tag_health(conn, config, latest, now_ms()),
                "receiver": health.receiver_health(conn),
                "collector": health.collector_health(conn),
                "server": {
                    "uptime_s": round(time.monotonic() - started),
                    "db_size_mb": round(db.size_mb(config.server.db_path), 2),
                    "version": app.version,
                },
            }

    @app.get(
        "/api/export.csv",
        summary="Download readings as CSV",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/csv": {}}}},
    )
    def get_export(
        from_: Annotated[int | None, Query(alias="from", ge=0, lt=2**53)] = None,
        to: Annotated[int | None, Query(ge=0, lt=2**53)] = None,
        resolution: Resolution = "auto",
    ) -> StreamingResponse:
        conn = db.open_reader(config.server.db_path)
        try:
            latest = db.latest_reading(conn)
            source = latest["source"] if latest else "hardware"
            end = to if to is not None else (latest["ts_ms"] if latest else now_ms())
            start = from_ if from_ is not None else max(0, end - history.DAY)
            if start > end:
                raise HTTPException(422, "'from' must not be after 'to'")
            light = calibrator.get(conn, source)
        except BaseException:
            conn.close()
            raise
        name = f"pip-weather-{source}-{sun.local_date(config.station, end)}.csv"
        return StreamingResponse(
            export.stream(conn, config, light, source, start, end, resolution),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get(
        "/api/settings",
        summary="Editable settings, with defaults",
        dependencies=[Depends(local_only)],
    )
    def get_settings() -> dict:
        return settings.describe(base_config, config)

    @app.put(
        "/api/settings",
        summary="Change some settings; null restores a default",
        dependencies=[Depends(local_only), Depends(same_site)],
    )
    def put_settings(changes: dict[str, dict]) -> dict:
        with db_lock:
            try:
                changed = settings.update(
                    app.state.db, base_config, config, changes, now_ms()
                )
            except ConfigError as exc:
                raise HTTPException(422, str(exc)) from exc
            if any(key.startswith(("station.", "thresholds.")) for key in changed):
                # Recomputed with the new values.
                for source in ("hardware", "simulator", "replay"):
                    forget_daily(source)
        if changed:
            broadcaster.publish_event("settings", {"changed": sorted(changed)})
        return settings.describe(base_config, config)

    @app.get("/api/events", summary="Inferences and alerts, newest first")
    def get_events(
        from_: Annotated[int | None, Query(alias="from", ge=0, lt=2**53)] = None,
        to: Annotated[int | None, Query(ge=0, lt=2**53)] = None,
        active: bool | None = None,
        type_: Annotated[str | None, Query(alias="type")] = None,
        limit: Annotated[int, Query(ge=1, le=2000)] = 500,
    ) -> list[dict]:
        with reader() as conn:
            latest = db.latest_reading(conn)
            source = latest["source"] if latest else "hardware"
            events = query_events(conn, source, from_, to, active, type_, limit)
        for event in events:
            event["banner"] = channel_enabled(config, event["type"], "dashboard")
        return events

    @app.post(
        "/api/events/{event_id}/ack",
        summary="Dismiss an alert",
        dependencies=[Depends(local_only), Depends(same_site)],
    )
    def post_ack(event_id: int) -> dict[str, bool]:
        if not 0 <= event_id < 2**63 or not app.state.alerts.acknowledge(event_id):
            raise HTTPException(404, "no such event")
        return {"acknowledged": True}

    @app.post(
        "/api/alerts/test",
        summary="Send a test alert through every enabled channel",
        dependencies=[Depends(local_only), Depends(same_site)],
    )
    def post_test_alert() -> dict[str, str]:
        event = {
            "id": None,
            "type": "test",
            "severity": "watch",
            "title": "Test alert",
            "explanation": "This is only a test. Nothing is wrong.",
            "start_ms": now_ms(),
            "source": None,
            "notify": True,
        }
        broadcaster.publish_event("alert", event)
        email = "disabled"
        if config.alerts.email_enabled:
            missing = notify_email.missing_settings(config)
            if missing:
                email = f"not configured: {', '.join(missing)}"
            else:
                try:
                    with reader() as conn:
                        latest = db.latest_reading(conn)
                    notify_email.send(notify_email.build_message(config, event, latest))
                    email = "sent"
                except Exception as exc:
                    logger.error("Test email failed: %s", exc)
                    email = f"failed: {type(exc).__name__}"
        return {
            "dashboard": "sent",
            "browser": "sent" if config.alerts.browser_notifications else "disabled",
            "email": email,
        }

    @app.get(
        "/api/stream",
        summary="Live readings as Server-Sent Events",
        response_class=StreamingResponse,
        responses={200: {"content": {"text/event-stream": {}}}},
    )
    async def get_stream(
        last_event_id: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse:
        if broadcaster.client_count >= STREAMS_MAX:
            raise HTTPException(503, "too many viewers")
        valid = last_event_id and last_event_id.isascii() and last_event_id.isdigit()
        last_id = int(last_event_id[:18]) if valid else None

        def missed_since(reading_id: int) -> list[dict]:
            with reader() as conn:
                missed = db.readings_after(conn, reading_id, REPLAY_MAX)
                return [add_light(conn, reading) for reading in missed]

        return StreamingResponse(
            broadcaster.events(last_id, missed_since),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


def current(conn, config: Config, calibrator: Calibrator, now: int) -> dict:
    """Build the GET /api/current response."""
    latest = db.latest_reading(conn)
    tag = config.tag
    status = health.tag_status(conn, config, latest, now)
    light = calibrator.get(conn, latest["source"]) if latest else None
    active = (
        {e["type"] for e in query_events(conn, latest["source"], active=True)}
        if latest
        else set()
    )
    conditions = (
        aggregates.conditions(conn, config, latest["source"], latest["ts_ms"], light)
        if latest
        else {
            "sky": None,
            "clearness": None,
            "change_1h": {"temp_c": None, "rh_pct": None},
            "trends": {},
            "trends_60m": {},
            "today": {},
        }
    )
    value: Callable = (latest or {}).get
    return {
        "station": {
            "name": config.station.name,
            "timezone": config.station.timezone,
            "latitude": config.station.latitude,
            "longitude": config.station.longitude,
        },
        "smoothing_alpha": config.smoothing.ema_alpha,
        "home": {
            "indoor_setpoint_c": config.home.indoor_setpoint_c,
            "indoor_rh_assumed": config.home.indoor_rh_assumed,
        },
        "display_temperature": config.units.display_temperature,
        "mode": value("source"),
        "server_ms": now,
        "last_reading_ms": value("ts_ms"),
        "age_s": status["age_s"],
        "status": status["status"],
        "tag": {
            "allowed_tx_ids": tag.allowed_tx_ids,
            "offline_warning_s": status["offline_warning_s"],
            "offline_critical_s": status["offline_critical_s"],
        },
        "bad_crc_1h": db.bad_crc_since(conn, now - history.HOUR),
        **{name: value(name) for name in CURRENT_VALUES},
        **conditions,
        "light_norm": light.light_norm(latest["light_raw"]) if latest else None,
        "sun": sun_info(config, latest["ts_ms"] if latest else now),
        "flags": {
            "calibrating_light": light is None or not light.calibrated,
            "light_saturated": latest is not None
            and light.is_saturated(latest["light_raw"]),
            "solar_heating": "solar_heating" in active,
        },
        "browser_notifications": config.alerts.browser_notifications,
        "sound": config.alerts.sound,
        "recommendations": {
            "open_windows": "open_windows" in active if latest else None,
            "laundry_score": laundry(conditions, latest, "rain_onset" in active)
            if latest
            else None,
            "window_condensation_risk": derive.condensation_risk(
                config.home.indoor_setpoint_c,
                config.home.indoor_rh_assumed,
                latest["temp_c"],
                config.home.window_u_value,
            )
            if latest
            else None,
        },
    }


def laundry(conditions: dict, latest: dict, rain: bool) -> float | None:
    """Drying score; None in daytime while the sky is unknown."""
    clearness = conditions["clearness"]
    if clearness is None:
        if conditions["sky"] is None:
            return None
        clearness = 0.0  # night or twilight
    return derive.laundry_score(latest["temp_c"], latest["rh_pct"], clearness, rain)


def same_site(sec_fetch_site: Annotated[str | None, Header()] = None) -> None:
    """Keep other web pages from triggering actions here."""
    if sec_fetch_site in ("cross-site", "same-site"):
        raise HTTPException(403, "cross-site request")


def sun_info(config: Config, ts_ms: int) -> dict:
    sunrise, sunset = sun.sun_times(config.station, sun.local_date(config.station, ts_ms))
    elevation = sun.elevation(config.station, ts_ms)
    return {
        "elevation_deg": elevation,
        "sunrise_ms": sunrise,
        "sunset_ms": sunset,
        "is_daylight": elevation > 0,
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
