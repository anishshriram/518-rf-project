"""Optional alert emails. SMTP settings come from environment variables."""

import logging
import os
import queue
import smtplib
import ssl
import threading
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from server.config import Config

logger = logging.getLogger("uvicorn.error")
TIMEOUT_S = 10
LABELS = {"simulator": "[SIMULATED] ", "replay": "[REPLAYED] "}


def missing_settings(config: Config) -> list[str]:
    missing = [n for n in ("SMTP_HOST", "SMTP_FROM") if not os.environ.get(n)]
    return missing + ([] if config.alerts.email_to else ["alerts.email_to"])


def build_message(config: Config, event: dict, reading: dict | None) -> EmailMessage:
    local = datetime.fromtimestamp(
        event["start_ms"] / 1000, ZoneInfo(config.station.timezone)
    )
    label = LABELS.get(event.get("source"), "")
    lines = [
        f"{label}{event['title']} ({event['severity']})",
        "",
        event["explanation"],
        "",
        f"Station: {config.station.name}",
        f"Since: {local:%Y-%m-%d %H:%M %Z}",
    ]
    if reading:
        lines.append(
            f"Latest reading: {reading['temp_c']:.1f} °C, {reading['rh_pct']:.0f}% "
            f"humidity, light {reading['light_raw']:.0f}"
        )
    lines += ["", f"Dashboard: http://{config.server.host}:{config.server.port}/"]
    message = EmailMessage()
    message["Subject"] = f"[PIP Weather] {label}{event['title']} — {local:%Y-%m-%d %H:%M}"
    message["From"] = os.environ.get("SMTP_FROM", "")
    message["To"] = config.alerts.email_to
    message.set_content("\n".join(lines))
    return message


def send(message: EmailMessage) -> None:
    """Send over TLS: implicit on port 465, STARTTLS otherwise."""
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    context = ssl.create_default_context()
    if port == 465:
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT_S, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT_S)
    with server:
        if port != 465:
            server.ehlo_or_helo_if_needed()
            # Without TLS on offer this fails rather than expose a password.
            if user or server.has_extn("starttls"):
                server.starttls(context=context)
        if user:
            server.login(user, os.environ.get("SMTP_PASS", ""))
        server.send_message(message)


class EmailSender:
    """Sends queued emails from a background thread, so alerts never wait."""

    def __init__(
        self,
        config: Config,
        latest_reading: Callable[[], dict | None],
        on_sent: Callable[[int], None],
    ):
        self._config = config
        self._latest_reading = latest_reading
        self._on_sent = on_sent
        self._queue: queue.Queue[dict] = queue.Queue(100)
        threading.Thread(target=self._run, name="email", daemon=True).start()

    def put(self, event: dict) -> None:
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("Email queue is full; dropping %s", event["title"])

    def _run(self) -> None:
        while True:
            event = self._queue.get()
            try:
                send(build_message(self._config, event, self._latest_reading()))
                self._on_sent(event["id"])
            except Exception as exc:
                logger.error("Alert email failed: %s", exc)
