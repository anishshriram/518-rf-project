"""User-editable overrides of the configuration (spec sections 19.7 and 20.4).

Overrides are stored in the database and applied to the running Config object,
so no restart is needed. The [server] and [collector] sections, with the ingest
token, are never readable or writable here.
"""

import copy
import json
import logging
import sqlite3
from dataclasses import asdict, fields

from server.config import ALERT_TYPES, Config, ConfigError, parse_config

logger = logging.getLogger("uvicorn.error")

SECTIONS = (
    "station",
    "tag",
    "units",
    "validation",
    "smoothing",
    "light",
    "home",
    "thresholds",
    "alerts",
)


def values(config: Config) -> dict:
    return {name: asdict(getattr(config, name)) for name in SECTIONS}


def overrides(conn: sqlite3.Connection) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in conn.execute("SELECT key, value_json FROM settings"):
        section, _, key = row["key"].partition(".")
        result.setdefault(section, {})[key] = json.loads(row["value_json"])
    return result


def merge(base: Config, changes: dict[str, dict]) -> Config:
    """Validated copy of base with the changes; raises ConfigError."""
    unknown = sorted(set(changes) - set(SECTIONS))
    if unknown:
        raise ConfigError(f"not editable: {', '.join(unknown)}")
    if not all(isinstance(v, dict) for v in changes.values()):
        raise ConfigError("each section must be an object")
    data = copy.deepcopy(asdict(base))
    for section, items in changes.items():
        data[section].update(items)
    return parse_config(data)


def apply(target: Config, source: Config) -> None:
    """Copy all editable values into the Config object in use."""
    for name in SECTIONS:
        section, new = getattr(target, name), getattr(source, name)
        for f in fields(section):
            setattr(section, f.name, copy.deepcopy(getattr(new, f.name)))


def apply_stored(conn: sqlite3.Connection, config: Config) -> None:
    """Apply saved overrides, ignoring any that are no longer valid."""
    base = copy.deepcopy(config)
    accepted: dict[str, dict] = {}
    for section, items in overrides(conn).items():
        for key, value in items.items():
            trial = copy.deepcopy(accepted)
            trial.setdefault(section, {})[key] = value
            try:
                merge(base, trial)
            except ConfigError as exc:
                logger.warning("Ignoring saved setting %s.%s: %s", section, key, exc)
            else:
                accepted = trial
    apply(config, merge(base, accepted))


def update(
    conn: sqlite3.Connection, base: Config, config: Config, changes: dict, now: int
) -> set[str]:
    """Validate, store and apply changes; returns the changed keys.

    `base` is the configuration from the file. A null value removes an override.
    """
    merge(base, {})  # sections must be valid before the changes are read
    if not isinstance(changes, dict) or not all(
        isinstance(v, dict) for v in changes.values()
    ):
        raise ConfigError("each section must be an object")
    wanted = overrides(conn)
    known = values(base)
    for section, items in changes.items():
        for key, value in items.items():
            if section not in known or key not in known[section]:
                raise ConfigError(f"unknown setting {section}.{key}")
            if value is None:
                wanted.get(section, {}).pop(key, None)
            else:
                wanted.setdefault(section, {})[key] = value
    merged = merge(base, wanted)

    before = values(config)
    with conn:
        for section, items in changes.items():
            for key, value in items.items():
                name = f"{section}.{key}"
                if value is None:
                    conn.execute("DELETE FROM settings WHERE key = ?", (name,))
                else:
                    conn.execute(
                        "INSERT OR REPLACE INTO settings VALUES (?, ?, ?)",
                        (name, json.dumps(value), now),
                    )
    apply(config, merged)
    after = values(config)
    return {
        f"{section}.{key}"
        for section in SECTIONS
        for key in after[section]
        if after[section][key] != before[section][key]
    }


def describe(base: Config, config: Config) -> dict:
    """The GET /api/settings response."""
    return {
        "values": values(config),
        "defaults": values(base),
        "alert_types": list(ALERT_TYPES),
    }
