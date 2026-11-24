# Decisions

Choices made where the spec was ambiguous or the environment differed from it.

## M0

- **`dev` branch:** it did not exist locally or on the remote, so it was created
  from `main`.
- **Repository root:** the spec's `pip-weather/` directory is this repository's
  root, not a nested folder.
- **`data/` and `logs/`:** gitignored, so a fresh clone does not contain them.
  Code that writes there must create them.
- **`pytest.ini`:** added (not in the spec layout) so plain `pytest` can import
  the `collector` package.
- **`observed_noise.txt`:** holds only the two real data lines, with no comments.
  The real startup and summary lines are in `mixed_session.txt`.
- **Lines without `Data:`:** rejected with a warning. The CRC status may be cut
  off ("BAD CRC" to "B"), which `is_crc_ok()` would accept as good.
- **Lines cut after `Data:`:** returned as a partial packet with missing sensor
  values set to None. A cut inside the last number (`humidity: 45` to `4`)
  cannot be detected here, so the collector should discard an unterminated final
  line and ingest should reject packets with missing values.
- **Required fields:** TS, RX, TX and RSSI. A missing `Drop` becomes None.
- **Invalid numbers:** a value such as `1.2.3` rejects the whole line.
- **Unrecognised `####` lines:** return None with a DEBUG message.
- **`raw_line`:** stored without its line ending, otherwise unchanged.

## M1

- **Clock-skew check (section 14) applies to `hardware` only.** Simulated and
  replayed timestamps are not tied to the current time.
- **`delay_ms`:** optional extra field on `POST /api/ingest`. It says how long a
  batch waited in the collector, so data drained after an outage is not marked
  as clock skew.
- **Resent batches:** an exact copy of a stored raw line (same timestamp and
  source) is counted as rejected and not stored again. Delivery is at-least-once.
- **Duplicate and step checks** only compare readings from the same source. The
  step check looks back 10 minutes and uses at least 1 s as the interval.
- **Missing temp, humidity or light** rejects a reading (`missing_value`).
- **Refused batches** (HTTP 400, 413, 422) go to `<spool_path>.rejected`, since
  retrying could never succeed. Other errors, including 401, are retried.
- **Unterminated or unstorable lines** are discarded by the collector with a
  warning, but still recorded by `--record`.
- **Default `--source`** is `hardware` for stdin and `--spawn`. Piping simulator
  output therefore needs `--source simulator`, as `run_sim.sh` does.
- **Configuration:** `PIP_CONFIG` selects a file. An explicitly named file must
  exist; otherwise `config.toml` is used if present, else defaults. Unknown keys
  are errors.
- **Scripts** start `python -m server.app` so that host and port come from the
  config, and use `gstdbuf` or `stdbuf`, whichever is installed.
- **Simulator extras:** `--duration`, `--light-cap`, `--config`, and the
  `rssi_shift` scenario, which section 10.3 lists but section 10.1 omits.
  `tag_dies` stops 30 simulated minutes after the start.
- **Deferred:** derived metrics (M3), SSE and static files (M2), and the
  background jobs of section 12.1 (with the milestones that use them).
- **Extra test files:** `test_config.py`, `test_simulator.py`,
  `test_collector.py`.
