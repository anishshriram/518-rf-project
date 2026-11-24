CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

-- Every parsed line, good or bad. Source of truth for reprocessing.
CREATE TABLE IF NOT EXISTS raw_packets (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms         INTEGER NOT NULL,          -- from TS field
  received_ms   INTEGER NOT NULL,          -- server receive time
  rx_id         INTEGER,
  tx_id         INTEGER,
  drop_count    INTEGER,
  rssi_dbm      REAL,
  crc_ok        INTEGER NOT NULL,          -- 0/1
  crc_text      TEXT,
  data_hex      TEXT,
  data_len      INTEGER,
  light_raw     REAL,
  temp_raw      REAL,
  humidity_raw  REAL,
  raw_line      TEXT,
  source        TEXT                       -- 'hardware' | 'simulator' | 'replay'
);
CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_packets(ts_ms);
CREATE INDEX IF NOT EXISTS idx_raw_tx_ts ON raw_packets(tx_id, ts_ms);

-- Clean, converted, per-packet readings for the allowed tag(s).
CREATE TABLE IF NOT EXISTS readings (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  raw_id          INTEGER REFERENCES raw_packets(id),
  ts_ms           INTEGER NOT NULL,
  tx_id           INTEGER NOT NULL,
  rssi_dbm        REAL,
  temp_c          REAL,
  rh_pct          REAL,
  light_raw       REAL,
  light_norm      REAL,        -- 0..1 after calibration (nullable until calibrated)
  dew_point_c     REAL,
  heat_index_c    REAL,
  abs_humidity_gm3 REAL,
  sun_elev_deg    REAL,
  is_daylight     INTEGER,
  quality         TEXT NOT NULL,   -- 'good' | 'suspect' | 'rejected'
  reject_reason   TEXT,
  solar_heating_flag INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts_ms);

-- One row per minute (UTC minute start), good readings only.
CREATE TABLE IF NOT EXISTS agg_minute (
  minute_ms     INTEGER PRIMARY KEY,
  n             INTEGER,
  temp_avg REAL, temp_min REAL, temp_max REAL,
  rh_avg REAL, rh_min REAL, rh_max REAL,
  dew_point_avg REAL,
  heat_index_avg REAL,
  light_avg REAL, light_max REAL, light_norm_avg REAL, light_norm_std REAL,
  rssi_avg REAL, rssi_min REAL, rssi_max REAL,
  clearness_avg REAL,
  expected_packets INTEGER, received_packets INTEGER
);

CREATE TABLE IF NOT EXISTS agg_hour (  -- same columns as agg_minute, keyed by hour_ms
  hour_ms INTEGER PRIMARY KEY,
  n INTEGER,
  temp_avg REAL, temp_min REAL, temp_max REAL,
  rh_avg REAL, rh_min REAL, rh_max REAL,
  dew_point_avg REAL, heat_index_avg REAL,
  light_avg REAL, light_max REAL, light_norm_avg REAL, light_norm_std REAL,
  rssi_avg REAL, rssi_min REAL, rssi_max REAL,
  clearness_avg REAL,
  expected_packets INTEGER, received_packets INTEGER
);

-- Local-calendar-day summaries (station timezone).
CREATE TABLE IF NOT EXISTS daily_summary (
  source            TEXT NOT NULL,
  local_date        TEXT NOT NULL,      -- 'YYYY-MM-DD'
  temp_min_c REAL, temp_min_time_ms INTEGER,
  temp_max_c REAL, temp_max_time_ms INTEGER,
  temp_avg_c REAL,
  rh_min REAL, rh_max REAL, rh_avg REAL,
  dew_point_avg_c REAL,
  heat_index_max_c REAL,
  hdd REAL, cdd REAL, gdd REAL,
  daylight_hours_measured REAL,
  daylight_hours_astronomical REAL,
  bright_sun_hours REAL,
  sunrise_detected_ms INTEGER, sunset_detected_ms INTEGER,
  sunrise_astro_ms INTEGER, sunset_astro_ms INTEGER,
  sky_summary TEXT,                     -- 'mostly clear' | 'partly cloudy' | 'mostly cloudy' | 'overcast'
  packet_success_pct REAL,
  data_coverage_pct REAL,               -- fraction of the day with data
  notes TEXT,                           -- e.g. "front passed 14:20"
  PRIMARY KEY (source, local_date)
);

CREATE TABLE IF NOT EXISTS events (      -- inferences and alerts
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  type          TEXT NOT NULL,           -- see §17/§18 type keys
  severity      TEXT NOT NULL,           -- 'info' | 'watch' | 'warning' | 'critical'
  confidence    TEXT,                    -- 'low' | 'medium' | 'high'
  start_ms      INTEGER NOT NULL,
  end_ms        INTEGER,                 -- NULL while active
  title         TEXT NOT NULL,
  explanation   TEXT NOT NULL,           -- human-readable reasons with numbers
  details_json  TEXT,                    -- the numbers used
  acknowledged  INTEGER DEFAULT 0,
  notified_email INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ms);

CREATE TABLE IF NOT EXISTS receiver_stats (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_ms INTEGER, packets INTEGER, ok_pct REAL, raw_line TEXT
);

CREATE TABLE IF NOT EXISTS calibration (
  key TEXT PRIMARY KEY,                  -- 'light_k', 'light_saturation', 'night_baseline', ...
  value REAL, updated_ms INTEGER, details_json TEXT
);

CREATE TABLE IF NOT EXISTS settings (    -- user-editable overrides of config (from settings page)
  key TEXT PRIMARY KEY, value_json TEXT, updated_ms INTEGER
);
