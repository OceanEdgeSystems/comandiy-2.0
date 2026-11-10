#!/usr/bin/env python3
"""Fixed speed cameras from OpenStreetMap, for the distance on the warning.

The Ooono app only says "speed camera", so this finds a mapped camera ahead of
the car. Mobile cameras are not in the map, and then there is simply no distance.

    python3 cameras.py build country.osm.pbf cameras.sqlite
    python3 cameras.py ahead cameras.sqlite 53.09 10.64 90
"""
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

DB = "/var/lib/comand-speed/cameras.sqlite"
REACH_M = 1500.0
CONE_DEG = 40.0          # how far off the car's heading "ahead" may be


def build(pbf: str, out: str) -> int:
    with tempfile.TemporaryDirectory(dir=os.path.dirname(os.path.abspath(out))) as tmp:
        opl = os.path.join(tmp, "cameras.opl")
        subprocess.run(["osmium", "tags-filter", "-O", "-o", opl, "-f", "opl", pbf,
                        "n/highway=speed_camera"], check=True)
        if os.path.exists(out):
            os.unlink(out)
        db = sqlite3.connect(out)
        db.execute("CREATE TABLE camera(id INTEGER PRIMARY KEY, lat REAL, lon REAL, limit_kmh INTEGER)")
        n = 0
        with open(opl) as fh:
            for line in fh:
                pos = re.search(r" x(-?[\d.]+) y(-?[\d.]+)", line)
                if not pos:
                    continue
                limit = re.search(r"maxspeed=(\d+)(?:[,\s]|$)", line)
                db.execute("INSERT INTO camera(lat, lon, limit_kmh) VALUES (?, ?, ?)",
                           (float(pos[2]), float(pos[1]), int(limit[1]) if limit else None))
                n += 1
        db.execute("CREATE INDEX camera_lat ON camera(lat)")
        db.commit()
        db.close()
    return n


def where(lat: float, lon: float, cam_lat: float, cam_lon: float) -> tuple[float, float]:
    """Distance in metres and bearing in degrees to a camera."""
    dy = (cam_lat - lat) * 111320.0
    dx = (cam_lon - lon) * 111320.0 * math.cos(math.radians(lat))
    return math.hypot(dx, dy), math.degrees(math.atan2(dx, dy)) % 360


def ahead(db: sqlite3.Connection, lat: float, lon: float, track: float | None):
    """(metres, limit) of the nearest mapped camera ahead, or None. Needs a heading."""
    if track is None:
        return None
    d = REACH_M / 111320.0
    best = None
    for c_lat, c_lon, limit in db.execute(
            "SELECT lat, lon, limit_kmh FROM camera WHERE lat BETWEEN ? AND ?", (lat - d, lat + d)):
        dist, bearing = where(lat, lon, c_lat, c_lon)
        off = abs((bearing - track + 180) % 360 - 180)
        if dist <= REACH_M and off <= CONE_DEG and (best is None or dist < best[0]):
            best = (dist, limit)
    return best


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "build":
        print(build(sys.argv[2], sys.argv[3]), "cameras")
    elif len(sys.argv) >= 6 and sys.argv[1] == "ahead":
        print(ahead(sqlite3.connect(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])))
    else:
        print(__doc__)
