# PIP Tag Outdoor Weather Station

Outdoor weather station and real-time dashboard built on a PIP RF sensor tag
and USB receiver. Only the scaffold and line parser exist so far (milestone M0);
other modules are placeholders.

## Setup

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.toml config.toml   # not read by any code yet
```

## Tests

```bash
pytest
```
