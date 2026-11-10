#!/usr/bin/env python3
"""Turn-by-turn onto the nav display.

nav-ui already draws it -- a maneuver arrow, the distance and the street, on the
second round screen -- and the protocol takes

    {"m":"nav","nav":{"maneuver":"left","dist":300,"unit":"m","street":"..."}}

so the whole display side exists. This is the source.

WHY A SECOND SCREEN AND NOT THE MAIN ONE
The main screen is showing the map, so repeating the next turn on top of it adds
nothing. A small round screen at hand, showing one thing, is what turn-by-turn
actually wants.

WHERE THE DATA COMES FROM
The navigating app's persistent notification, read with `dumpsys notification`.
Nothing is installed inside Android and no accessibility service is involved.

Found by trying apps rather than by reasoning, and they differ wildly:

    Google Maps   title "50 m", text "toward Zurawia",
                  subText "3 hr 11 min - 295 km - 5:48 PM ETA"   -> works
    Sygic         title "Sygic", text "Running. Tap to open"     -> nothing
    Yandex Navi   posts no notification at all                   -> nothing

So this follows **`category=navigation`** rather than a package name. That is a
standard Android category, so any app publishing a real navigation notification
is picked up without being listed here, and an app that only says "still
running" is ignored because it does not claim to be one.

THE MANEUVER IS AN ICON, NOT TEXT
Maps puts the distance in the title and the road in the text, but the direction
only in the notification's icon -- `id=0x7f080a36` and friends. There is no
public mapping from those resource ids to turns, so ICONS below has to be
learned by watching. An unknown id draws "straight" and is logged rather than
guessed: a confidently wrong arrow is worse than a vague one, and this is a
screen someone glances at while driving.

Run with --learn along a route to collect the ids.

ROOT
`dumpsys` needs root and the bridge runs as the session user, so this is
separate and posts what it finds to the bridge over HTTP.

Usage:
    nav-watch.py --dump            # what every app publishes right now
    nav-watch.py                   # follow the navigation notification and feed it
    nav-watch.py --learn           # report unknown maneuver icon ids
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request

BRIDGE = "http://127.0.0.1:8080/cmd"
POLL_S = 1.0

PKG_RE = re.compile(r"pkg=(\S+)")
CAT_RE = re.compile(r"category=(\w+)")
ICON_RE = re.compile(r"icon=Icon\(typ=RESOURCE pkg=\S+ id=(0x[0-9a-f]+)\)")
FIELD_RE = re.compile(
    r"(android\.\w+)=(?:String|SpannableString|CharSequence)\s*\((.*)\)\s*$")

# "50 m", "1.2 km", "0,5 mi". Anchored at the start because this reads a title
# that is only ever a distance -- searching anywhere would find the "295 km"
# remaining in the trip summary and announce it as the next turn.
DIST_RE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(m|km|mi|ft)\b", re.I)

# Maps writes the road as "toward X", "on X", or bare. Strip the preposition so
# the display shows a road name rather than a sentence fragment.
LEAD_RE = re.compile(r"^\s*(toward|towards|on|onto|to)\s+", re.I)

# Google Maps maneuver icon resource ids. Learned by watching, undocumented, and
# specific to a Maps version -- so a miss is "unknown", never a guess.
ICONS: dict[str, str] = {
}


def dumpsys() -> str:
    try:
        r = subprocess.run(
            ["waydroid", "shell", "--", "dumpsys", "notification", "--noredact"],
            capture_output=True, text=True, timeout=15)
        return r.stdout
    except Exception:
        return ""


def records(dump: str) -> list[dict]:
    """Split the dump into one dict per notification."""
    out: list[dict] = []
    cur: dict | None = None
    for line in dump.splitlines():
        if "NotificationRecord(" in line:
            if cur:
                out.append(cur)
            m, c = PKG_RE.search(line), CAT_RE.search(line)
            cur = {"pkg": m.group(1) if m else "",
                   "category": c.group(1) if c else "",
                   "icon": "", "fields": {}}
            continue
        if cur is None:
            continue
        mi = ICON_RE.search(line)
        if mi and not cur["icon"]:
            cur["icon"] = mi.group(1)
        mf = FIELD_RE.search(line.strip())
        if mf:
            cur["fields"][mf.group(1)] = mf.group(2)
    if cur:
        out.append(cur)
    return out


def navigation_record(recs: list[dict]) -> dict | None:
    for r in recs:
        if r["category"] == "navigation" and r["fields"].get("android.title"):
            return r
    return None


def parse(rec: dict) -> dict | None:
    f = rec["fields"]
    title = f.get("android.title", "")

    m = DIST_RE.match(title)
    if not m:
        # A navigation notification whose title is not a distance is not
        # announcing a turn -- "Rerouting", say. Holding the last turn beats
        # drawing a zero.
        return None

    value = float(m.group(1).replace(",", "."))
    unit = m.group(2).lower()
    # km goes as tenths, so the firmware never parses a float.
    dist = int(round(value * 10)) if unit == "km" else int(round(value))

    street = LEAD_RE.sub("", f.get("android.text", "")).strip()

    return {
        "maneuver": ICONS.get(rec["icon"], "straight"),
        "dist": dist,
        "unit": unit,
        "street": street[:40],
        "_icon": rec["icon"],
    }


def post(nav: dict) -> None:
    body = json.dumps({"nav": {k: v for k, v in nav.items()
                               if not k.startswith("_")}}).encode()
    req = urllib.request.Request(BRIDGE, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass          # the bridge may be restarting; the next poll will do


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", action="store_true",
                    help="print every app's notification fields and stop")
    ap.add_argument("--learn", action="store_true",
                    help="report maneuver icon ids that are not in ICONS")
    ap.add_argument("--package",
                    help="read this package instead of whichever notification "
                         "declares category=navigation")
    ap.add_argument("--interval", type=float, default=POLL_S)
    args = ap.parse_args()

    last = None
    seen_icons: set[str] = set()

    while True:
        recs = records(dumpsys())

        if args.dump:
            for r in recs:
                if args.package and r["pkg"] != args.package:
                    continue
                if not r["fields"]:
                    continue
                print(json.dumps({"pkg": r["pkg"], "category": r["category"],
                                  "icon": r["icon"], **r["fields"]},
                                 ensure_ascii=False, indent=2), flush=True)
            time.sleep(args.interval)
            continue

        rec = (next((r for r in recs if r["pkg"] == args.package), None)
               if args.package else navigation_record(recs))
        if not rec:
            time.sleep(args.interval)
            continue

        nav = parse(rec)
        if not nav:
            time.sleep(args.interval)
            continue

        if args.learn and nav["_icon"] and nav["_icon"] not in ICONS:
            if nav["_icon"] not in seen_icons:
                seen_icons.add(nav["_icon"])
                print(f"unknown maneuver icon {nav['_icon']} "
                      f"while heading to {nav['street']!r}", flush=True)

        stripped = {k: v for k, v in nav.items() if not k.startswith("_")}
        if stripped != last:
            last = stripped
            print(f"nav: {stripped}", flush=True)
            post(nav)

        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
