"""Event storage, alert lifecycle and notifications (spec section 18)."""

import json
import logging
import sqlite3
import threading
from collections.abc import Callable

from server.config import Config

logger = logging.getLogger("uvicorn.error")

SEVERITIES = ("info", "watch", "warning", "critical")
ALERT_FROM = SEVERITIES.index("watch")
STALE_MS = 3_600_000  # no notifications about hardware events older than this


def rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def is_alert(event: dict) -> bool:
    return rank(event["severity"]) >= ALERT_FROM


def to_dict(row: sqlite3.Row) -> dict:
    event = dict(row)
    event["details"] = json.loads(event.pop("details_json") or "{}")
    event["source"] = event["details"].pop("source", None)
    event["acknowledged"] = bool(event["acknowledged"])
    event["notified_email"] = bool(event["notified_email"])
    return event


def query_events(
    conn: sqlite3.Connection,
    source: str,
    start: int | None = None,
    end: int | None = None,
    active: bool | None = None,
    kind: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Events of one source that overlap [start, end], newest first."""
    where, values = ["json_extract(details_json, '$.source') = ?"], [source]
    if start is not None:
        where.append("(end_ms IS NULL OR end_ms >= ?)")
        values.append(start)
    if end is not None:
        where.append("start_ms <= ?")
        values.append(end)
    if active is not None:
        where.append("end_ms IS NULL" if active else "end_ms IS NOT NULL")
    if kind is not None:
        where.append("type = ?")
        values.append(kind)
    rows = conn.execute(
        f"SELECT * FROM events WHERE {' AND '.join(where)} "
        "ORDER BY start_ms DESC, id DESC LIMIT ?",
        (*values, limit),
    ).fetchall()
    return [to_dict(row) for row in rows]


class AlertManager:
    """Opens, escalates and closes events, and tells the world about them.

    `publish(event_name, data)` sends to dashboards; `send_email(event)` queues an
    email. Writes happen under `lock`.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        lock: threading.Lock,
        config: Config,
        publish: Callable[[str, dict], None] = lambda name, data: None,
        send_email: Callable[[dict], None] | None = None,
        now_ms: Callable[[], int] | None = None,
    ):
        self._conn = conn
        self._lock = lock
        self._config = config
        self._publish = publish
        self._send_email = send_email
        self._now_ms = now_ms
        self.quiet = False  # ignore changes while rules warm up
        self._active: dict[tuple[str, str], dict] = {}
        self._cleared: dict[tuple[str, str], int] = {}
        with lock:
            for row in conn.execute("SELECT * FROM events ORDER BY id"):
                event = to_dict(row)
                key = (event["source"], event["type"])
                if event["end_ms"] is None:
                    self._active[key] = event
                else:
                    self._cleared[key] = event["end_ms"]

    def get_active(self, source: str, kind: str) -> dict | None:
        return self._active.get((source, kind))

    def active_types(self, source: str) -> set[str]:
        return {kind for (s, kind) in self._active if s == source}

    def open(self, source: str, kind: str, result, start_ms: int, now: int) -> None:
        if self.quiet or (source, kind) in self._active:
            return
        event = self._insert(source, kind, result, start_ms, None)
        self._active[(source, kind)] = event
        self._announce(event, now)

    def one_shot(self, source: str, kind: str, result, start_ms: int, now: int) -> None:
        if not self.quiet:
            self._announce(self._insert(source, kind, result, start_ms, now), now)

    def escalate(self, source: str, kind: str, result, now: int) -> None:
        """Raise the severity of an active event; it never goes down."""
        event = self._active[(source, kind)]
        if self.quiet or rank(result.severity) <= rank(event["severity"]):
            return
        event.update(
            severity=result.severity,
            title=result.title,
            explanation=result.explanation,
            confidence=result.confidence,
            details=result.details,
            acknowledged=False,
        )
        self._update(
            event["id"],
            severity=result.severity,
            title=result.title,
            explanation=result.explanation,
            confidence=result.confidence,
            details_json=json.dumps({**result.details, "source": source}),
            acknowledged=0,
        )
        self._announce(event, now, escalated=True)

    def close(self, source: str, kind: str, end_ms: int, note: str = "") -> None:
        event = self._active.get((source, kind))
        if self.quiet or event is None:
            return
        del self._active[(source, kind)]
        event["end_ms"] = max(end_ms, event["start_ms"])
        event["explanation"] += note
        self._cleared[(source, kind)] = event["end_ms"]
        self._update(
            event["id"], end_ms=event["end_ms"], explanation=event["explanation"]
        )
        self._publish("alert_cleared" if is_alert(event) else "inference", event)

    def acknowledge(self, event_id: int) -> bool:
        with self._lock, self._conn:
            found = self._conn.execute(
                "UPDATE events SET acknowledged = 1 WHERE id = ?", (event_id,)
            ).rowcount
        for event in list(self._active.values()):
            if event["id"] == event_id:
                event["acknowledged"] = True
        return bool(found)

    def flag_solar_heating(self, source: str, start: int, end: int) -> None:
        if self.quiet:
            return
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE readings SET solar_heating_flag = 1
                   WHERE ts_ms >= ? AND ts_ms < ? AND raw_id IN
                     (SELECT id FROM raw_packets
                      WHERE ts_ms >= ? AND ts_ms < ? AND source = ?)""",
                (start, end, start, end, source),
            )

    # Where the rule engine got to, so that a restart resumes there.

    def save_position(self, source: str, minute: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO calibration VALUES (?, ?, ?, NULL)",
                (f"{source}.inference_position", minute, minute),
            )

    def load_position(self, conn: sqlite3.Connection, source: str) -> int | None:
        row = conn.execute(
            "SELECT value FROM calibration WHERE key = ?",
            (f"{source}.inference_position",),
        ).fetchone()
        return int(row["value"]) if row else None

    def _announce(self, event: dict, now: int, escalated: bool = False) -> None:
        name = "alert" if is_alert(event) else "inference"
        notify = is_alert(event) and self._may_notify(event, now, escalated)
        self._publish(name, {**event, "notify": notify})
        if notify and self._send_email:
            self._send_email(dict(event))

    def _may_notify(self, event: dict, now: int, escalated: bool) -> bool:
        if event["source"] == "hardware" and self._now_ms:
            if self._now_ms() - now > STALE_MS:
                return False  # a backlog, not news
        if escalated:
            return True
        cleared = self._cleared.get((event["source"], event["type"]))
        cooldown = self._config.alerts.cooldown_minutes * 60_000
        return cleared is None or not 0 <= now - cleared < cooldown

    def _insert(self, source, kind, result, start_ms, end_ms) -> dict:
        with self._lock, self._conn:
            event_id = self._conn.execute(
                """INSERT INTO events (type, severity, confidence, start_ms, end_ms,
                     title, explanation, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    kind, result.severity, result.confidence, start_ms, end_ms,
                    result.title, result.explanation,
                    json.dumps({**result.details, "source": source}),
                ),
            ).lastrowid
            row = self._conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return to_dict(row)

    def _update(self, event_id: int, **values) -> None:
        columns = ", ".join(f"{name} = ?" for name in values)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE events SET {columns} WHERE id = ?", (*values.values(), event_id)
            )

    def mark_emailed(self, event_id: int) -> None:
        self._update(event_id, notified_email=1)
