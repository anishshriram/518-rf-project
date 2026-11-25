"""Validate, convert and store incoming packets (spec sections 12.2 and 14)."""

import sqlite3
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from server import db, derive, sun
from server.config import Config, ValidationConfig

CLOCK_SKEW_MS = 10 * 60 * 1000
STEP_WINDOW_MS = 10 * 60 * 1000
RH_CLIP_MARGIN = 3.0
SIGN_BIT = 32768

Int64 = Annotated[int, Field(ge=-(2**63), lt=2**63)]
Number = Annotated[float, Field(allow_inf_nan=False)]


class PacketIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts_ms: Annotated[int, Field(ge=0, lt=2**53)]
    drop: Int64 | None = None
    rx_id: Int64
    tx_id: Int64
    rssi_dbm: Number
    crc_ok: bool
    crc_text: str
    data_hex: str
    data_len: Int64
    light_raw: Number | None = None
    temp_raw: Number | None = None
    humidity_raw: Number | None = None
    raw_line: str


class SummaryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packets: Int64
    ok_pct: Number
    raw_line: str


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["hardware", "simulator", "replay"]
    packets: list[PacketIn] = Field(default=[], max_length=1000)
    summaries: list[SummaryIn] = Field(default=[], max_length=1000)
    # How long the batch waited in the collector, so a drained backlog is not
    # mistaken for clock skew.
    delay_ms: Annotated[int, Field(ge=0, lt=2**53)] = 0


def ingest(
    conn: sqlite3.Connection,
    config: Config,
    request: IngestRequest,
    now_ms: int | None = None,
) -> tuple[dict[str, int], list[dict]]:
    """Store one batch in a single transaction.

    Returns the outcome counts and the stored readings that were not rejected.
    """
    if now_ms is None:
        now_ms = time.time_ns() // 1_000_000
    counts = {"accepted": 0, "rejected": 0, "bad_crc": 0, "foreign": 0}
    readings: list[dict] = []
    with conn:
        for packet in request.packets:
            outcome = _ingest_packet(
                conn, config, request, packet, now_ms, readings
            )
            counts[outcome] += 1
        for summary in request.summaries:
            db.insert_receiver_stats(
                conn, {"received_ms": now_ms, **summary.model_dump()}
            )
    return counts, readings


def _ingest_packet(conn, config, request, packet, now_ms, readings) -> str:
    # Already stored: the collector resent a batch whose response was lost.
    if db.raw_exists(conn, packet.ts_ms, packet.raw_line, request.source):
        return "rejected"

    values = packet.model_dump()
    values["drop_count"] = values.pop("drop")
    raw_id = db.insert_raw(
        conn, {**values, "received_ms": now_ms, "source": request.source}
    )
    # Simulated and replayed timestamps are not tied to the current time.
    skew = abs(packet.ts_ms + request.delay_ms - now_ms) > CLOCK_SKEW_MS
    outcome, reading = store_reading(
        conn,
        config,
        packet,
        raw_id=raw_id,
        source=request.source,
        received_ms=now_ms,
        clock_skew=skew and request.source == "hardware",
    )
    if outcome == "accepted":
        readings.append(reading)
    return outcome


def store_reading(
    conn: sqlite3.Connection,
    config: Config,
    packet: PacketIn,
    *,
    raw_id: int,
    source: str,
    received_ms: int,
    clock_skew: bool,
) -> tuple[str, dict | None]:
    """Turn a stored raw packet into a reading if it qualifies.

    Returns the outcome and the reading, which is None if none was stored.
    """
    if not packet.crc_ok:
        return "bad_crc", None
    allowed = config.tag.allowed_tx_ids
    if allowed and packet.tx_id not in allowed:
        return "foreign", None

    temp_c, rh_pct = convert(
        config, packet.temp_raw, packet.humidity_raw, source
    )
    quality, reason, rh_pct = _assess(
        conn, config, packet, source, temp_c, rh_pct, clock_skew
    )
    derived = {} if quality == "rejected" else derive.instant(temp_c, rh_pct)
    reading = {
        "ts_ms": packet.ts_ms,
        "tx_id": packet.tx_id,
        "rssi_dbm": packet.rssi_dbm,
        "temp_c": temp_c,
        "rh_pct": rh_pct,
        "light_raw": packet.light_raw,
        "dew_point_c": derived.get("dew_point_c"),
        "heat_index_c": derived.get("heat_index_c"),
        "abs_humidity_gm3": derived.get("abs_humidity_gm3"),
        "quality": quality,
    }
    elevation = sun.elevation(config.station, packet.ts_ms)
    reading_id = db.insert_reading(
        conn,
        {
            **reading,
            "raw_id": raw_id,
            "reject_reason": reason,
            "sun_elev_deg": elevation,
            "is_daylight": elevation > 0,
        },
    )
    published = {
        **reading,
        **derived,
        "id": reading_id,
        "light_norm": None,
        "source": source,
        "received_ms": received_ms,
    }
    return ("rejected" if quality == "rejected" else "accepted"), published


def convert(
    config: Config, temp_raw: float | None, humidity_raw: float | None, source: str
):
    """Convert raw sensor values to (temp_c, rh_pct), applying scales and offsets.

    The simulator already prints degrees and percent, so it is not scaled.
    """
    units = config.units
    scaled = source != "simulator"
    temp_c = rh_pct = None
    if temp_raw is not None:
        # pip_sense prints the tag's signed 16-bit number as unsigned, over 10.
        if temp_raw >= SIGN_BIT / 10:
            temp_raw -= 2 * SIGN_BIT / 10
        if scaled:
            temp_raw *= units.temp_scale
        if units.temp_input_unit == "F":
            temp_raw = derive.f_to_c(temp_raw)
        temp_c = temp_raw + units.temp_offset_c
    if humidity_raw is not None:
        scale = units.humidity_scale if scaled else 1.0
        rh_pct = humidity_raw * scale + units.humidity_offset_pct
    return temp_c, rh_pct


def _assess(conn, config, packet, source, temp_c, rh_pct, clock_skew):
    """Return (quality, reason, possibly clipped rh_pct)."""
    limits = config.validation
    suspect = ["clock_skew"] if clock_skew else []

    if db.reading_exists(conn, packet.tx_id, packet.ts_ms, source):
        return "rejected", "duplicate", rh_pct
    if temp_c is None or rh_pct is None or packet.light_raw is None:
        return "rejected", "missing_value", rh_pct

    if not limits.temp_min_c <= temp_c <= limits.temp_max_c:
        return "rejected", "temp_range", rh_pct
    if not limits.rh_min <= rh_pct <= limits.rh_max + RH_CLIP_MARGIN:
        return "rejected", "rh_range", rh_pct
    if rh_pct > limits.rh_max:
        rh_pct = limits.rh_max
        suspect.append("rh_over_100")

    previous = db.previous_good(
        conn, packet.tx_id, packet.ts_ms, source, STEP_WINDOW_MS
    )
    if previous is not None and _is_spike(limits, previous, packet, temp_c, rh_pct):
        return "rejected", "spike", rh_pct

    if suspect:
        return "suspect", ",".join(suspect), rh_pct
    return "good", None, rh_pct


def _is_spike(
    limits: ValidationConfig, previous, packet: PacketIn, temp_c: float, rh_pct: float
) -> bool:
    # At least 1 s, so closely spaced packets do not turn noise into a spike.
    dt = max((packet.ts_ms - previous["ts_ms"]) / 1000, 1.0)
    return (
        abs(temp_c - previous["temp_c"]) / dt > limits.max_temp_step_c_per_s
        or abs(rh_pct - previous["rh_pct"]) / dt > limits.max_rh_step_per_s
    )
