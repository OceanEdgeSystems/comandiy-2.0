#!/usr/bin/env python3
"""Speed limits from OpenStreetMap, offline, for the speed warning.

The Ooono app says only "too fast" (one byte, no number), so the limit shown
with that warning comes from here: every road in the extract that carries a
maxspeed, with its geometry, in one SQLite file with an R-tree over the ways.
A lookup is the nearest such road to the car's GPS position, with the car's
heading picking the side of a road whose limit differs by direction.

Build (on the head unit; the Germany extract is ~4.5 GB, the result ~1 GB):

    curl -L -o germany-latest.osm.pbf https://download.geofabrik.de/europe/germany-latest.osm.pbf
    python3 speedlimits.py build germany-latest.osm.pbf speedlimits.sqlite

Look up:

    python3 speedlimits.py lookup speedlimits.sqlite 53.0937 10.6484 [track]

Needs osmium-tool for the build only.
"""

import json
import math
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile

DB = "/var/lib/comand-speed/speedlimits.sqlite"

# maxspeed values that are not a number of km/h. "none" is the unrestricted
# Autobahn: no number to show, so no sign.
IMPLICIT = {
    "DE:urban": 50, "DE:rural": 100, "DE:living_street": 7, "DE:bicycle_road": 30,
    "DE:zone30": 30, "DE:zone:30": 30, "DE:zone20": 20, "DE:zone:20": 20,
    "walk": 7,
}


def kmh(value):
    """A maxspeed tag as km/h, or None if it has no single number."""
    if not value:
        return None
    v = value.strip()
    if v in IMPLICIT:
        return IMPLICIT[v]
    if v.endswith("mph"):
        try:
            return round(float(v[:-3]) * 1.609)
        except ValueError:
            return None
    try:
        n = int(float(v.split(";")[0]))
    except ValueError:
        return None
    return n if 5 <= n <= 150 else None


def build(pbf, out):
    work = tempfile.mkdtemp(dir=os.path.dirname(os.path.abspath(out)) or ".")
    roads = os.path.join(work, "roads.osm.pbf")
    seq = os.path.join(work, "roads.geojsonseq")
    # Ways with a highway tag and some maxspeed, then their lines.
    subprocess.run(["osmium", "tags-filter", "-O", "-o", roads, pbf,
                    "w/maxspeed", "w/maxspeed:forward", "w/maxspeed:backward"], check=True)
    subprocess.run(["osmium", "export", "-O", "-f", "geojsonseq", "--geometry-types=linestring",
                    "-a", "id", "-o", seq, roads], check=True)

    if os.path.exists(out):
        os.remove(out)
    db = sqlite3.connect(out)
    db.executescript("""
        CREATE VIRTUAL TABLE idx USING rtree(id, minlat, maxlat, minlon, maxlon);
        CREATE TABLE way(id INTEGER PRIMARY KEY, fwd INTEGER, bwd INTEGER, pts BLOB);
    """)
    n = 0
    with open(seq) as f:
        for line in f:
            line = line.lstrip("\x1e").strip()
            if not line:
                continue
            ft = json.loads(line)
            tags = ft.get("properties") or {}
            if "highway" not in tags:
                continue
            both = kmh(tags.get("maxspeed"))
            fwd = kmh(tags.get("maxspeed:forward")) or both
            bwd = kmh(tags.get("maxspeed:backward")) or both
            if fwd is None and bwd is None:
                continue
            coords = ft["geometry"]["coordinates"]          # [lon, lat]
            lats = [c[1] for c in coords]
            lons = [c[0] for c in coords]
            n += 1
            pts = struct.pack(f"<{2 * len(coords)}f", *[x for c in coords for x in (c[1], c[0])])
            db.execute("INSERT INTO way VALUES (?,?,?,?)", (n, fwd, bwd, pts))
            db.execute("INSERT INTO idx VALUES (?,?,?,?,?)",
                       (n, min(lats), max(lats), min(lons), max(lons)))
            if n % 200000 == 0:
                db.commit()
                print(f"{n} ways", flush=True)
    db.commit()
    db.close()
    for p in (roads, seq):
        os.remove(p)
    os.rmdir(work)
    print(f"done: {n} ways with a limit -> {out}")


def lookup(db, lat, lon, track=None, reach_m=25.0):
    """The limit in km/h on the road the car is on, or None.

    The nearest road within reach_m wins. With a heading, a road running across
    the car's direction counts as farther than it is -- at a junction the road
    you are on beats the one you are crossing -- and a road whose limit differs
    by direction gives the one for the way the car is going.
    """
    m_lat = 111320.0
    m_lon = 111320.0 * math.cos(math.radians(lat))
    d = reach_m / m_lat
    rows = db.execute(
        "SELECT way.fwd, way.bwd, way.pts FROM idx JOIN way ON way.id = idx.id "
        "WHERE idx.maxlat >= ? AND idx.minlat <= ? AND idx.maxlon >= ? AND idx.minlon <= ?",
        (lat - d, lat + d, lon - d * m_lat / m_lon, lon + d * m_lat / m_lon)).fetchall()
    best = None
    for fwd, bwd, blob in rows:
        p = struct.unpack(f"<{len(blob) // 4}f", blob)
        for i in range(0, len(p) - 2, 2):
            ax, ay = (p[i + 1] - lon) * m_lon, (p[i] - lat) * m_lat
            bx, by = (p[i + 3] - lon) * m_lon, (p[i + 2] - lat) * m_lat
            dx, dy = bx - ax, by - ay
            ll = dx * dx + dy * dy
            t = 0.0 if ll == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / ll))
            dist = math.hypot(ax + t * dx, ay + t * dy)
            limit = fwd if fwd is not None else bwd
            if track is not None and ll > 0:
                bearing = math.degrees(math.atan2(dx, dy)) % 360
                diff = abs((track - bearing + 180) % 360 - 180)     # 0..180
                along = min(diff, 180 - diff)                       # 0 along, 90 across
                dist += along / 90 * 20
                limit = (fwd if diff <= 90 else bwd) or fwd or bwd
            if dist <= reach_m and (best is None or dist < best[0]):
                best = (dist, limit)
    return best[1] if best else None


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "build":
        build(sys.argv[2], sys.argv[3])
    elif len(sys.argv) >= 5 and sys.argv[1] == "lookup":
        db = sqlite3.connect(sys.argv[2])
        tr = float(sys.argv[5]) if len(sys.argv) > 5 else None
        print(lookup(db, float(sys.argv[3]), float(sys.argv[4]), tr))
    else:
        print(__doc__)
        sys.exit(2)
