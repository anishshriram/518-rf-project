"""Hardware check: read pip_sense output on stdin and report PASS or FAIL.

Usage: stdbuf -o0 PIPtagCode/ReceiverCode/pip_sense.v2 l l \
         | python -m scripts.probe_tag [--seconds 60] [--tag 3416]

Decodes the data bytes directly (header, light, temperature x16, humidity x16,
hourly battery), independently of the numbers pip_sense prints.
"""

import argparse
import queue
import sys
import threading
import time
from statistics import fmean, median

from collector.parser import Packet, parse_line


def decode(data_hex: str):
    """Return (light, temp_c, rh_pct, battery_mv) or None for another layout."""
    data = bytes.fromhex(data_hex)
    if len(data) < 6 or data[0] & 0x0C != 0x0C or data[0] & 0x03:
        return None
    temp = int.from_bytes(data[2:4], "big", signed=True) / 16
    rh = int.from_bytes(data[4:6], "big", signed=True) / 16
    battery = data[0] & 0x40 and len(data) >= 10
    return data[1], temp, rh, int.from_bytes(data[6:8], "big") if battery else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--tag", type=int, default=3416)
    args = parser.parse_args()

    lines: queue.Queue = queue.Queue()

    def read():
        for line in sys.stdin:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    end = time.monotonic() + args.seconds
    good, rows, others, bad = [], [], set(), 0
    while (left := end - time.monotonic()) > 0:
        try:
            line = lines.get(timeout=left)
        except queue.Empty:
            break
        if line is None:
            break
        packet = parse_line(line)
        if not isinstance(packet, Packet):
            if line.strip() and not line.startswith("#"):
                print("receiver:", line.strip())
            continue
        if not packet.crc_ok:
            bad += 1
        elif packet.tx_id != args.tag:
            others.add(packet.tx_id)
        else:
            good.append(packet)
            values = decode(packet.data_hex)
            if values:
                rows.append(values)
                light, temp, rh, battery = values
                extra = f"  battery {battery} mV" if battery else ""
                print(
                    f"tag {packet.tx_id}  {temp:6.2f} C ({temp * 1.8 + 32:5.1f} F)"
                    f"  {rh:5.1f} %RH  light {light:3d}"
                    f"  {packet.rssi_dbm:6.1f} dBm{extra}",
                    flush=True,
                )

    checks = [("good packets from the tag", bool(good), f"{len(good)}")]
    if good:
        # The receiver releases stored packets in a burst at the start.
        times = sorted({p.ts_ms for p in good})
        gaps = [(b - a) / 1000 for a, b in zip(times, times[1:]) if b - a > 100]
        if gaps:
            interval = median(gaps)
            expected = round((times[-1] - times[0]) / 1000 / interval) + 1
            share = min(1.0, len(times) / expected)
            checks += [
                ("beacon interval", interval <= 15, f"{interval:.2f} s"),
                ("packets received", share >= 0.9, f"{share:.0%} of expected"),
            ]
        rssi = fmean(p.rssi_dbm for p in good)
        checks += [
            ("signal", rssi > -95, f"{rssi:.1f} dBm average"),
            ("data layout", len(rows) == len(good), f"{len(rows)}/{len(good)}"),
        ]
    if rows:
        temps = [r[1] for r in rows]
        rhs = [r[2] for r in rows]
        lights = [r[0] for r in rows]
        checks += [
            ("temperature plausible", all(-40 <= t <= 60 for t in temps),
             f"{min(temps):.2f} to {max(temps):.2f} C"),
            ("humidity plausible", all(0 <= r <= 103 for r in rhs),
             f"{min(rhs):.1f} to {max(rhs):.1f} %"),
        ]
        print(f"\nlight range: {min(lights)} to {max(lights)} (0 dark, 255 saturated)")
    print(f"corrupt packets: {bad}; other tags heard: {sorted(others) or 'none'}")
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
