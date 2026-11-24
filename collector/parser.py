"""Parser for the text lines printed by pip_sense.v2 (spec section 9).

Pure functions with no I/O other than logging. A malformed line never raises:
it is logged and reported as None.
"""

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Packet:
    """One received radio packet, valid or corrupted."""

    ts_ms: int
    drop: int | None
    rx_id: int
    tx_id: int
    rssi_dbm: float
    crc_ok: bool
    crc_text: str  # literal status text, e.g. "BAD CRC", "OK", ""
    data_hex: str  # lowercase bytes separated by single spaces
    data_len: int
    light_raw: float | None
    temp_raw: float | None
    humidity_raw: float | None
    raw_line: str


@dataclass
class Summary:
    """Periodic receiver statistics ("#### Received ... ####")."""

    packets: int
    ok_pct: float
    raw_line: str


ParseResult = Packet | Summary | None  # None = startup/blank/unrecognised line

_SUMMARY_RE = re.compile(r"Received\s+(\d+)\s+packets.*?\(([\d.]+)%\s*OK\)")
_FIELD_RE = re.compile(r"(\w+):\s*(-?[\d.]+)")
_DATA_RE = re.compile(r"Data:\s*([0-9a-fA-F]{2}(?:\s+[0-9a-fA-F]{2})*)")
_LIGHT_RE = re.compile(r"light:\s*(-?[\d.]+)")
_TEMP_RE = re.compile(r"temp:\s*(-?[\d.]+)")
_HUMIDITY_RE = re.compile(r"humidity:\s*(-?[\d.]+)")


def is_crc_ok(text: str) -> bool:
    """Decide whether a CRC status text means the packet is intact.

    The text of a good packet has not been observed yet, so anything that does
    not contain "BAD" counts as good, including an empty status. Downstream
    code must also check the TX ID against allowed_tx_ids.
    """
    return "BAD" not in text.upper()


def parse_line(line: str) -> ParseResult:
    """Parse one line of pip_sense output. Never raises."""
    try:
        return _parse(line)
    except Exception as exc:
        logger.warning("Could not parse line (%s): %r", exc, line)
        return None


def _parse(line: str) -> ParseResult:
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    if line.startswith("####"):
        return _parse_summary(line)
    if "TS:" not in line:
        logger.debug("Skipping non-data line: %r", line)
        return None
    return _parse_packet(line)


def _parse_summary(line: str) -> Summary | None:
    match = _SUMMARY_RE.search(line)
    if match is None:
        logger.debug("Skipping unrecognised summary line: %r", line)
        return None
    return Summary(
        packets=int(match.group(1)), ok_pct=float(match.group(2)), raw_line=line
    )


def _parse_packet(line: str) -> Packet:
    left, _, right = line.partition("|")

    # Without the "Data:" marker the CRC status may itself be cut off ("BAD CRC"
    # -> "B"), which is_crc_ok() would accept. Treat such a line as unusable.
    head, marker, _ = left.partition("Data:")
    if not marker:
        raise ValueError("no Data: field")

    fields = {m.group(1): m for m in _FIELD_RE.finditer(head)}
    missing = [key for key in ("TS", "RX", "TX", "RSSI") if key not in fields]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")

    crc_text = head[fields["RSSI"].end():].strip()
    data_match = _DATA_RE.search(left)
    data_bytes = data_match.group(1).lower().split() if data_match else []

    return Packet(
        ts_ms=int(fields["TS"].group(2)),
        drop=int(fields["Drop"].group(2)) if "Drop" in fields else None,
        rx_id=int(fields["RX"].group(2)),
        tx_id=int(fields["TX"].group(2)),
        rssi_dbm=float(fields["RSSI"].group(2)),
        crc_ok=is_crc_ok(crc_text),
        crc_text=crc_text,
        data_hex=" ".join(data_bytes),
        data_len=len(data_bytes),
        light_raw=_optional_float(_LIGHT_RE, right),
        temp_raw=_optional_float(_TEMP_RE, right),
        humidity_raw=_optional_float(_HUMIDITY_RE, right),
        raw_line=line,
    )


def _optional_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    return float(match.group(1)) if match else None
