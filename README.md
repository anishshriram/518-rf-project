# PIP Tag Outdoor Weather Station

Outdoor weather station and real-time dashboard built on a PIP RF sensor tag
and USB receiver. So far (milestone M1) packets are simulated or received,
collected, sent to the server and stored in SQLite. There is no dashboard yet.

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

Data is stored in `data/weather.db`. The API is listed at
http://127.0.0.1:8000/docs.

## Tests

```bash
pytest
```
