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

## M2

- **Simulator database:** `run_sim.sh` uses `data/simulator.db` and replaces it on
  each run. Simulated time overlaps between runs, and simulated rows must not
  mix with real ones. `PIP_DB_PATH` and `PIP_SPOOL_PATH` override the paths.
- **History shows one source only:** that of the latest reading.
- **Window end:** the clock for hardware data, the newest reading for simulated
  or replayed data.
- **Extra `5s` resolution:** the spec's "up to 2 h is raw" would give 7200 points,
  above its limit of 2000. `auto` uses raw up to 30 min and 5 s up to 2 h.
  Explicit `raw` is limited to 6 h.
- **Buckets are computed on request** from `readings`, so they cannot disagree
  with live data. Rollup tables come with the milestones that need them. `1d`
  buckets are UTC days for now.
- **Extra response fields:** `/api/history` adds `resolution_ms`, `source`, `n`,
  `suspect`, `last_id` and `gap_threshold_ms`. `night_intervals` stays empty
  until M4. `/api/current` has only the fields available so far, plus status.
- **Light** is charted as raw values until calibration exists (M4).
- **RSSI** has its own chart. Packet success is not put on a second axis, since
  two scales on one plot suggest a relation that is not in the data.
- **Chart.js is served locally** rather than from a CDN, so the page works offline.
- **Deferred:** min/max bands, smoothing and suspect toggles, night shading and
  event markers.

## M3

- **Minute averages are computed on request.** `agg_minute` has no source column,
  so a rollup table could mix simulated and real data. The tables come with the
  daily summaries (M6).
- **Reference time** for trends and "today" is the newest reading, so simulated
  time works. "Today" is the local calendar day.
- **Trends:** `trends` covers 30 min (shown on the cards) and `trends_60m` covers
  60 min. Both need data in at least half of their minutes. The arrow limits for
  humidity (2 and 10 points per hour) are ours; the spec only gives temperature.
- **Heat index** is null below 80 °F, where feels-like equals the temperature.
- **`/api/current`** also returns `cdd_so_far` and `gdd_so_far`.
- **Recommendations:** only window condensation is shown. The laundry and
  open-window formulas are tested but need clearness (M4) and alerts (M5).
- **Open windows, "evening":** noon to 23:00 counts, as does any time the sun is up.
- **Dew point line** is dashed in the humidity colour; the other candidate hues
  were too close to the temperature colour.
- **`server.rebuild`** parses raw lines again and replaces readings. It keeps
  clock-skew verdicts, because the delivery delay is not stored.
- **Degree-day totals** per month and season come with the daily summaries (M6).

## M4

- **Calibration per source:** keys are stored as `simulator.light_k` and so on, so
  simulated light never calibrates the real sensor.
- **Normalised light is computed when read,** not stored, so all history uses one
  scale. The `readings.light_norm` column stays empty. `sun_elev_deg` and
  `is_daylight` are stored.
- **"Enough data" to calibrate:** readings spanning 24 h, with at least 120 minutes
  of sun above 15 degrees and 60 minutes of night. A scale hardly above the night
  noise (a covered sensor) is refused. In places where the winter sun stays
  below 15 degrees, light stays uncalibrated.
- **Recalibration** runs when the data has advanced an hour, so simulated time
  works too. Saturated samples are left out of the scale.
- **Until calibrated** the light chart shows raw values, since a scale that keeps
  changing would distort the curve. The API still gives the spec's fallback.
- **Sky rules, uncovered case:** clearness of 0.75 or more with spread from 0.08 to
  0.12 counts as partly cloudy. At least 8 of the 15 minutes need data.
- **Saturated sensor:** shown as "very bright", because clearness is then only a
  lower bound.
- **Minimum night sigma** is 1 raw unit, since a sensor that reads 0 all night has
  no spread.
- **Night shading** uses a small plugin of our own instead of
  chartjs-plugin-annotation.
- **Deferred to M6:** detected sunrise and sunset, and daylight and sunshine hours.
  Laundry and open-window advice wait for the M5 alerts.
