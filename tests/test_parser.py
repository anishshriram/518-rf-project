"""Tests for collector.parser (spec section 9.3)."""

import logging
import re
from dataclasses import replace
from pathlib import Path

import pytest

from collector.parser import Packet, Summary, is_crc_ok, parse_line

FIXTURES = Path(__file__).parent / "fixtures"

STARTUP_LINES = [
    "parameters are ac:3",
    "server ipl port 0",
    "Connected to USB Tag Reader.",
    "New pipsqueak opened.",
    "Connected to the GRAIL aggregation server.",
]


def read_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


@pytest.fixture
def noise_lines():
    return read_fixture("observed_noise.txt")


@pytest.fixture
def good_line():
    lines = read_fixture("hypothetical_good.txt")
    return next(line for line in lines if not line.startswith("#"))


# Real observed noise


def test_first_real_noise_line(noise_lines):
    assert parse_line(noise_lines[0]) == Packet(
        ts_ms=1795036435411,
        drop=0,
        rx_id=191,
        tx_id=13172869,
        rssi_dbm=-109.0,
        crc_ok=False,
        crc_text="BAD CRC",
        data_hex="f5",
        data_len=1,
        light_raw=0.0,
        temp_raw=0.0,
        humidity_raw=0.0,
        raw_line=noise_lines[0],
    )


def test_second_real_noise_line(noise_lines):
    assert parse_line(noise_lines[1]) == Packet(
        ts_ms=1795036853059,
        drop=0,
        rx_id=191,
        tx_id=8912822,
        rssi_dbm=-108.5,
        crc_ok=False,
        crc_text="BAD CRC",
        data_hex="45 d4 16 d1 1d 9a 16 76 e0 e0 86 97 ae 58 c0 24 6c 8c e5 f0",
        data_len=20,
        light_raw=212.0,
        temp_raw=584.10,
        humidity_raw=7578.0,
        raw_line=noise_lines[1],
    )


def test_noise_fixture_keeps_observed_whitespace(noise_lines):
    assert len(noise_lines) == 2
    assert noise_lines[0].startswith("TS:1795036435411        Drop:0  RX:191  TX:")
    assert "RSSI:-109.00\tBAD CRC\tData: f5  | light" in noise_lines[0]
    assert noise_lines[1].startswith("TS:1795036853059\tDrop:0\tRX:191\tTX:")


# Whitespace and line endings


@pytest.mark.parametrize("separator", ["\t", " ", "    ", " \t  \t"])
@pytest.mark.parametrize("index", [0, 1])
def test_any_whitespace_between_fields(noise_lines, index, separator):
    original = noise_lines[index]
    line = re.sub(r"\s+", separator, original)
    expected = replace(parse_line(original), raw_line=line)
    # Whitespace inside the status text is kept literally.
    expected.crc_text = f"BAD{separator}CRC"
    assert parse_line(line) == expected


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
def test_line_endings_are_stripped(noise_lines, ending):
    assert parse_line(noise_lines[1] + ending) == parse_line(noise_lines[1])


def test_windows_line_endings_on_summary_and_startup():
    result = parse_line("#### Received 001 packets in 5 seconds. (0.00% OK) ####\r\n")
    assert isinstance(result, Summary)
    assert not result.raw_line.endswith("\r")
    assert parse_line("New pipsqueak opened.\r\n") is None


# Lines that are not packets


@pytest.mark.parametrize("line", STARTUP_LINES)
def test_startup_lines_are_skipped(line):
    assert parse_line(line) is None


@pytest.mark.parametrize("line", ["", "\n", "\r\n", "   \t "])
def test_blank_lines_are_skipped(line):
    assert parse_line(line) is None


@pytest.mark.parametrize(
    ("line", "packets"),
    [
        ("#### Received 019 packets in 1795036435 seconds. (0.00% OK) ####", 19),
        ("#### Received 001 packets in 1795036853 seconds. (0.00% OK) ####", 1),
    ],
)
def test_summary_lines(line, packets):
    assert parse_line(line) == Summary(packets=packets, ok_pct=0.0, raw_line=line)


def test_summary_with_nonzero_percentage():
    result = parse_line("#### Received 060 packets in 60 seconds. (98.33% OK) ####")
    assert result.packets == 60
    assert result.ok_pct == 98.33


def test_unrecognised_summary_is_skipped():
    assert parse_line("#### not a real summary ####") is None


# Good packets (hypothetical format) and CRC status


def test_hypothetical_good_line(good_line):
    assert parse_line(good_line) == Packet(
        ts_ms=1795100000000,
        drop=0,
        rx_id=191,
        tx_id=3378,
        rssi_dbm=-62.5,
        crc_ok=True,
        crc_text="OK",
        data_hex="0d 32 00 7a 09 c4 17 70 00 00",
        data_len=10,
        light_raw=140.0,
        temp_raw=21.35,
        humidity_raw=45.0,
        raw_line=good_line,
    )


def test_hypothetical_fixture_is_labelled():
    assert "HYPOTHETICAL" in read_fixture("hypothetical_good.txt")[0]


def test_absent_crc_status_counts_as_ok(good_line):
    packet = parse_line(good_line.replace("\tOK\t", "\t"))
    assert packet.crc_text == ""
    assert packet.crc_ok is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [("BAD CRC", False), ("bad crc", False), ("OK", True), ("", True)],
)
def test_is_crc_ok(text, expected):
    assert is_crc_ok(text) is expected


# Field values


def test_negative_temperature(good_line):
    packet = parse_line(good_line.replace("temp: 21.35", "temp: -5.25"))
    assert packet.temp_raw == -5.25


def test_leading_zeros_in_tx_id(good_line):
    packet = parse_line(good_line.replace("TX:3378", "TX:03378"))
    assert packet.tx_id == 3378


def test_missing_drop_is_none(good_line):
    assert parse_line(good_line.replace("Drop:0\t", "")).drop is None


def test_uppercase_hex_is_normalised(good_line):
    packet = parse_line(good_line.replace("0d 32 00", "0D\t32   00"))
    assert packet.data_hex == "0d 32 00 7a 09 c4 17 70 00 00"


def test_empty_data(good_line):
    packet = parse_line(good_line.replace("0d 32 00 7a 09 c4 17 70 00 00", ""))
    assert packet.data_hex == ""
    assert packet.data_len == 0


def test_missing_sensor_section(good_line):
    packet = parse_line(good_line.partition("|")[0])
    assert packet.tx_id == 3378
    assert packet.light_raw is None
    assert packet.temp_raw is None
    assert packet.humidity_raw is None


# Truncated and malformed input


def test_truncation_at_every_position_never_crashes(noise_lines, good_line):
    for line in [*noise_lines, good_line]:
        for length in range(len(line)):
            result = parse_line(line[:length])
            assert result is None or isinstance(result, Packet)


def test_truncated_noise_is_never_reported_as_good(noise_lines):
    for line in noise_lines:
        for length in range(len(line)):
            result = parse_line(line[:length])
            assert result is None or result.crc_ok is False


def test_truncated_in_data_keeps_complete_bytes(noise_lines):
    line = noise_lines[1]
    packet = parse_line(line[: line.index("16 d1") + 1])
    assert packet.data_hex == "45 d4"
    assert packet.data_len == 2
    assert packet.temp_raw is None


@pytest.mark.parametrize(
    "line",
    [
        "TS:not-a-number",
        "TS:1795036853059",
        "TS:1.2.3\tDrop:0\tRX:191\tTX:1\tRSSI:-60.0\tOK\tData: 00  | light: 0",
        "TS:1\tDrop:0\tRX:191\tTX:1\tRSSI:-60.0\tOK\tData: 00  | temp: 1.2.3",
    ],
)
def test_malformed_data_line_warns_with_raw_line(caplog, line):
    with caplog.at_level(logging.WARNING, logger="collector.parser"):
        assert parse_line(line) is None
    assert repr(line) in caplog.text


def test_mixed_session():
    results = [parse_line(line) for line in read_fixture("mixed_session.txt")]
    packets = [r for r in results if isinstance(r, Packet)]
    summaries = [r for r in results if isinstance(r, Summary)]

    assert [p.tx_id for p in packets] == [13172869, 3378, 8912822]
    assert [p.crc_ok for p in packets] == [False, True, False]
    assert [s.packets for s in summaries] == [19, 1]


# Real good packets from tag 3416


def test_real_good_lines():
    packets = [parse_line(line) for line in read_fixture("observed_good.txt")]
    assert len(packets) == 4
    for packet in packets:
        assert (packet.crc_ok, packet.crc_text) == (True, "CRC")
        assert (packet.rx_id, packet.tx_id) == (191, 3416)
    # Once an hour the tag appends battery voltage and energy used.
    assert [p.data_len for p in packets] == [6, 6, 6, 10]
    battery = packets[-1]
    assert (battery.light_raw, battery.temp_raw) == (202.0, 32.3)
    assert battery.humidity_raw == 431.0
