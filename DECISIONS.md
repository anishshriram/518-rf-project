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
