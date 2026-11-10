#!/usr/bin/env python3
"""A log of what every reading has been, kept in SQLite.

The car already knows what the battery is doing now. What it could not answer
is anything of the form "was it like this yesterday" -- whether the bank came
back up overnight, whether a cell has been drifting for a week, whether the
solar did anything at all on Tuesday. That needs history, and history needs a
file.

Narrow rather than wide -- one row per reading per moment, rather than a
column per reading -- because the set of parameters is not fixed. A second
battery, another Victron, the car's own bus: each adds names, not schema.

Two things keep it small enough to live on an SD card in a car:

    a deadband   a reading is written when it has moved by more than its own
                 threshold, not on every poll. A pack resting at 13.30 V for
                 an hour is one row, not a hundred and twenty.
    a heartbeat  and written anyway every so often, so a flat line is
                 recorded as flat rather than as absence.

Usage:
    history.py --show soc          # what the state of charge has been
    history.py --names             # what is being recorded
    history.py --prune
"""

from __future__ import annotations

import os
import sqlite3
import time

PATH = "/var/lib/comand/history.db"

# How much a reading must move before it is worth another row. Chosen per
# parameter rather than as one number: 0.05 is noise on a pack voltage and a
# real change on a cell.
DEADBAND = {
    "soc": 0.5,
    "volts": 0.02,
    "amps": 0.3,
    "capacity_ah": 0.5,
    "temp_hi": 1.0,
    "temp_lo": 1.0,
    "cell_spread": 0.002,
    "solar.watts": 5.0,
    "solar.amps": 0.3,
    "solar.volts": 0.02,
    "solar.yield_kwh": 0.01,
    "charger.vin": 0.05,
    "charger.vout": 0.02,
    # Trip log: the car's own readings, prefixed veh.* so they never collide
    # with the aux bank's soc/volts. Deadbands chosen so a drive is a few
    # hundred rows, not tens of thousands -- speed to the nearest 2 km/h is
    # plenty for "how fast was I going", and rpm need not log every flare.
    "veh.speed": 2.0,
    "veh.rpm": 150.0,
    "veh.coolant": 1.0,
    "veh.oil": 1.0,
    "veh.fuel_left": 1.0,
    "veh.odometer": 0.1,
    "veh.battery_v": 0.1,
    "veh.battery_soc": 1.0,
}
CELL_DEADBAND = 0.002           # every cellN
DEFAULT_DEADBAND = 0.01

HEARTBEAT_S = 600               # ten minutes, even when nothing moves
KEEP_DAYS = 60

_last: dict[str, tuple[float, float]] = {}      # name -> (value, when)


def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    db = sqlite3.connect(PATH, timeout=5)
    db.execute("PRAGMA journal_mode=WAL")        # a car loses power abruptly
    db.execute("""CREATE TABLE IF NOT EXISTS samples (
                    ts REAL NOT NULL,
                    name TEXT NOT NULL,
                    value REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS states (
                    ts REAL NOT NULL,
                    name TEXT NOT NULL,
                    value TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS samples_name_ts ON samples(name, ts)")
    db.execute("CREATE INDEX IF NOT EXISTS states_name_ts ON states(name, ts)")
    return db


def deadband(name: str) -> float:
    if name in DEADBAND:
        return DEADBAND[name]
    if name.startswith("cell") and name[4:].isdigit():
        return CELL_DEADBAND
    return DEFAULT_DEADBAND


def flatten(aux: dict) -> tuple[dict, dict]:
    """One reading dict into (numbers, words).

    Nested readings keep their owner's name -- "solar.watts" rather than
    "watts" -- because two devices report voltages and a log that cannot say
    whose is a log of nothing. Cells are numbered from one, as a person would.
    """
    numbers: dict[str, float] = {}
    words: dict[str, str] = {}

    def walk(prefix: str, d: dict) -> None:
        for key, value in d.items():
            name = f"{prefix}{key}"
            if key in ("at", "cells", "balancing", "off_reason"):
                continue
            if isinstance(value, bool):
                numbers[name] = 1.0 if value else 0.0
            elif isinstance(value, (int, float)):
                numbers[name] = float(value)
            elif isinstance(value, str):
                words[name] = value
            elif isinstance(value, dict):
                walk(f"{name}.", value)

    walk("", aux)
    for i, volts in enumerate(aux.get("cells") or [], start=1):
        numbers[f"cell{i}"] = float(volts)
    balancing = aux.get("balancing")
    if balancing is not None:
        words["balancing"] = ",".join(str(n) for n in balancing)
    return numbers, words


def record(aux: dict, now: float | None = None) -> int:
    """Write what has changed. Returns how many rows were written."""
    now = time.time() if now is None else now
    numbers, words = flatten(aux)
    rows, word_rows = [], []

    for name, value in numbers.items():
        was = _last.get(name)
        if (was is None or abs(value - was[0]) >= deadband(name)
                or now - was[1] >= HEARTBEAT_S):
            rows.append((now, name, value))
            _last[name] = (value, now)

    for name, value in words.items():
        was = _last.get("w:" + name)
        if was is None or was[0] != value or now - was[1] >= HEARTBEAT_S:
            word_rows.append((now, name, value))
            _last["w:" + name] = (value, now)

    if not rows and not word_rows:
        return 0
    db = _db()
    try:
        with db:
            db.executemany("INSERT INTO samples VALUES (?,?,?)", rows)
            db.executemany("INSERT INTO states VALUES (?,?,?)", word_rows)
    finally:
        db.close()
    return len(rows) + len(word_rows)


def series(name: str, since: float = 0, limit: int = 2000) -> list[tuple[float, float]]:
    """What one reading has been, oldest first."""
    db = _db()
    try:
        rows = db.execute(
            "SELECT ts, value FROM samples WHERE name=? AND ts>=?"
            " ORDER BY ts DESC LIMIT ?", (name, since, limit)).fetchall()
    finally:
        db.close()
    return list(reversed(rows))


def summary(name: str, since: float = 0) -> dict:
    """Low, high and latest for one reading, which is what a screen shows."""
    db = _db()
    try:
        row = db.execute(
            "SELECT MIN(value), MAX(value), COUNT(*) FROM samples"
            " WHERE name=? AND ts>=?", (name, since)).fetchone()
        last = db.execute(
            "SELECT ts, value FROM samples WHERE name=? ORDER BY ts DESC LIMIT 1",
            (name,)).fetchone()
    finally:
        db.close()
    if not row or row[2] == 0:
        return {}
    return {"min": row[0], "max": row[1], "count": row[2],
            "last": last[1] if last else None,
            "at": last[0] if last else None}


def names() -> list[str]:
    db = _db()
    try:
        return [r[0] for r in db.execute(
            "SELECT DISTINCT name FROM samples ORDER BY name")]
    finally:
        db.close()


def prune(keep_days: int = KEEP_DAYS) -> int:
    """Drop what is older than we keep. Returns rows removed."""
    cutoff = time.time() - keep_days * 86400
    db = _db()
    try:
        with db:
            n = db.execute("DELETE FROM samples WHERE ts<?", (cutoff,)).rowcount
            n += db.execute("DELETE FROM states WHERE ts<?", (cutoff,)).rowcount
        db.execute("VACUUM")
    finally:
        db.close()
    return n


def main() -> int:
    import argparse
    import datetime
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", metavar="NAME")
    ap.add_argument("--names", action="store_true")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--hours", type=float, default=24)
    args = ap.parse_args()

    if args.names:
        for n in names():
            print(n)
    elif args.show:
        since = time.time() - args.hours * 3600
        for ts, value in series(args.show, since):
            when = datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
            print(f"{when}  {value:g}")
        s = summary(args.show, since)
        if s:
            print(f"\n{args.show}: {s['count']} rows, "
                  f"low {s['min']:g}, high {s['max']:g}, last {s['last']:g}")
    elif args.prune:
        print(f"removed {prune()} rows")
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
