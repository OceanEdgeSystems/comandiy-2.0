#!/usr/bin/env python3
"""Keep the offline speed limit table up to date, from Geofabrik's extracts.

Run daily by comand-map-update.timer. Settings > Maps says how often it should
really do anything and for which countries; "Update now" sets FORCE. Downloads run only
when allowed in the settings.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, "/usr/local/share/comand-ui")
import cameras  # noqa: E402
import speedlimits  # noqa: E402

BRIDGE = "http://127.0.0.1:8080"
HOME = "/var/lib/comand-speed"
MAPS = os.path.join(HOME, "maps")
FORCE = os.path.join(HOME, "update-now")
URL = "https://download.geofabrik.de/europe/{}-latest.osm.pbf"
DAYS = {"weekly": 7, "monthly": 30}
HA_URLS = os.environ.get("HA_URLS", "").split()
TOKEN = os.environ.get("HA_TOKEN", "")
MIN_ROADS = 1000


def status(**kw) -> None:
    req = urllib.request.Request(BRIDGE + "/cmd", json.dumps({"map_status": kw}).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except OSError as e:
        print(f"bridge: {e}", flush=True)
    print("status", kw, flush=True)


def ha_state(entity: str) -> str | None:
    for base in HA_URLS:
        req = urllib.request.Request(f"{base}/api/states/{entity}",
                                     headers={"Authorization": f"Bearer {TOKEN}"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return str(json.load(r).get("state"))
        except (OSError, ValueError):
            continue
    return None


def network_allowed() -> tuple[bool, str]:
    """Downloads are large: only when MAP_AUTO_UPDATE=1 is set in /etc/default/comand."""
    if os.environ.get("MAP_AUTO_UPDATE") == "1":
        return True, "allowed by settings"
    return False, "MAP_AUTO_UPDATE is off"


def fetch(country: str) -> str:
    path = os.path.join(MAPS, f"{country}.osm.pbf")
    part = path + ".part"
    url = URL.format(country)
    cmd = ["curl", "-L", "--fail", "-sS", "--limit-rate", "15M", "-o", part, url]
    if os.path.exists(path):
        cmd[1:1] = ["-z", path]              # only if Geofabrik has a newer one
    elif os.path.exists(part):
        cmd[1:1] = ["-C", "-"]
    subprocess.run(cmd, check=True)
    if os.path.exists(part) and os.path.getsize(part) > 0:
        os.replace(part, path)
    return path


def update(countries: list[str]) -> None:
    os.makedirs(MAPS, exist_ok=True)
    paths = []
    for c in countries:
        status(state=f"Downloading {c}")
        paths.append(fetch(c))
    status(state="Building")
    source = paths[0]
    if len(paths) > 1:
        source = os.path.join(MAPS, "merged.tmp.osm.pbf")
        subprocess.run(["osmium", "merge", "-O", "-o", source, *paths], check=True)
    new = os.path.join(HOME, "speedlimits.new")
    if os.path.exists(new):
        os.unlink(new)
    speedlimits.build(source, new)
    new_cameras = os.path.join(HOME, "cameras.new")
    cameras.build(source, new_cameras)
    if source.endswith("merged.tmp.osm.pbf"):
        os.unlink(source)
    db = sqlite3.connect(new)
    rows = db.execute("select count(*) from way").fetchone()[0]
    db.close()
    if rows < MIN_ROADS:
        raise RuntimeError(f"only {rows} roads in the new table")
    os.replace(new, speedlimits.DB)
    os.replace(new_cameras, cameras.DB)
    status(state="", updated=time.strftime("%Y-%m-%d"), done=True)


def main() -> int:
    with urllib.request.urlopen(BRIDGE + "/state", timeout=5) as r:
        st = json.load(r)
    forced = os.path.exists(FORCE)
    every = DAYS.get(st.get("mapFreq"))
    age = (time.time() - os.path.getmtime(speedlimits.DB)) / 86400 if os.path.exists(speedlimits.DB) else 1e9
    if not forced and (not every or age < every):
        print(f"not due: {st.get('mapFreq')}, {age:.0f} days old", flush=True)
        return 0
    ok, why = network_allowed()
    print("network:", why, flush=True)
    if not ok:
        status(state="Waiting for Wi-Fi or Starlink")
        return 0
    if forced:
        os.unlink(FORCE)
    try:
        update(list(st.get("mapCountries") or ["germany"]))
    except Exception as e:  # noqa: BLE001 - keep the working table, say so
        status(state="Update failed")
        print(f"failed: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
