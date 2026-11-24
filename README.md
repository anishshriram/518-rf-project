# PIP Tag Outdoor Weather Station

Outdoor weather station and real-time dashboard built on a PIP RF sensor tag
and USB receiver. So far (milestone M4) packets are simulated or received,
collected, sent to the server, stored in SQLite and shown on a live dashboard.

## Setup

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml   # optional; set your tag ID and location
```

## Run

```bash
scripts/run_sim.sh normal 60     # simulator: scenario, speed
scripts/run_mac.sh               # real receiver on macOS
```

Open the dashboard at http://127.0.0.1:8000. The API is listed at
http://127.0.0.1:8000/docs.

Real data is stored in `data/weather.db`. The simulator uses
`data/simulator.db`, which is replaced on every run.

After changing units, offsets or the tag list, recompute stored readings with
`python -m server.rebuild`.

## Tests

```bash
pytest
```
