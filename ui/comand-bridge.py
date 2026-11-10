#!/usr/bin/env python3
"""Join the knob and the head unit screen into one device.

The knob speaks JSON over USB CDC; the screen is a web page in cog. Neither can
reach the other, so this sits between them: it serves the page, streams knob
events to it, and pushes the resulting state back to the knob so both ends
always agree.

    knob  --USB CDC-->  bridge  --SSE-->    page
    knob  <--USB CDC--  bridge  <--POST--   page

WHY THE PAGE IS SERVED RATHER THAN OPENED AS A FILE
A file:// page has a null origin, which makes EventSource and fetch back to
localhost awkward at best. Serving it from here gives it a real origin, and the
same process then owns the page, the event stream and the serial port -- one
thing to start, one thing to debug.

WHY SSE AND NOT WEBSOCKETS
Server-sent events are one long HTTP response, so this needs nothing outside
the standard library. The page never has more to say than a menu move, and that
fits in a POST. A WebSocket would mean a dependency on the car's box for a
duplex we do not need.

WHICH SERIAL PORT
Matched by USB vendor id 0x303A (Espressif), never by device name. On this box
/dev/ttyACM0 is the u-blox GPS receiver, and writing a knob feed into it would
break the GPS -- the same trap the LIVI bridge documents.

Usage:
    comand-bridge.py                 # serve on 127.0.0.1:8080, find the knob
    comand-bridge.py --port 8080
    comand-bridge.py --device /dev/ttyACM1
    comand-bridge.py --no-serial     # page only, for working without hardware
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# The reading log. Its own module because it is a database with a deadband and
# a retention policy, none of which this file should have opinions about.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import history
except Exception as _e:                 # pragma: no cover - a car still drives
    history = None
    print(f"history log unavailable: {_e}", file=sys.stderr)
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "main-menu.html")

ESPRESSIF_VID = 0x303A
BAUD = 115200

VOL_STYLES = ("band", "page", "box")
WAKE_WORDS = ("off", "comand", "ml", "mercedes")
NAV_APPS = ("carplay", "sygic")
MAP_FREQS = ("off", "weekly", "monthly")
MAP_FORCE = "/var/lib/comand-speed/update-now"
WARN_STYLES = ("a", "b", "c", "d", "e", "f")
VOICE_SOUNDS = ("current", "bell", "chime", "sweep", "pop")
MENU = ["Telephone", "Music", "Radio", "Video", "Mirror", "Carplay", "Navigation",
        "Car", "Settings"]

# The knob's menu, which is a tree rather than the flat list it used to be.
#
# The panel's page has always had submenus; the knob only ever received one
# list, so Settings and Car were rows it could highlight and not enter -- the
# bridge looked the label up in SOURCES, found nothing, and logged "no
# destination yet". Which is a fine thing for the software to say and a poor
# thing for a driver to be told by a knob that just sat there.
#
# The knob is not given the tree: it gets one level at a time, the same flat
# list it already knows how to draw, and this side remembers where in the
# tree that level came from. The firmware needs no changes at all.
#
# Leaves whose labels carry a value are built rather than written, so a row
# reads "Battery monitor: On" and changes under the selection when pressed.
KNOB_TREE = {
    "root": MENU,
    "Video": ["Live TV", "YouTube", "Movies", "TV shows", "Back"],
    "Car": ["Vehicle", "Aux battery", "System", "Back"],
    "Settings": ["Bluetooth", "CarPlay", "Phone Bluetooth", "Start with", "Drive", "Back"],
    "Bluetooth": ["Battery monitor", "Back"],
}

BACK = "Back"
MORE = "More…"

# The video branch of the knob's menu is not a fixed KNOB_TREE list: its rows
# (channels, films, YouTube videos) are fetched from car/video-link.py when the
# driver walks into it, the same source the panel's page browses. A dynamic
# level's id starts with DYN and its rows live in knob_dyn; because the firmware
# draws at most MENU_MAX (12) rows, long lists are paged -- KNOB_PAGE content
# rows, then More if there is another page, then Back.
DYN = "§"
KNOB_PAGE = 10
KNOB_LABEL_MAX = 19          # firmware menuItems is char[20]
VIDEO_LEAVES = {
    "Live TV":  {"kind": "tv"},
    "Movies":   {"kind": "list", "what": "movie"},
    "TV shows": {"kind": "list", "what": "show"},
    "YouTube":  {"kind": "yt_root"},
}
knob_dyn: dict[str, dict] = {}
knob_dyn_lock = threading.Lock()

# What a menu entry actually does. Only the ones that have a destination are
# listed; the rest are still menu items, they simply do not go anywhere yet.
#
# Selecting one raises Android and starts the app. Raising is separate from
# starting on purpose: if Sygic is already running mid-route, relaunching it
# would throw away the route, so the launch is by intent and the compositor is
# told to bring Android forward either way.
# Each source is a sway workspace plus, for the Android ones, the package to
# bring forward inside it. Switching workspace leaves the other source running:
# a CarPlay session survives a look at the map, and a route survives a look at
# CarPlay.
# Where the car starts. The menu is a source like any other, on its own empty
# workspace, so "boot to the menu" is not a special case -- it is the default
# source. Persisted, because a setting the driver chose should survive the
# ignition.
HOME_WS = "3"

STATIONS_PATH = "/var/lib/comand/stations.json"
FAVOURITES_PATH = "/var/lib/comand/favourites.json"
LOGO_DIR = "/var/lib/comand/logos"
MPD_ADDR = ("127.0.0.1", 6600)

# The knob opens its own menu when it has no host, so at boot it is sitting on
# a menu where any press activates whatever is selected -- and that arrives
# while the boot source is still being applied. Whatever the driver chose in
# Settings should win over a keypress that happened during startup, so knob
# activations are ignored until the boot source has been applied.
booted = threading.Event()
SETTINGS_PATH = os.path.expanduser("~/.config/comand/settings.json")
settings = {"boot_source": "menu"}      # "menu" or a key of SOURCES


# ------------------------------------------------------------------- audio ----
# One volume for the whole head unit, applied to the PipeWire sink rather than
# to any one player. The knob has to mean the same thing whether the sound is
# radio, CarPlay or Android, and a per-application volume would make it lie
# about the other two.

def hw_sink() -> str:
    """The real output. Volume lives here rather than on the default sink: the
    default is the sound correction (comand-eq.py), held at 100%, so that if it
    ever goes missing the sound falls back to the output at the same volume."""
    with lock:
        return state.get("outputActive") or "@DEFAULT_SINK@"


# Speed-dependent volume: a gentle lift added to the set level as the car
# speeds up, so road noise does not bury the sound and a stop does not blast
# it. The base level the user chose is state["vol"] and is what the UI shows;
# the lift is added only where the volume is actually put on the sink, so it
# never drifts into the stored level. Off by default.
SPEED_LIFT_MAX = {"low": 4, "medium": 8, "high": 12}   # extra percent at full speed
SPEED_LIFT_LO = 30.0        # km/h below which there is no lift
SPEED_LIFT_HI = 120.0       # km/h at which the lift is full


def speed_lift() -> int:
    if not settings.get("speed_volume"):
        return 0
    with lock:
        veh = state.get("vehicle") or {}
    try:
        kph = float(veh.get("speed"))
    except (TypeError, ValueError):
        return 0
    if kph <= SPEED_LIFT_LO:
        return 0
    top = SPEED_LIFT_MAX.get(settings.get("speed_volume_strength", "medium"), 8)
    frac = min(1.0, (kph - SPEED_LIFT_LO) / (SPEED_LIFT_HI - SPEED_LIFT_LO))
    return int(round(top * frac))


def set_output_volume(percent: int, muted: bool) -> None:
    hw = hw_sink()
    eff = max(0, min(100, int(percent) + speed_lift()))
    run(["pactl", "set-sink-volume", hw, f"{eff}%"])
    run(["pactl", "set-sink-mute", hw, "1" if muted else "0"])


def speed_vol_watch() -> None:
    """Re-apply the volume when the speed lift changes, so the level tracks the
    road without the user touching anything. Does nothing while off, and only
    re-applies when the lift actually moves (not on every speed sample)."""
    last = 0
    while True:
        time.sleep(2)
        if not settings.get("speed_volume"):
            if last:
                last = 0
                with lock:
                    vol, muted = state["vol"], state["muted"]
                set_output_volume(vol, muted)
            continue
        lift = speed_lift()
        if lift != last:
            last = lift
            with lock:
                vol, muted = state["vol"], state["muted"]
            set_output_volume(vol, muted)


EQ_NODE, EQ_OUT = "comand_eq", "comand_eq_out"
EQ_TOOL = "/usr/local/share/comand-ui/comand-eq.py"


def duckable_streams(timeout: float = 1.5) -> list[str]:
    """Sink inputs that are players, not the sound correction's own output:
    ducking that would duck the warning that is meant to be heard over them."""
    _, out = run(["pactl", "-f", "json", "list", "sink-inputs"], timeout=timeout)
    try:
        return [str(i["index"]) for i in json.loads(out)
                if i.get("properties", {}).get("node.name") != EQ_OUT]
    except (ValueError, KeyError, TypeError):
        return []


def mpd(*lines: str) -> str:
    """One MPD command, one connection.

    A short-lived connection per command rather than a persistent one: MPD is
    on localhost, the commands are rare -- a station change, not a frame -- and
    a persistent socket is one more thing to notice has died after a network
    blip or an MPD restart.
    """
    import socket
    try:
        with socket.create_connection(MPD_ADDR, timeout=5) as sk:
            sk.settimeout(5)
            sk.recv(4096)                       # the OK MPD banner
            sk.sendall(("".join(l + "\n" for l in lines)).encode())
            out = b""
            while b"OK\n" not in out and b"ACK " not in out:
                chunk = sk.recv(8192)
                if not chunk:
                    break
                out += chunk
            return out.decode("utf-8", "replace")
    except Exception as e:
        return f"ACK error: {e}"


def mpd_status() -> dict:
    """What is playing, for the display."""
    info: dict[str, str] = {}
    for line in mpd("status", "currentsong").splitlines():
        if ": " in line:
            k, v = line.split(": ", 1)
            info[k.lower()] = v
    return info


stations: list[dict] = []
stations_mtime = 0.0


def logo_name(station: str) -> str:
    """The cached artwork for a station, or "" if it has none."""
    import re
    base = re.sub(r"[^a-z0-9]+", "-", station.lower()).strip("-")
    for ext in (".png", ".jpg", ".ico", ".svg", ".webp"):
        if os.path.exists(os.path.join(LOGO_DIR, base + ext)):
            return base + ext
    return ""


_logo_fetch_lock = threading.Lock()
_logo_fetch_running = False
_logo_attempted: set[str] = set()      # names we have already run a fetch for this session


def maybe_fetch_logos(sts: list[dict]) -> None:
    """Fetch artwork for any station that has none yet, in the background.

    A station added to stations.json draws as letters until its logo is cached;
    this resolves and strips one (fetch-logos.py, beside this file) the first
    time the list is read with it missing, so a new station gets its mark
    without anyone running a tool. Each name is tried once a session -- a
    station with no findable logo keeps its letters rather than being chased on
    every read. One run at a time."""
    global _logo_fetch_running
    want = [st["name"] for st in sts if not st.get("logo") and st["name"] not in _logo_attempted]
    if not want:
        return
    with _logo_fetch_lock:
        if _logo_fetch_running:
            return
        _logo_fetch_running = True
        _logo_attempted.update(want)

    def run():
        global _logo_fetch_running
        import subprocess
        try:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fetch-logos.py")
            subprocess.run([sys.executable, script], timeout=180,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # Pick up the freshly cached files in place, no reload needed.
            with lock:
                for st in stations:
                    st["logo"] = logo_name(st["name"])
        except Exception as e:
            print(f"logo fetch: {e}", file=sys.stderr)
        finally:
            _logo_fetch_running = False

    threading.Thread(target=run, daemon=True).start()


def load_stations() -> list[dict]:
    """Re-read when the file changes, so editing it needs no restart."""
    global stations, stations_mtime
    try:
        m = os.path.getmtime(STATIONS_PATH)
        if m != stations_mtime:
            with open(STATIONS_PATH) as fh:
                stations = [st for st in json.load(fh).get("stations", [])
                            if st.get("name") and st.get("url")]
            # Tell the page which stations have artwork, so it falls back to
            # lettering per station rather than for the whole grid.
            for st in stations:
                st["logo"] = logo_name(st["name"])
            stations_mtime = m
            print(f"loaded {len(stations)} radio stations", flush=True)
            maybe_fetch_logos(stations)
    except Exception as e:
        if not stations:
            print(f"no stations: {e}", file=sys.stderr)
    return stations


# Favourites: one list the driver curates, across the three things the car can
# play on demand -- an internet-radio station, a Live TV channel, a Music
# Assistant playlist. Each is {type, label, payload, art?}, where payload is the
# very thing the existing play paths already take: a station name, a {id, name}
# channel, or an MA uri. So "play a favourite" is not a fourth player, it is a
# fan-out to the three that are already there. Unlike stations.json this one is
# written at runtime (the screen and the phone add and remove), so it has a
# saver of its own rather than being hand-edited.
FAV_TYPES = ("radio", "tv", "playlist")
favourites: list[dict] = []
favourites_mtime = 0.0
fav_lock = threading.Lock()


def fav_key(f: dict) -> tuple:
    """What makes two favourites the same one, so adding twice is adding once."""
    p = f.get("payload")
    ident = p.get("id") if isinstance(p, dict) else p
    return (f.get("type"), str(ident))


def clean_fav(f: dict) -> dict | None:
    """A favourite as it is kept: validated, trimmed, nothing extra."""
    if not isinstance(f, dict) or f.get("type") not in FAV_TYPES:
        return None
    label = str(f.get("label") or "").strip()[:120]
    typ = f["type"]
    payload = f.get("payload")
    if typ == "tv":
        if not isinstance(payload, dict):
            return None
        cid = str(payload.get("id") or "")
        if not re.fullmatch(r"[\w-]{1,64}", cid):
            return None
        payload = {"id": cid, "name": str(payload.get("name") or label)[:120]}
        label = label or payload["name"]
    else:   # radio, playlist: payload is a string (station name / MA uri)
        payload = str(payload or "").strip()
        if not payload or len(payload) > 500:
            return None
        label = label or (payload if typ == "radio" else "")
    if not label:
        return None
    out = {"type": typ, "label": label, "payload": payload}
    art = str(f.get("art") or "").strip()
    if art and len(art) <= 300:
        out["art"] = art
    return out


def load_favourites() -> list[dict]:
    """Re-read when the file changes, so an edit on the box needs no restart."""
    global favourites, favourites_mtime
    try:
        m = os.path.getmtime(FAVOURITES_PATH)
        if m != favourites_mtime:
            with open(FAVOURITES_PATH) as fh:
                raw = json.load(fh).get("favourites", [])
            favourites = [c for c in (clean_fav(f) for f in raw) if c]
            favourites_mtime = m
            print(f"loaded {len(favourites)} favourites", flush=True)
    except FileNotFoundError:
        favourites = favourites or []
    except Exception as e:
        if not favourites:
            print(f"no favourites: {e}", file=sys.stderr)
    return favourites


def save_favourites() -> None:
    global favourites_mtime
    os.makedirs(os.path.dirname(FAVOURITES_PATH), exist_ok=True)
    tmp = FAVOURITES_PATH + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"favourites": favourites}, fh, indent=2)
    os.replace(tmp, FAVOURITES_PATH)
    try:
        favourites_mtime = os.path.getmtime(FAVOURITES_PATH)   # our own write is not a reload
    except OSError:
        pass


def fav_set(action: str, f: dict) -> bool:
    """Add or remove one favourite; returns whether the list changed."""
    global favourites
    with fav_lock:
        load_favourites()
        if action == "add":
            c = clean_fav(f)
            if not c:
                return False
            k = fav_key(c)
            if any(fav_key(x) == k for x in favourites):
                return False
            favourites = favourites + [c]
        elif action == "remove":
            k = fav_key(f if "payload" in f else {"type": f.get("type"), "payload": f.get("payload")})
            kept = [x for x in favourites if fav_key(x) != k]
            if len(kept) == len(favourites):
                return False
            favourites = kept
        else:
            return False
        save_favourites()
    return True


def play_favourite(f: dict) -> None:
    """Hand a favourite to whichever player already knows how to play its kind."""
    typ, payload = f.get("type"), f.get("payload")
    if typ == "radio" and isinstance(payload, str):
        play_station(payload)
    elif typ == "tv" and isinstance(payload, dict):
        guest_vid("/play", {"tv": payload.get("id"), "name": str(payload.get("name") or "")[:120]})
    elif typ == "playlist" and isinstance(payload, str):
        guest_fetch(GUEST_MA + "/play", {"uri": payload})


def publish_favourites() -> None:
    """Put the current list where every face can see it: the screens' state, and
    the car HA (so the phone app reads it as an entity's attributes)."""
    with lock:
        state["favourites"] = list(favourites)
    publish()
    ha_favourites()


def handle_favourite(fc: dict) -> None:
    """add / remove / play, from the COMAND screen, the guest page, or the app."""
    action = fc.get("action")
    if action == "play":
        threading.Thread(target=play_favourite, args=(fc,), daemon=True).start()
        return
    if action in ("add", "remove") and fav_set(action, fc):
        publish_favourites()


CONTACTS_PATH = "/var/lib/comand/contacts.json"
contacts: list[dict] = []
contacts_mtime = 0.0


def load_contacts() -> list[dict]:
    """Numbers worth having one press away.

    Hands-free gives no phonebook: HFP carries calls, not contacts, and the
    profile that would carry them needs the phone to serve them on request.
    Until that exists this is a list the driver writes, which is the same
    answer stations.json is -- and for the handful of numbers anybody actually
    dials from a car, it may be the whole answer.
    """
    global contacts, contacts_mtime
    try:
        m = os.path.getmtime(CONTACTS_PATH)
        if m != contacts_mtime:
            with open(CONTACTS_PATH) as fh:
                contacts = [c for c in json.load(fh).get("contacts", [])
                            if c.get("name") and c.get("number")]
            contacts_mtime = m
            print(f"loaded {len(contacts)} contacts", flush=True)
    except Exception as e:
        if not contacts:
            print(f"no contacts: {e}", file=sys.stderr)
    return contacts


PHONEBOOK_PATH = "/var/lib/comand/phonebook.json"
PBAP_SYNC = "/usr/local/share/comand-ui/pbap-sync.py"
CALL_AUDIO = "/usr/local/share/comand-ui/call-audio.py"
phonebook: list[dict] = []
phonebook_mtime = 0.0


def load_phonebook() -> list[dict]:
    """The phone's own contacts, pulled over PBAP by phone/pbap-sync.py.

    Kept apart from the driver's list rather than merged into it. Theirs is a
    handful of numbers they chose to have one press away; the phone's is three
    hundred, and letting one overwrite the other would lose the small one.
    """
    global phonebook, phonebook_mtime
    try:
        m = os.path.getmtime(PHONEBOOK_PATH)
        if m != phonebook_mtime:
            with open(PHONEBOOK_PATH) as fh:
                phonebook = [c for c in json.load(fh).get("contacts", [])
                             if c.get("name") and c.get("number")]
            phonebook_mtime = m
            print(f"loaded {len(phonebook)} numbers from the phone", flush=True)
    except Exception:
        pass                    # nothing synced yet is not an error
    return phonebook


def all_contacts() -> list[dict]:
    """The driver's list first, then the phone's.

    Order is the point: the numbers somebody chose to put in the car are the
    ones they dial from it, and they should not be somewhere down a list of
    three hundred.
    """
    return load_contacts() + load_phonebook()


call_audio_on = threading.Lock()


def follow_call_audio() -> None:
    """Keep the car's audio matched to the call until it ends: the phone can take the
    audio to its handset, or give it back, at any point in the call."""
    if not call_audio_on.acquire(blocking=False):
        return
    try:
        call_audio(True)
        while True:
            with lock:
                if not state["call"]:
                    break
            time.sleep(1)
            code, out = run(["python3", CALL_AUDIO, "--sync"], timeout=25)
            for line in (out or "").strip().splitlines():
                print(f"call audio: {line}", flush=True)
        call_audio(False)
    finally:
        call_audio_on.release()


def call_audio(up: bool) -> None:
    """Wire the call to the car, and say what happened.

    Logged rather than run silently: when the far end cannot hear the car,
    what the routing found is the first thing anybody needs, and running it
    through a pipe that discards its output means asking again later with the
    call long gone.
    """
    code, out = run(["python3", CALL_AUDIO] + ([] if up else ["--stop"]),
                    timeout=25)
    for line in (out or "").strip().splitlines():
        print(f"call audio: {line}", flush=True)


def sync_phonebook() -> None:
    """Ask the phone for its contacts.

    Run when a phone connects rather than on a timer: a phonebook changes when
    somebody edits it on the phone, and the car finds out the next time it is
    in the car.
    """
    code, out = run(["python3", PBAP_SYNC], timeout=120)
    line = (out or "").strip().splitlines()
    print(f"phonebook sync: {line[-1] if line else 'no output'}", flush=True)


COVER_DIR = "/var/lib/comand/covers"
COVER_FETCH = "/usr/local/share/comand-ui/cover-art.py"


def cover_key(artist: str, album: str, title: str) -> str:
    """One name for one piece of art.

    Keyed on album where there is one and on the track where there is not: a
    single and a live recording of the same song are different covers, and an
    album's twelve tracks are one.
    """
    import re
    basis = f"{artist} {album or title}".strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", basis).strip("-")[:80]


def cover_name(artist: str, album: str, title: str) -> str:
    """The cached art for a track, or "" if it has none yet."""
    key = cover_key(artist, album, title)
    if not key:
        return ""
    for ext in (".jpg", ".png", ".webp"):
        if os.path.exists(os.path.join(COVER_DIR, key + ext)):
            return key + ext
    return ""


def fetch_cover(artist: str, album: str, title: str) -> None:
    """Ask for a cover once, and let the next track's update pick it up.

    Not waited on: a screen that stalls until a web request answers is a
    screen that is wrong about what is playing.
    """
    code, out = run(["python3", COVER_FETCH, artist, album or title, title], timeout=25)
    line = (out or "").strip().splitlines()
    if line:
        print(f"cover: {line[-1]}", flush=True)
    # Re-publish so the art appears without waiting for the next track.
    with lock:
        m = dict(state["btmedia"]) if state["btmedia"] else None
    if m:
        m["art"] = cover_name(m.get("artist", ""), m.get("album", ""),
                              m.get("title", ""))
        with lock:
            state["btmedia"] = m
        publish()
        # And to the knob. The art usually arrives after the track does -- it
        # is a web request -- so the push that went out with the track had
        # nothing to send.
        if m["art"]:
            push_knob_cover(m)


def contact_name(number: str) -> str:
    """The name for a number, if the car knows one.

    Matched on the last nine digits rather than the whole string: the same
    number arrives as +45..., 0045... and 45... depending on who is asking,
    and a car that shows a name for one form and a bare number for another is
    a car that looks broken.
    """
    if not number:
        return ""
    want = "".join(c for c in number if c.isdigit())[-9:]
    if not want:
        return ""
    for c in all_contacts():
        have = "".join(ch for ch in str(c.get("number", "")) if ch.isdigit())
        if have[-9:] == want:
            return str(c.get("name", ""))
    return ""


def mpd_list(what: str, filter_tag: str = "", filter_val: str = "") -> list[str]:
    """Distinct values of a tag, optionally within another tag.

    Browsing by tag rather than by directory is what makes an iPod usable at
    all: its files live under scrambled names like Music/F07/ABCD.mp3, and only
    the tags say what they are.
    """
    cmd = f'list {what}'
    if filter_tag and filter_val:
        cmd += f' {filter_tag} "{filter_val.replace(chr(34), "")}"'
    out = mpd(cmd)
    vals = [l.split(": ", 1)[1] for l in out.splitlines()
            if l.lower().startswith(what.lower() + ": ")]
    return sorted({v for v in vals if v.strip()})


def play_album(artist: str, album: str) -> None:
    a = artist.replace('"', "")
    b = album.replace('"', "")
    mpd("clear", f'findadd artist "{a}" album "{b}"', "play")
    with lock:
        state["radio"] = ""          # a library track is not a station
        state["source"] = "Music"
        state["screen"] = "menu"
    publish()


def play_station(name: str) -> None:
    # Whatever else is playing gets out of the way first: two music sources
    # over one pair of speakers is the fault this cuts every way for.
    bt_stand_down()
    video_stand_down()
    # And CarPlay's: a pause, so it is where it was when the driver goes back.
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(b"pause", ("127.0.0.1", 7340))
    st = next((x for x in load_stations() if x["name"] == name), None)
    if not st:
        print(f"no such station: {name}", file=sys.stderr)
        return
    # Clear rather than append: a head unit that queues up every station you
    # ever pressed is a head unit that plays the wrong one when a stream drops.
    mpd("clear", f'add "{st["url"]}"', "play")
    with lock:
        state["radio"] = name
        state["song"] = name
        state["playing"] = True
        state["source"] = "Radio"
    threading.Thread(target=push_knob_radio, args=(name, True, True),
                     daemon=True).start()
    publish()


# ------------------------------------------------------------- knob logo ----
# The knob has a 240x240 round screen, and a station's own logo on it is worth
# more than its name in text: it is read at a glance, from the corner of the
# eye, which is the only way anything on a knob is read while driving.
#
# The artwork is pushed rather than held in the firmware. The stations are a
# list the driver edits, and firmware that must be reflashed to add one is
# firmware nobody adds a station to.
KNOB_LOGO_PX = 116
# Raw bytes per message: 192 encodes to 256 characters, so a whole line fits
# comfortably inside the board's receive ring even if one arrives while the
# previous is still being parsed.
KNOB_CHUNK = 192
# Set by the knob's acknowledgement of each chunk. The link has no flow
# control of its own, and the board reads its port once a frame, so a host
# that just writes as fast as it can loses most of an image.
logo_ack = threading.Event()


def rgb565_rle(img) -> bytes:
    """RGB565, run-length encoded as (count, hi, lo).

    Station logos are flat colour and a few marks, so runs are long: a 116x116
    image that would be 27 kB raw usually lands in one or two. Runs are capped
    at 255 because the count is a byte, and they do not wrap across rows --
    the decoder walks the image width, so a run that crossed a row boundary
    would be drawn in the wrong place.
    """
    out = bytearray()
    px = img.load()
    w, h = img.size
    run, cur = 0, None
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y][:3]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            if v == cur and run < 255:
                run += 1
            else:
                if cur is not None:
                    out += bytes((run, (cur >> 8) & 0xFF, cur & 0xFF))
                cur, run = v, 1
    if cur is not None:
        out += bytes((run, (cur >> 8) & 0xFF, cur & 0xFF))
    return bytes(out)


# What the firmware will hold. Run-length encoding is generous with a station
# logo -- flat colour, long runs -- and mean with a photograph, which is what
# album art is. A cover that does not fit is shrunk and then flattened until it
# does, rather than dropped: a smaller picture of the right record beats no
# picture at all.
KNOB_IMAGE_MAX = 24000
KNOB_SIZES = (100, 92, 84, 76)
KNOB_COLOURS = (0, 96, 48, 24)      # 0 means leave the colours alone


def knob_image(path: str, back: tuple[int, int, int]) -> tuple[int, int, bytes] | None:
    """Any picture, sized and encoded to fit the knob's screen."""
    try:
        from PIL import Image
    except ImportError:
        return None                  # no python3-pil: the name still shows
    try:
        original = Image.open(path).convert("RGBA")
    except Exception as e:
        print(f"image {path}: {e}", file=sys.stderr)
        return None

    for n in KNOB_SIZES:
        src = original.copy()
        src.thumbnail((n, n), Image.LANCZOS)
        canvas = Image.new("RGB", (n, n), back)
        canvas.paste(src, ((n - src.width) // 2, (n - src.height) // 2), src)
        for colours in KNOB_COLOURS:
            img = canvas
            if colours:
                # Fewer colours means longer runs. On a 116px circle behind a
                # steering wheel the flattening is not what anybody notices;
                # the record not being there is.
                img = canvas.quantize(colors=colours, method=Image.MEDIANCUT
                                      ).convert("RGB")
            data = rgb565_rle(img)
            if len(data) <= KNOB_IMAGE_MAX:
                if n != KNOB_SIZES[0] or colours:
                    print(f"image {os.path.basename(path)}: {n}px"
                          + (f", {colours} colours" if colours else "")
                          + f" -> {len(data)} bytes", flush=True)
                return n, n, data

    print(f"image {path}: will not fit the knob at any size", file=sys.stderr)
    return None


def knob_logo(st: dict) -> tuple[int, int, bytes] | None:
    """A station's artwork, sized and encoded for the knob."""
    logo = st.get("logo")
    if not logo:
        return None
    # The station's own colour behind it, not black: a mark drawn for a white
    # page disappears on a black screen, and the tile on the panel already
    # uses this colour, so the two ends match.
    bg = str(st.get("color") or "#000000").lstrip("#")
    try:
        back = tuple(int(bg[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        back = (0, 0, 0)
    return knob_image(os.path.join(LOGO_DIR, logo), back)


def push_knob_radio(name: str, playing: bool, with_logo: bool) -> None:
    global knob_picture
    """Put the knob on the radio face and, when asked, send the artwork.

    Sent from a thread of its own by the callers that change stations, because
    encoding and shipping 24 kB of base64 is not something to do on the path
    that starts the audio.
    """
    push_knob({"m": "radio"})
    push_knob({"radio": {"name": name, "playing": playing}})
    if not with_logo:
        return
    st = next((x for x in load_stations() if x["name"] == name), None)
    if not st:
        return
    made = knob_logo(st)
    if not made:
        return
    w, h, data = made
    knob_picture = f"logo:{name}"
    push_knob({"radio": {"logo": {"w": w, "h": h, "bytes": len(data)}}})
    sent = 0
    for i in range(0, len(data), KNOB_CHUNK):
        logo_ack.clear()
        chunk = base64.b64encode(data[i:i + KNOB_CHUNK]).decode()
        push_knob({"radio": {"chunk": chunk}})
        # Wait for the board to say it took it. The timeout is a backstop for
        # firmware too old to acknowledge, not the normal path: over USB the
        # round trip is a millisecond or two.
        if not logo_ack.wait(0.5):
            print(f"logo: no ack after {sent} chunks", file=sys.stderr)
        sent += 1
    print(f"logo for {name}: {len(data)} bytes in {sent} chunks", flush=True)


# Marks the knob shows when there is no artwork to show: the panel is doing
# something that has a symbol rather than a cover.
#
# Drawn rather than shipped. Each is a handful of primitives, and generating
# them means one less set of binary assets to install, to keep in step with
# the knob's size, and to account for the provenance of. Kept on disk once
# drawn, because the knob's encoder wants a file and redrawing on every push
# would be work for the same bytes.
ICON_DIR = "/var/lib/comand"
ICON_ASSET_DIR = "/usr/local/share/comand-ui/icons"


def knob_icon(name: str, paint) -> str | None:
    """A mark for the knob: the one installed with the UI, or a drawn one.

    An installed file wins. Some of these marks are somebody's drawing rather
    than a shape with a description -- the navigation arrow is -- and
    approximating those in code gets close and stays wrong. The painters
    remain for the marks that really are a few primitives, and as the answer
    when an asset is missing.

    Drawn at 240px and scaled down by knob_image, so a mark stays sharp
    whichever of the knob's sizes a picture ends up at.
    """
    shipped = os.path.join(ICON_ASSET_DIR, f"{name}.png")
    if os.path.exists(shipped):
        return shipped
    path = os.path.join(ICON_DIR, f"{name}.png")
    if os.path.exists(path):
        return path
    if paint is None:
        return None                  # shipped-only mark, and it is not there
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None                  # no python3-pil: the name still shows
    n = 240
    img = Image.new("RGB", (n, n), (0, 0, 0))
    paint(ImageDraw.Draw(img), n)
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
        img.save(path)
    except Exception as e:
        print(f"{name} icon: {e}", file=sys.stderr)
        return None
    return path


WHITE, BLACK = (255, 255, 255), (0, 0, 0)


def _paint_mirror(d, n: int) -> None:
    """One screen repeating another: the mark everything uses for mirroring.

    Two offset rounded rectangles, the front one filled with the background so
    it covers the lines it crosses rather than letting them run through it.
    That overlap is the whole mark -- without it the two read as a pair of
    boxes instead of one screen showing what is on another.
    """
    stroke, radius = max(3, n // 26), int(n * 0.06)

    def screen(x0, y0, x1, y1, fill=None):
        d.rounded_rectangle([int(n * x0), int(n * y0), int(n * x1), int(n * y1)],
                            radius=radius, outline=WHITE, width=stroke, fill=fill)

    screen(0.18, 0.26, 0.65, 0.60)
    screen(0.35, 0.39, 0.82, 0.73, fill=BLACK)


def _paint_nav(d, n: int) -> None:
    """A heading arrow in a ring: the mark for navigation.

    The arrow's base is notched rather than flat, which is what stops it
    reading as a plain triangle -- at 80px on a round screen that notch is the
    only thing distinguishing "going somewhere" from "warning".
    """
    stroke = max(3, n // 22)
    m = int(n * 0.06)
    d.ellipse([m, m, n - m, n - m], outline=WHITE, width=stroke)
    d.polygon([(int(n * 0.80), int(n * 0.20)),      # tip, pointing up and out
               (int(n * 0.20), int(n * 0.47)),      # left tail
               (int(n * 0.47), int(n * 0.53)),      # the notch, at the centre
               (int(n * 0.53), int(n * 0.80))],     # lower tail
              fill=WHITE)


def _paint_youtube(d, n: int) -> None:
    """A white play triangle on a red rounded rectangle."""
    d.rounded_rectangle([int(n * 0.06), int(n * 0.20), int(n * 0.94), int(n * 0.80)],
                        radius=int(n * 0.16), fill=(255, 0, 0))
    d.polygon([(int(n * 0.40), int(n * 0.36)), (int(n * 0.40), int(n * 0.64)),
               (int(n * 0.66), int(n * 0.50))], fill=WHITE)


def _paint_film(d, n: int) -> None:
    """A film strip: a white-outlined frame with perforations top and bottom."""
    stroke = max(3, n // 26)
    d.rounded_rectangle([int(n * 0.14), int(n * 0.24), int(n * 0.86), int(n * 0.76)],
                        radius=int(n * 0.05), outline=WHITE, width=stroke)
    hole = int(n * 0.055)
    for i in range(5):
        x = int(n * 0.20) + i * int(n * 0.13)
        d.rectangle([x, int(n * 0.28), x + hole, int(n * 0.28) + hole], fill=WHITE)
        d.rectangle([x, int(n * 0.72) - hole, x + hole, int(n * 0.72)], fill=WHITE)


def _paint_tv(d, n: int) -> None:
    """A screen on a stand: the mark for a television channel."""
    stroke = max(3, n // 26)
    d.rounded_rectangle([int(n * 0.16), int(n * 0.26), int(n * 0.84), int(n * 0.64)],
                        radius=int(n * 0.06), outline=WHITE, width=stroke)
    d.line([(int(n * 0.40), int(n * 0.74)), (int(n * 0.47), int(n * 0.64))], fill=WHITE, width=stroke)
    d.line([(int(n * 0.60), int(n * 0.74)), (int(n * 0.53), int(n * 0.64))], fill=WHITE, width=stroke)
    d.line([(int(n * 0.34), int(n * 0.74)), (int(n * 0.66), int(n * 0.74))], fill=WHITE, width=stroke)


def mirror_icon() -> str | None:
    return knob_icon("mirror", _paint_mirror)


def nav_icon() -> str | None:
    return knob_icon("nav", _paint_nav)


# What picture the knob is holding, as one key for both things that put one
# there: "logo:<station>" or "cover:<file>".
#
# It was two variables, and only the cover's was checked. The station logo
# would overwrite the cover on the knob without saying so, and the next track
# from the same album then matched the cover key and was skipped as already
# shown -- so the knob kept a station logo through an album. One screen, one
# record of what is on it.
knob_picture = ""


def push_knob_cover(media: dict) -> None:
    """Put the phone's album art and track on the knob.

    The same face the radio uses, and the same transfer: the knob has one way
    of showing "a picture and what it is", and a second one would be a second
    thing to keep working.

    Black behind the art rather than a station colour -- a cover is a
    photograph with its own edges, and a coloured surround would read as part
    of it.
    """
    global knob_picture
    # A phone that was left playing keeps reporting its track, and the video on
    # the panel is what the knob is showing and controlling.
    with lock:
        if (state["nowvideo"] or {}).get("playing"):
            return
    title = media.get("title") or media.get("artist") or "Bluetooth"
    playing = media.get("status") == "playing"
    push_knob({"m": "radio"})
    push_knob({"radio": {"name": title, "playing": playing}})

    art = media.get("art")
    if not art:
        # No picture rather than the last track's: the name takes its place.
        if knob_picture:
            push_knob({"radio": {"logo": {"w": 0, "h": 0, "bytes": 0}}})
            knob_picture = ""
        return
    want = f"cover:{art}"
    if want == knob_picture:
        return                      # same album, and it is what is on there
    made = knob_image(os.path.join(COVER_DIR, art), (0, 0, 0))
    if not made:
        # Remembered as attempted, or every metadata update retries an
        # encode that cannot succeed -- which is what filled the log.
        knob_picture = want
        return
    w, h, data = made
    push_knob({"radio": {"logo": {"w": w, "h": h, "bytes": len(data)}}})
    for i in range(0, len(data), KNOB_CHUNK):
        logo_ack.clear()
        push_knob({"radio": {"chunk": base64.b64encode(
            data[i:i + KNOB_CHUNK]).decode()}})
        logo_ack.wait(0.5)
    knob_picture = want
    print(f"cover on the knob: {art}, {len(data)} bytes", flush=True)


def push_knob_nav() -> None:
    """The knob while the car is navigating and nothing is playing.

    The same picture-and-name face as everything else here. There is no
    transport to speak of, but the alternative was a blank resting screen
    while the car was actually doing something, which tells the driver less
    than the mark does.
    """
    global knob_picture
    push_knob({"m": "radio"})
    push_knob({"radio": {"name": "Navigation", "playing": False}})
    if knob_picture == "nav":
        return
    path = nav_icon()
    if not path:
        return
    made = knob_image(path, (0, 0, 0))
    if not made:
        knob_picture = "nav"
        return
    w, h, data = made
    push_knob({"radio": {"logo": {"w": w, "h": h, "bytes": len(data)}}})
    for k in range(0, len(data), KNOB_CHUNK):
        logo_ack.clear()
        push_knob({"radio": {"chunk": base64.b64encode(
            data[k:k + KNOB_CHUNK]).decode()}})
        logo_ack.wait(0.5)
    knob_picture = "nav"
    print(f"navigation mark on the knob: {len(data)} bytes", flush=True)


# How long an announcement stays up, on both screens. Long enough to read a
# phone's name at a glance and short enough that it is gone before it is in
# the way -- and the same number at both ends, so the two clear together
# rather than one lingering after the other.
TOAST_S = 4.0


# Going to sleep is not instant -- the radio fades over more than a second,
# and the knob is told over a serial link -- so a wake can arrive while it is
# still happening. Without this lock the two interleaved: the wake turned the
# panel back on, and the sleep still in flight reached its last line and
# turned it off again, leaving a car that was awake by every measure except
# the screen. Which looks exactly like the UI having broken.
standby_lock = threading.Lock()


def set_standby(on: bool) -> None:
    """Take the car's screens down, or bring them back.

    The panel's backlight is the single biggest draw here and the knob's is
    the one that is annoying in a dark car at night, so both go off. Audio is
    paused rather than muted: a radio playing to a dark car is still a radio
    playing, and a phone left streaming to a receiver nobody can see keeps the
    phone awake too.

    Nothing is resumed on the way back. A driver who wakes the car is looking
    at it, and having sound start by itself from a screen that was dark a
    second ago is worse than pressing play.
    """
    with standby_lock:
        with lock:
            if state["standby"] == on:
                return
            state["standby"] = on

        if on:
            print("standby", flush=True)
            radio_stand_down()
            bt_stand_down()
            push_knob({"m": "sleep"})
            # The output last: everything above talks to devices, and doing it
            # after the screen is dark would be work nobody can see going
            # wrong.
            sway("output * power off")
        else:
            print("awake", flush=True)
            sway("output * power on")
            knob_resting()
    publish()


def log_aux(aux: dict) -> None:
    """Put one reading in the log, and keep the log from growing for ever."""
    global _pruned_at
    try:
        history.record(aux)
    except Exception as e:
        print(f"history: {e}", file=sys.stderr, flush=True)
        return
    # Once a day, from whichever reading happens to arrive after the day
    # turns. A timer would be another thread to own for a job that has no
    # deadline.
    if time.time() - _pruned_at > 86400:
        _pruned_at = time.time()
        try:
            gone = history.prune()
            if gone:
                print(f"history: pruned {gone} old rows", flush=True)
        except Exception as e:
            print(f"history prune: {e}", file=sys.stderr, flush=True)


_pruned_at = time.time()


def clear_toast(at: float) -> None:
    """Take an announcement down, unless a newer one has replaced it."""
    time.sleep(TOAST_S)
    with lock:
        current = state["toast"]
        if not current or current.get("at") != at:
            return
        state["toast"] = None
    publish()


def push_knob_toast(text: str) -> None:
    """Say something on the knob for a moment, then put back what was there.

    Reuses the picture-and-name face, because the knob has one and a second
    way of showing a mark with a caption would be a second thing to keep
    working. What restores it is knob_resting, so the knob goes back to
    whatever the car is doing now rather than to whatever it was showing when
    this started -- those differ if a phone connects while the driver is
    changing stations.
    """
    global knob_picture
    push_knob({"m": "radio"})
    push_knob({"radio": {"name": text, "playing": False}})
    path = knob_icon("bluetooth", None)
    if path:
        made = knob_image(path, (0, 0, 0))
        if made:
            w, h, data = made
            push_knob({"radio": {"logo": {"w": w, "h": h, "bytes": len(data)}}})
            for k in range(0, len(data), KNOB_CHUNK):
                logo_ack.clear()
                push_knob({"radio": {"chunk": base64.b64encode(
                    data[k:k + KNOB_CHUNK]).decode()}})
                logo_ack.wait(0.5)
            knob_picture = "bluetooth"
            print(f"knob says: {text} ({len(data)} bytes)", flush=True)
    time.sleep(TOAST_S)
    # Only if nothing else has claimed the screen since -- a call arriving
    # during the announcement owns the knob, and putting the resting face up
    # over it would be worse than leaving the announcement.
    if knob_picture == "bluetooth":
        knob_resting()


def knob_clock() -> None:
    """Keep the time on the knob's resting face.

    The firmware draws this on its idle screen and ships with "--:--" in the
    field as a placeholder. Nothing ever sent a real one, so the placeholder
    was the clock: browse the menu, stop, and five seconds later it times out
    to a resting face with dashes where the time should be.

    Pushed when the minute turns rather than on a fixed interval, so what is
    on the screen is never a stale copy of a minute that has already passed.
    """
    last = ""
    while True:
        now = time.strftime("%H:%M")
        if now != last:
            last = now
            push_knob({"idle": {"clock": now}})
        # Just after the next minute turns.
        time.sleep(60 - (time.time() % 60) + 0.5)


def idle_volts() -> float | None:
    """The one number the resting face shows: the battery voltage. The aux bank
    if it is being watched, otherwise the car's own battery from the vehicle
    feed. None when neither is known, so the firmware keeps its hint row."""
    with lock:
        aux = state.get("aux") or {}
        veh = state.get("vehicle") or {}
    for v in (aux.get("volts"), veh.get("battery_v")):
        try:
            return round(float(v), 1)
        except (TypeError, ValueError):
            continue
    return None


def knob_volts() -> None:
    """Keep the resting face's voltage fresh. Slow on purpose -- a battery at
    rest is a number that creeps -- and only sent when it moves."""
    last = None
    while True:
        v = idle_volts()
        if v is not None and v != last:
            last = v
            push_knob({"idle": {"volts": v}})
        time.sleep(15)


def knob_resting() -> None:
    """What the knob shows when whatever it was showing has finished.

    One place decides, because the knob holds one picture and the ways of
    putting one there have grown: a station logo, album art, the mirroring
    mark, the navigation mark. Each used to restore the knob its own way, and
    the mirroring mark stayed up after the phone had stopped -- the mark said
    the panel was showing the phone's screen when the panel had gone back to
    the menu.

    Whatever is playing wins, because that is what the knob's transport acts
    on. Only when nothing is does the source decide, and only navigation has
    something to say; anything else gets the resting face.

    knob_picture is cleared before the resting face so the next picture is
    actually sent: the knob is still holding the old one, and a picture the
    bridge believes is already up is a picture it will not send again.
    """
    global knob_picture
    with lock:
        source = state["source"]
        station, playing = state["radio"], state["playing"]
        media = state["btmedia"] or {}
        in_call = bool(state["call"])
        video = state["nowvideo"]

    # A number on the keypad comes first: it is what the driver is doing right
    # now, and the knob's dial button is the next thing they will reach for.
    if dial_typed and not in_call:
        push_knob_dial()
        return

    if video and video.get("playing"):
        push_knob_video(video)
        return
    if playing and station:
        push_knob_radio(station, playing, with_logo=True)
        return
    if media.get("status") == "playing":
        push_knob_cover(media)
        return
    if video:                        # paused, and nothing else is playing
        push_knob_video(video)
        return
    if source == "Navigation":
        push_knob_nav()
        return
    knob_picture = ""
    push_knob({"idle": {"clock": time.strftime("%H:%M")}})
    push_knob({"m": "idle"})


def push_knob_mirror(media: dict | None) -> None:
    """The knob while the phone's screen is on the panel.

    The same face as the radio and the same face as Bluetooth music, because
    the knob has one way of showing "a picture and what it is" and a second
    would be a second thing to keep working. What changes is what fills it:
    the mirroring mark instead of artwork, and whatever the phone says it is
    playing instead of a station.

    AirPlay mirroring carries no metadata -- it is a video stream and nothing
    else -- so the title comes from AVRCP over the Bluetooth link the phone
    already has for calls. That is the same channel the knob's play/pause and
    swipes use to control it, so when there is a title there is also control,
    and when there is neither the mark alone still says what the panel is
    doing.
    """
    global knob_picture
    title, playing = "", True
    if media:
        title = media.get("title") or media.get("artist") or ""
        if media.get("status"):
            playing = media.get("status") == "playing"

    push_knob({"m": "radio"})
    push_knob({"radio": {"name": title or "Screen Mirroring",
                         "playing": playing}})

    # The mark does not change with the track, so it is sent once and then
    # left alone: every metadata update would otherwise re-ship 24 kB over a
    # serial link to redraw the same picture.
    if knob_picture == "mirror":
        return
    path = mirror_icon()
    if not path:
        return
    made = knob_image(path, (0, 0, 0))
    if not made:
        knob_picture = "mirror"
        return
    w, h, data = made
    push_knob({"radio": {"logo": {"w": w, "h": h, "bytes": len(data)}}})
    for i in range(0, len(data), KNOB_CHUNK):
        logo_ack.clear()
        push_knob({"radio": {"chunk": base64.b64encode(
            data[i:i + KNOB_CHUNK]).decode()}})
        logo_ack.wait(0.5)
    knob_picture = "mirror"
    print(f"mirroring mark on the knob: {len(data)} bytes", flush=True)


def push_knob_youtube(video: dict) -> None:
    """The knob during a YouTube video: the logo and the video's title."""
    global knob_picture
    push_knob({"m": "radio"})
    push_knob({"radio": {"name": video.get("title") or "YouTube",
                         "playing": bool(video.get("playing"))}})
    if knob_picture == "youtube":
        return
    # Marked first: the title arrives seconds into the transfer, and its update
    # must not start a second transfer alongside this one.
    knob_picture = "youtube"
    path = knob_icon("youtube", _paint_youtube)
    made = knob_image(path, (0, 0, 0)) if path else None
    if not made:
        return
    w, h, data = made
    push_knob({"radio": {"logo": {"w": w, "h": h, "bytes": len(data)}}})
    for i in range(0, len(data), KNOB_CHUNK):
        logo_ack.clear()
        push_knob({"radio": {"chunk": base64.b64encode(
            data[i:i + KNOB_CHUNK]).decode()}})
        logo_ack.wait(0.5)
    print(f"YouTube logo on the knob: {len(data)} bytes", flush=True)


def push_knob_video(video: dict) -> None:
    """The knob during a film, show, channel or YouTube video: its title and a
    mark for what it is. kind comes from how it was started; a video the panel
    reported (YouTube only, no kind) keeps the YouTube mark."""
    global knob_picture
    icon, paint, fallback = {
        "tv":    ("tv",   _paint_tv,   "Live TV"),
        "movie": ("film", _paint_film, "Video"),
        "show":  ("film", _paint_film, "Video"),
        "plex":  ("film", _paint_film, "Video"),
        "film":  ("film", _paint_film, "Video"),
    }.get((video or {}).get("kind") or "youtube", ("youtube", _paint_youtube, "YouTube"))
    push_knob({"m": "radio"})
    push_knob({"radio": {"name": video.get("title") or fallback,
                         "playing": bool(video.get("playing"))}})
    if knob_picture == icon:
        return
    knob_picture = icon
    path = knob_icon(icon, paint)
    made = knob_image(path, (0, 0, 0)) if path else None
    if not made:
        return
    w, h, data = made
    push_knob({"radio": {"logo": {"w": w, "h": h, "bytes": len(data)}}})
    for i in range(0, len(data), KNOB_CHUNK):
        logo_ack.clear()
        push_knob({"radio": {"chunk": base64.b64encode(
            data[i:i + KNOB_CHUNK]).decode()}})
        logo_ack.wait(0.5)
    print(f"{icon} logo on the knob: {len(data)} bytes", flush=True)


def toggle_radio() -> None:
    """Pause or resume whatever is playing.

    MPD's bare `pause` toggles, but it is deprecated and does nothing at all
    when the player is stopped -- which is exactly the state a car ends up in
    after a stream drops. Reading the state first and choosing means the button
    always does the obvious thing: a stopped station starts again.
    """
    st = (mpd_status().get("state") or "").strip()
    if st == "play":
        mpd("pause 1")
        playing = False
    elif st == "pause":
        mpd("pause 0")
        playing = True
    else:
        mpd("play")
        playing = True
    with lock:
        state["playing"] = playing
        name = state["radio"]
    if name:
        push_knob({"radio": {"playing": playing}})
    publish()


def stop_radio() -> None:
    mpd("stop", "clear")
    with lock:
        state["radio"] = ""
        state["song"] = ""
        state["playing"] = False
    # Back to the volume face: a logo for a station that is not playing is a
    # knob lying about what the car is doing.
    push_knob({"m": "idle"})
    publish()


# COMAND picture settings. ui/video/comand-video.py holds sway's gamma tables
# for the output that feeds COMAND and re-reads this file when it changes; the
# bridge only keeps the numbers. Each is -100..100, 0 = unchanged.
VIDEO_PATH = "/var/lib/comand/video.json"
# What "Reset to defaults" goes back to: the values tuned for this COMAND's
# screen and wiring ({"video": {"save_default": true}} stores the current ones).
# Without it, reset means 0 -- the signal exactly as the laptop sends it.
VIDEO_DEFAULT_PATH = "/var/lib/comand/video-default.json"
VIDEO_KEYS = ("brightness", "contrast", "gamma", "red", "green", "blue")


def load_video(path: str = VIDEO_PATH) -> dict:
    try:
        with open(path) as fh:
            v = json.load(fh)
    except Exception:
        v = {}
    return {k: int(max(-100, min(100, float(v.get(k, 0) or 0)))) for k in VIDEO_KEYS}


def save_video(v: dict, path: str = VIDEO_PATH) -> None:
    # Replace, not rewrite in place: the daemon may read it at any moment.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(v, fh)
        os.replace(tmp, path)
    except Exception as e:
        print(f"could not save video settings: {e}", file=sys.stderr)


def set_video(changes: dict) -> None:
    """Change picture settings from any side (menu, /cmd, Home Assistant).
    Keys not given stay; {"reset": true} first puts them all back to the saved
    defaults; {"save_default": true} makes the current values the defaults."""
    if changes.get("save_default"):
        with lock:
            save_video(dict(state["video"]), VIDEO_DEFAULT_PATH)
        return
    if "card" in changes:
        set_card(bool(changes["card"]))
        return
    with lock:
        v = dict(state["video"])
        if changes.get("reset"):
            v = load_video(VIDEO_DEFAULT_PATH)
        for k in VIDEO_KEYS:
            if k in changes:
                try:
                    v[k] = int(max(-100, min(100, float(changes[k]))))
                except (TypeError, ValueError):
                    pass
        state["video"] = v
    save_video(v)
    publish()
    ha_video_state(v)


# Home Assistant in the car, over its Mosquitto broker. The box finds HA rather
# than HA finding the box: HA's address is fixed (on the car network, and on
# a VPN), the box's is whatever the car's router hands out.
# MQTT discovery makes a "COMAND" device appear in HA with the picture
# settings as number entities, so the phone app sets them through HA like any
# other entity and HA keeps their history. Login: HA_MQTT_USER, password in
# ~/.config/comand/mqtt-pass (a login of its own in the Mosquitto add-on).
HA_MQTT_HOSTS = os.environ.get("HA_HOSTS", "").split()     # car network first, then the tailnet
HA_PASS_PATH = os.path.expanduser("~/.config/comand/mqtt-pass")
HA_BASE = "comand"
HA_DEVICE = {"identifiers": ["comand-head-unit"], "name": "COMAND",
             "manufacturer": "COMAND", "model": "COMAND 2.0 head unit"}
ha_client = None
HTTP_PORT = 8080        # the port /cmd is served on; set in main(), used by local_cmd
card_timer = None
CARD_MAX_S = 600        # the card goes away by itself if the phone that asked for it never says so


def set_card(on: bool) -> None:
    """The picture calibration card over everything on COMAND's screen (main-menu.html #calib)."""
    global card_timer
    if card_timer:
        card_timer.cancel()
        card_timer = None
    if on:
        card_timer = threading.Timer(CARD_MAX_S, set_card, args=(False,))
        card_timer.daemon = True
        card_timer.start()
    with lock:
        state["calib"] = on
    publish()
    c = ha_client
    if c is not None and c.is_connected():
        c.publish(f"{HA_BASE}/picture/card/state", "ON" if on else "OFF", qos=1, retain=True)


def ha_video_state(v: dict) -> None:
    c = ha_client
    if c is not None and c.is_connected():
        c.publish(f"{HA_BASE}/picture/state", json.dumps(v), qos=1, retain=True)


def ha_sound_state() -> None:
    """The sound settings the app and HA share, as one retained JSON blob so
    the switch/select entities all read from a single state topic."""
    c = ha_client
    if c is None or not c.is_connected():
        return
    with lock:
        out = {"speed_volume": "ON" if state.get("speedVolume") else "OFF",
               "speed_volume_strength": state.get("speedVolumeStrength") or "medium",
               "profile_per_source": "ON" if state.get("profilePerSource") else "OFF"}
    c.publish(f"{HA_BASE}/sound/state", json.dumps(out), qos=1, retain=True)


def ha_favourites() -> None:
    """The favourites list for the phone app to read. A sensor whose state is the
    count and whose attributes carry the list, published retained so the app has
    it the moment it connects; the app adds/removes/plays by writing back to
    comand/favourites/cmd (through HA's mqtt.publish), the same shape as /cmd."""
    c = ha_client
    if c is None or not c.is_connected():
        return
    with lock:
        favs = list(state.get("favourites") or [])
    c.publish(f"{HA_BASE}/favourites/state",
              json.dumps({"count": len(favs), "favourites": favs}), qos=1, retain=True)


HA_CONTROLS = {"play_pause": ("Play pause", "mdi:play-pause"), "next": ("Next", "mdi:skip-next"),
               "previous": ("Previous", "mdi:skip-previous"), "volume_up": ("Volume up", "mdi:volume-plus"),
               "volume_down": ("Volume down", "mdi:volume-minus"), "mute": ("Mute", "mdi:volume-mute")}


def local_cmd(cmd: dict) -> None:
    """POST a command to our own /cmd, so a message that arrives over MQTT runs
    through the one settings dispatch rather than a second copy of it."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{HTTP_PORT}/cmd",
                                     json.dumps(cmd).encode(), method="POST")
        urllib.request.urlopen(req, timeout=5).read()
    except OSError as e:
        print(f"local_cmd: {e}", file=sys.stderr)


def to_carplay(word: str) -> None:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(word.encode(), ("127.0.0.1", 7340))


def head_control(name: str) -> None:
    """A button from Home Assistant, on whatever COMAND is playing now."""
    with lock:
        if name in ("volume_up", "volume_down"):
            state["vol"] = max(0, min(100, state["vol"] + (5 if name == "volume_up" else -5)))
            state["muted"] = False
        elif name == "mute":
            state["muted"] = not state["muted"]
        vol, muted = state["vol"], state["muted"]
        radio, playing, source = state["radio"], state["playing"], state["source"]
        carplay, bt = state["carplayConn"], state["btmedia"]
    if name in ("volume_up", "volume_down", "mute"):
        set_output_volume(vol, muted)
        publish()
        return
    print(f"head unit control: {name}, source {source}", flush=True)
    if radio and (playing or source == "Radio"):
        if name == "play_pause":
            toggle_radio()
        else:
            names = [s["name"] for s in load_stations()]
            i = names.index(radio) if radio in names else 0
            play_station(names[(i + (1 if name == "next" else -1)) % len(names)])
    elif carplay:
        to_carplay({"play_pause": "playpause"}.get(name, name))
    elif bt:
        bt_control({"play_pause": "toggle"}.get(name, name))
    else:
        req = urllib.request.Request("http://127.0.0.1:8091/ctl", json.dumps({"action": name}).encode(), method="POST")
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except OSError as e:
            print(f"head unit control: {e}", file=sys.stderr)


# A remote for COMAND's menu (a phone app, through HA's Mosquitto): key names on comand/remote/key go to
# ui/comand-keys.py, which treats them exactly as the same key on COMAND (menu, CarPlay, video, holds).
# What is on screen goes back on comand/remote/screen (sensor.comand_screen in HA) for the remote to show.
REMOTE_KEYS = {"up", "down", "enter", "back", "back-hold", "del", "track-", "track+",
               *(f"d{c}" for c in "0123456789*#")}
REMOTE_ADDR = ("127.0.0.1", 7344)
_remote_udp = None
_screen_sent = {"k": None}


def remote_key(name: str) -> None:
    global _remote_udp
    if name not in REMOTE_KEYS:
        print(f"remote: ignored {name[:20]!r}", file=sys.stderr); return
    import socket
    if _remote_udp is None:
        _remote_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _remote_udp.sendto(name.encode(), REMOTE_ADDR)


def publish_screen() -> None:
    """{path, focus, source, menu} on comand/remote/screen, retained, when it changes."""
    with lock:
        sc = dict(state.get("screenInfo") or {})
        out = {"path": [str(x)[:40] for x in (sc.get("path") or [])][:8], "focus": str(sc.get("focus") or "")[:80],
               "source": str(state.get("source") or ""), "menu": bool(state.get("menuOpen"))}
    k = json.dumps(out, separators=(",", ":"))
    c = ha_client
    if k != _screen_sent["k"] and c is not None and c.is_connected():
        c.publish(f"{HA_BASE}/remote/screen", k, qos=1, retain=True)
        _screen_sent["k"] = k


def screen_ticker() -> None:
    while True:
        try:
            publish_screen()
        except Exception as e:      # never let the ticker die
            print(f"screen: {e}", file=sys.stderr)
        time.sleep(0.5)


def ha_link() -> None:
    global ha_client
    try:
        import paho.mqtt.client as mqtt         # python3-paho-mqtt
        with open(HA_PASS_PATH) as fh:
            password = fh.read().strip()
    except Exception as e:
        print(f"HA link off: {e}", file=sys.stderr)
        return

    def on_connect(c, u, flags, rc, props=None):
        if rc != 0:
            return
        c.publish(f"{HA_BASE}/status", "online", qos=1, retain=True)
        avail = {"availability_topic": f"{HA_BASE}/status", "device": HA_DEVICE}
        names = {"brightness": "Picture brightness", "contrast": "Picture contrast", "gamma": "Picture gamma",
                 "red": "Picture red", "green": "Picture green", "blue": "Picture blue"}
        for k in VIDEO_KEYS:
            c.publish(f"homeassistant/number/comand_picture_{k}/config", json.dumps({
                "name": names[k], "unique_id": f"comand_picture_{k}", "object_id": f"comand_picture_{k}",
                "command_topic": f"{HA_BASE}/picture/{k}/set", "state_topic": f"{HA_BASE}/picture/state",
                "value_template": "{{ value_json.%s }}" % k, "min": -100, "max": 100, "step": 5,
                "mode": "slider", "icon": "mdi:television", **avail}), qos=1, retain=True)
        c.publish("homeassistant/button/comand_picture_reset/config", json.dumps({
            "name": "Picture reset", "unique_id": "comand_picture_reset", "object_id": "comand_picture_reset",
            "command_topic": f"{HA_BASE}/picture/reset/set", "payload_press": "reset",
            "icon": "mdi:restore", **avail}), qos=1, retain=True)
        c.publish("homeassistant/switch/comand_picture_card/config", json.dumps({
            "name": "Picture calibration card", "unique_id": "comand_picture_card", "object_id": "comand_picture_card",
            "command_topic": f"{HA_BASE}/picture/card/set", "state_topic": f"{HA_BASE}/picture/card/state",
            "icon": "mdi:television-guide", **avail}), qos=1, retain=True)
        c.publish("homeassistant/button/comand_picture_save_default/config", json.dumps({
            "name": "Picture save as default", "unique_id": "comand_picture_save_default",
            "object_id": "comand_picture_save_default", "command_topic": f"{HA_BASE}/picture/save_default/set",
            "payload_press": "save", "icon": "mdi:content-save", **avail}), qos=1, retain=True)
        with lock:
            card = bool(state.get("calib"))
        c.publish(f"{HA_BASE}/picture/card/state", "ON" if card else "OFF", qos=1, retain=True)
        c.subscribe(f"{HA_BASE}/picture/+/set", qos=1)
        for k, (name, icon) in HA_CONTROLS.items():
            c.publish(f"homeassistant/button/comand_{k}/config", json.dumps({
                "name": name, "unique_id": f"comand_{k}", "object_id": f"comand_{k}",
                "default_entity_id": f"button.comand_{k}", "command_topic": f"{HA_BASE}/control/{k}/set",
                "payload_press": "press", "icon": icon, **avail}), qos=1, retain=True)
        c.subscribe(f"{HA_BASE}/control/+/set", qos=1)
        # Sound: the per-source profile switch, and speed-dependent volume
        # (on/off + strength). One retained state topic, comand/sound/state,
        # feeds all three so the app and HA never disagree on them.
        c.publish("homeassistant/switch/comand_speed_volume/config", json.dumps({
            "name": "Speed volume", "unique_id": "comand_speed_volume",
            "object_id": "comand_speed_volume", "command_topic": f"{HA_BASE}/sound/speed_volume/set",
            "state_topic": f"{HA_BASE}/sound/state", "value_template": "{{ value_json.speed_volume }}",
            "icon": "mdi:speedometer", **avail}), qos=1, retain=True)
        c.publish("homeassistant/select/comand_speed_volume_strength/config", json.dumps({
            "name": "Speed volume strength", "unique_id": "comand_speed_volume_strength",
            "object_id": "comand_speed_volume_strength",
            "command_topic": f"{HA_BASE}/sound/speed_volume_strength/set",
            "state_topic": f"{HA_BASE}/sound/state",
            "value_template": "{{ value_json.speed_volume_strength }}",
            "options": ["low", "medium", "high"], "icon": "mdi:volume-vibrate", **avail}), qos=1, retain=True)
        c.publish("homeassistant/switch/comand_sound_profile/config", json.dumps({
            "name": "Per-source sound profile", "unique_id": "comand_sound_profile",
            "object_id": "comand_sound_profile", "command_topic": f"{HA_BASE}/sound/profile_per_source/set",
            "state_topic": f"{HA_BASE}/sound/state", "value_template": "{{ value_json.profile_per_source }}",
            "icon": "mdi:tune-variant", **avail}), qos=1, retain=True)
        c.subscribe(f"{HA_BASE}/sound/+/set", qos=1)
        # Favourites: a sensor the app reads (count as state, the list in
        # attributes) and a command topic it writes to add/remove/play.
        c.publish("homeassistant/sensor/comand_favourites/config", json.dumps({
            "name": "Favourites", "unique_id": "comand_favourites", "object_id": "comand_favourites",
            "state_topic": f"{HA_BASE}/favourites/state", "value_template": "{{ value_json.count }}",
            "json_attributes_topic": f"{HA_BASE}/favourites/state", "icon": "mdi:star", **avail}), qos=1, retain=True)
        c.subscribe(f"{HA_BASE}/favourites/cmd", qos=1)
        c.publish("homeassistant/sensor/comand_screen/config", json.dumps({
            "name": "Screen", "unique_id": "comand_screen", "object_id": "comand_screen",
            "default_entity_id": "sensor.comand_screen", "state_topic": f"{HA_BASE}/remote/screen",
            "value_template": "{{ value_json.focus }}", "json_attributes_topic": f"{HA_BASE}/remote/screen",
            "icon": "mdi:remote", **avail}), qos=1, retain=True)
        c.subscribe(f"{HA_BASE}/remote/key", qos=0)
        _screen_sent["k"] = None            # republish what is on screen after a reconnect
        with lock:
            v = dict(state["video"])
        c.publish(f"{HA_BASE}/picture/state", json.dumps(v), qos=1, retain=True)
        ha_sound_state()
        ha_favourites()
        print(f"HA link up ({c._host})", file=sys.stderr)

    def on_message(c, u, msg):
        if msg.topic == f"{HA_BASE}/remote/key":
            remote_key(msg.payload.decode(errors="replace").strip())
            return
        if msg.topic == f"{HA_BASE}/favourites/cmd":
            try:
                fc = json.loads(msg.payload.decode(errors="replace"))
            except ValueError:
                return
            if isinstance(fc, dict):
                handle_favourite(fc)
            return
        k = msg.topic.split("/")[-2]
        if "/control/" in msg.topic:
            if k in HA_CONTROLS:
                threading.Thread(target=head_control, args=(k,), daemon=True).start()
        elif "/sound/" in msg.topic:
            # Route back through the one settings dispatch (POST /cmd) rather
            # than a second copy of it, so persistence and publish happen once.
            payload = msg.payload.decode(errors="replace").strip()
            if k in ("speed_volume", "profile_per_source"):
                val = payload.upper() == "ON"
            elif k == "speed_volume_strength":
                val = payload
            else:
                return
            threading.Thread(target=local_cmd, args=({"setting": {k: val}},), daemon=True).start()
        elif k == "reset":
            set_video({"reset": True})
        elif k == "card":
            set_card(msg.payload.decode().upper() == "ON")
        elif k == "save_default":
            set_video({"save_default": True})
        elif k in VIDEO_KEYS:
            try:
                set_video({k: float(msg.payload.decode())})
            except ValueError:
                pass

    while True:
        for host in HA_MQTT_HOSTS:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="comand-head-unit")
            c.username_pw_set(os.environ.get("HA_MQTT_USER", ""), password)
            c.will_set(f"{HA_BASE}/status", "offline", qos=1, retain=True)
            c.on_connect, c.on_message = on_connect, on_message
            try:
                c.connect(host, 1883, keepalive=30)
            except OSError:
                continue
            ha_client = c
            c.loop_forever(retry_first_connection=False)    # returns when the connection is lost for good
            ha_client = None
        time.sleep(15)                                       # neither reachable: car off, or no tailnet yet


def apply_settings_to_state() -> None:
    """Put persisted settings where the screens can see them."""
    with lock:
        state["video"] = load_video()
        if settings.get("hand") in ("left", "right"):
            state["hand"] = settings["hand"]
        if isinstance(settings.get("warn_vol"), int):
            state["warnVol"] = settings["warn_vol"]
        if settings.get("map_freq") in MAP_FREQS:
            state["mapFreq"] = settings["map_freq"]
        if isinstance(settings.get("map_countries"), list) and settings["map_countries"]:
            state["mapCountries"] = settings["map_countries"]
        try:
            state["mapUpdated"] = time.strftime(
                "%Y-%m-%d", time.localtime(os.path.getmtime("/var/lib/comand-speed/speedlimits.sqlite")))
        except OSError:
            pass
        if settings.get("warn_style") in WARN_STYLES:
            state["warnStyle"] = settings["warn_style"]
        if settings.get("voice_sound") in VOICE_SOUNDS:
            state["voiceSound"] = settings["voice_sound"]
        if settings.get("nav_app") in NAV_APPS:
            state["navApp"] = settings["nav_app"]
        if settings.get("wake_word") in WAKE_WORDS:
            state["wakeWord"] = settings["wake_word"]
        if settings.get("vol_style") in VOL_STYLES:
            state["volStyle"] = settings["vol_style"]
        state["ble"] = bool(settings.get("ble", True))
        state["carplay"] = bool(settings.get("carplay", True))
        state["phoneBt"] = bool(settings.get("phone_bt", True))
        state["micSerial"] = str(settings.get("mic_serial", ""))
        state["output"] = str(settings.get("output", ""))
        state["eqOn"] = bool(settings.get("eq_on", True))
        state["sound"] = dict(settings.get("sound") or {})
        state["speedVolume"] = bool(settings.get("speed_volume", False))
        state["speedVolumeStrength"] = settings.get("speed_volume_strength", "medium")
        state["profilePerSource"] = bool(settings.get("profile_per_source", False))
        state["eqProfile"] = os.path.exists(os.path.join(SOUND_DIR, "eq.json"))
        state["soundPreset"] = str(settings.get("sound_preset", ""))
        migrate_presets()
        state["presets"] = preset_info()
        if settings.get("boot_source"):
            state["bootSource"] = settings["boot_source"]
        state["style"] = settings.get("style", "edge")
        state["styleTune"] = settings.get("style_tune") or {}


def set_carplay(on: bool) -> None:
    """Whether the CarPlay dongle may take the phone at all. The caller saves.

    carplay.py reads the same file: off, it opens no session, and without a host
    the dongle doesn't call the phone in, so it stays on plain Bluetooth.
    """
    settings["carplay"] = on
    with lock:
        state["carplay"] = on
        leave = not on and state.get("source") == "Carplay"
        if not on:
            state["carplayConn"] = False
    if leave:
        threading.Thread(target=activate, args=("Home",), daemon=True).start()


# A paired device is a phone if it offers calls or music to the car.
PHONE_UUIDS = ("0000111f-0000-1000-8000-00805f9b34fb",   # Handsfree Audio Gateway
               "0000110a-0000-1000-8000-00805f9b34fb")   # Audio Source


def paired_phones() -> list[str]:
    import subprocess
    def ctl(*a: str) -> str:
        try:
            return subprocess.run(["bluetoothctl", *a], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, timeout=10).stdout
        except Exception:  # noqa: BLE001 - no Bluetooth: no phones
            return ""
    found = []
    for line in ctl("devices", "Paired").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "Device":
            info = ctl("info", parts[1]).lower()
            if any(u in info for u in PHONE_UUIDS):
                found.append(parts[1])
    return found


def apply_phone_bt(on: bool) -> None:
    """Off blocks the paired phones in BlueZ, which also drops them, so they
    can't reconnect on their own; the battery monitor keeps the radio. On
    unblocks them and calls each one in, as an iPhone doesn't always come back
    by itself. Blocking is BlueZ's and survives a restart; done again at start
    for a phone paired since."""
    import subprocess
    for addr in paired_phones():
        cmd = ["bluetoothctl", "--timeout", "15"]
        steps = [["block", addr]] if not on else [["unblock", addr], ["connect", addr]]
        for step in steps:
            try:
                subprocess.run(cmd + step, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=25)
            except Exception as e:  # noqa: BLE001 - keep going with the next phone
                print(f"phone bluetooth {step[0]} {addr}: {e}", file=sys.stderr)
        print(f"phone bluetooth {'on' if on else 'off'}: {addr}", flush=True)


def set_phone_bt(on: bool) -> None:
    """Settings > Phone Bluetooth. The caller saves."""
    settings["phone_bt"] = on
    with lock:
        state["phoneBt"] = on
    threading.Thread(target=apply_phone_bt, args=(on,), daemon=True).start()


# ------------------------------------------------------- measurement mic ----
# A UMIK-1 for Settings > Sound calibration. Each one has its own response
# measured at the factory, published by miniDSP per serial number: the first
# line carries the sensitivity, the rest are frequency / dB pairs.
UMIK_URL = "https://www.minidsp.com/scripts/umikcal/umik.php/{}.txt"
UMIK_DIR = os.path.expanduser("~/.config/comand/umik")


UMIK90_URL = "https://www.minidsp.com/scripts/umikcal/umik90.php/{}_90deg.txt"


def umik_cal_path(serial: str, deg: int = 0) -> str:
    return os.path.join(UMIK_DIR, serial.replace("-", "") + ("_90deg" if deg == 90 else "") + ".txt")


def umik_cal_read(serial: str) -> dict | None:
    """The saved file's state, or None if there isn't one."""
    try:
        with open(umik_cal_path(serial)) as fh:
            head = fh.readline()
    except OSError:
        return None
    m = re.search(r"Sens Factor\s*=\s*(-?[\d.]+)", head)
    return {"status": "ok", "sens": float(m.group(1)) if m else 0.0,
            "deg90": os.path.exists(umik_cal_path(serial, 90))}


def fetch_umik_cal(serial: str) -> None:
    digits = serial.replace("-", "")
    result: dict
    try:
        # miniDSP answers Python's own user agent with 403.
        req = urllib.request.Request(UMIK_URL.format(digits),
                                     headers={"User-Agent": "curl/8.5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read(200_000).decode("utf-8", "replace")
        first = text.splitlines()[0] if text.strip() else ""
        if f"SERNO: {digits}" not in first:
            result = {"status": "missing"}
        else:
            os.makedirs(UMIK_DIR, exist_ok=True)
            with open(umik_cal_path(serial) + ".tmp", "w") as fh:
                fh.write(text)
            os.replace(umik_cal_path(serial) + ".tmp", umik_cal_path(serial))
            result = umik_cal_read(serial) or {"status": "error"}
            # The 90-degree file too: in a car the mic points at the roof, with
            # speakers all round it, and that is what this file is for.
            try:
                req90 = urllib.request.Request(UMIK90_URL.format(digits),
                                               headers={"User-Agent": "curl/8.5.0"})
                with urllib.request.urlopen(req90, timeout=20) as r:
                    t90 = r.read(200_000).decode("utf-8", "replace")
                if f"SERNO: {digits}" in (t90.splitlines()[0] if t90.strip() else ""):
                    with open(umik_cal_path(serial, 90) + ".tmp", "w") as fh:
                        fh.write(t90)
                    os.replace(umik_cal_path(serial, 90) + ".tmp", umik_cal_path(serial, 90))
                    result["deg90"] = True
            except Exception as e:  # noqa: BLE001 - the 0-degree file still works
                print(f"umik cal {serial} 90deg: {e}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - shown on the menu row, try again from there
        print(f"umik cal {serial}: {e}", file=sys.stderr)
        result = {"status": "error"}
    print(f"umik cal {serial}: {result}", flush=True)
    with lock:
        if state["micSerial"] == serial:
            state["micCal"] = result
    publish()


CAL_TOOL = "/usr/local/share/comand-ui/comand-cal.py"
cal_proc = None
# Sound presets: one calibration per listening position. The active one is
# copied to SOUND_DIR/eq.json, which is what comand-eq.py reads; each preset
# keeps its own copy under presets/<id>/. A preset with several seats is
# measured at each in turn and averaged (comand-cal.py --positions).
SOUND_DIR = os.path.expanduser("~/.config/comand/sound")
PRESET_DIR = os.path.join(SOUND_DIR, "presets")
PRESETS = {
    "driver":    ("Driver", ["driver"]),
    "passenger": ("Passenger", ["passenger"]),
    "front":     ("Front seats", ["driver", "passenger"]),
    "all":       ("All seats", ["driver", "passenger", "rear_left", "rear_right"]),
}


def preset_info() -> dict:
    """{id: "2026-11-03 13:26"} for each preset that has a calibration."""
    out = {}
    for pid in PRESETS:
        try:
            with open(os.path.join(PRESET_DIR, pid, "measurement.json")) as fh:
                out[pid] = str(json.load(fh).get("at", ""))[:16]
        except (OSError, ValueError):
            continue
    return out


def migrate_presets() -> None:
    """A calibration from before presets becomes the Driver preset."""
    active = os.path.join(SOUND_DIR, "eq.json")
    if not os.path.exists(active) or os.path.isdir(PRESET_DIR):
        return
    import shutil
    os.makedirs(os.path.join(PRESET_DIR, "driver"), exist_ok=True)
    for f in ("eq.json", "measurement.json"):
        if os.path.exists(os.path.join(SOUND_DIR, f)):
            shutil.copy2(os.path.join(SOUND_DIR, f), os.path.join(PRESET_DIR, "driver", f))
    settings["sound_preset"] = "driver"
    state["soundPreset"] = "driver"


def activate_preset(pid: str) -> bool:
    """Makes a calibrated preset the correction in use (live). The caller saves."""
    src = os.path.join(PRESET_DIR, pid)
    if pid not in PRESETS or not os.path.exists(os.path.join(src, "eq.json")):
        return False
    import shutil
    for f in ("eq.json", "measurement.json"):
        if os.path.exists(os.path.join(src, f)):
            shutil.copy2(os.path.join(src, f), os.path.join(SOUND_DIR, f + ".tmp"))
            os.replace(os.path.join(SOUND_DIR, f + ".tmp"), os.path.join(SOUND_DIR, f))
    settings["sound_preset"] = pid
    with lock:
        state["soundPreset"] = pid
        state["eqProfile"] = True
        on = state["eqOn"]
    set_eq(on)
    return True


def umik_source() -> str:
    _, out = run(["pactl", "list", "short", "sources"], timeout=5)
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) > 1 and "Umik" in parts[1] and ".monitor" not in parts[1]:
            return parts[1]
    return ""


def calibrate(pid: str = "driver") -> None:
    """Settings > Sound > Calibration. Sweeps through the output, recorded by the
    UMIK-1 at the preset's seats in turn; the result becomes that preset, is made
    the one in use and switched on."""
    global cal_proc
    import subprocess
    if pid not in PRESETS:
        pid = "driver"
    name, seats = PRESETS[pid]

    def status(**kw):
        kw.setdefault("preset", pid)
        with lock:
            state["cal"] = kw
        publish()

    with lock:
        serial, hw, in_call = state["micSerial"], state["outputActive"], bool(state["call"])
    if in_call:
        return status(step="error", msg="Not during a call")
    mic = umik_source()
    if not mic:
        return status(step="error", msg="UMIK-1 not connected")
    cal = umik_cal_path(serial, 90) if os.path.exists(umik_cal_path(serial, 90)) else umik_cal_path(serial)
    if not os.path.exists(cal):
        return status(step="error", msg="No correction file for the microphone")
    status(step="starting")
    # Everything else quiet: the sweep is the only sound the mic should hear.
    for f in (radio_stand_down, bt_stand_down, video_stand_down):
        try:
            f()
        except Exception as e:  # noqa: BLE001 - one player not stopping is not a reason to stop
            print(f"calibrate: {f.__name__}: {e}", file=sys.stderr)
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
        sk.sendto(b"pause", ("127.0.0.1", 7340))
    time.sleep(1.5)
    print(f"calibrate: {hw} with {mic}, mic file {os.path.basename(cal)}", flush=True)
    cal_proc = subprocess.Popen(["python3", CAL_TOOL, "--hw", hw, "--mic", mic, "--cal", cal,
                                 "--positions", ",".join(seats), "--name", name,
                                 "--out", os.path.join(PRESET_DIR, pid)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
    last = {"step": "error", "msg": "Stopped"}
    for line in cal_proc.stdout:
        line = line.strip()
        try:
            msg = json.loads(line)
        except ValueError:
            print(f"calibrate: {line}", flush=True)
            continue
        if msg.get("step") == "done":
            print(f"calibrate: done {json.dumps(msg.get('profile'))}", flush=True)
            # A short summary for the done screen: level, delay, filters per speaker.
            chans = (msg.get("profile") or {}).get("channels") or {}
            msg = {"step": "done", "at": time.strftime("%H:%M"),
                   "summary": {ch: {"gain_db": c.get("gain_db", 0), "delay_ms": c.get("delay_ms", 0),
                                    "bands": len(c.get("bands") or [])} for ch, c in chans.items()}}
        elif msg.get("step") != "measuring":
            print(f"calibrate: {line}", flush=True)
        last = msg
        status(**msg)
    cal_proc.wait()
    cal_proc = None
    if last.get("step") == "done":
        with lock:
            state["eqOn"] = True
            state["presets"] = preset_info()
        settings["eq_on"] = True
        activate_preset(pid)
        save_settings()
        publish()
    elif last.get("step") != "error":
        status(step="error", msg="Stopped")


def calibration_next() -> None:
    """The driver pressed "ready" with the mic at the next seat."""
    p = cal_proc
    if p and p.poll() is None and p.stdin:
        try:
            p.stdin.write("\n")
            p.stdin.flush()
        except OSError:
            pass


def stop_calibration() -> None:
    p = cal_proc
    if p and p.poll() is None:
        p.terminate()
        # The sweep player is its child's child: stop it too, it is loud.
        run(["pkill", "-f", "pw-play --target .*comand-cal-"], timeout=3)
        run(["pkill", "-f", "pw-record --target .*comand-cal-"], timeout=3)


def set_mic_serial(serial: str) -> None:
    """Saves the serial and fetches its file. The caller saves settings."""
    serial = re.sub(r"[^0-9-]", "", serial)[:9]
    settings["mic_serial"] = serial
    with lock:
        state["micSerial"] = serial
        state["micCal"] = {"status": "fetching"} if serial else None
    if serial:
        threading.Thread(target=fetch_umik_cal, args=(serial,), daemon=True).start()


def output_sinks() -> list[dict]:
    """Real outputs: ALSA sinks, built-in first. Not Bluetooth, not virtual."""
    code, out = run(["pactl", "-f", "json", "list", "sinks"], timeout=5)
    try:
        sinks = json.loads(out) if code == 0 else []
    except ValueError:
        return []
    found = [{"name": s["name"], "desc": s.get("description") or s["name"],
              "channels": len(str(s.get("channel_map", "")).split(",")) or 2}
             for s in sinks if str(s.get("name", "")).startswith("alsa_output.")]
    found.sort(key=lambda s: (".pci-" not in s["name"], s["name"]))
    return found


def apply_output(force: bool = False) -> None:
    """Make the chosen output the default sink, or the built-in one while the
    chosen one isn't there. Volume goes with it: it is set on the default sink."""
    sinks = output_sinks()
    names = [s["name"] for s in sinks]
    with lock:
        want = state["output"]
        was = state["outputActive"]
        state["outputs"] = sinks
    use = want if want in names else (names[0] if names else "")
    if not use:
        return
    with lock:
        state["outputActive"] = use
        eq_on, in_call = state["eqOn"], bool(state["call"])
    # The correction in front of the output. Rewritten (and restarted) only when
    # it changes, and not under a call: a restart is a second of silence.
    if not in_call:
        code, out = run(["python3", EQ_TOOL, "--write", "--target", use]
                        + ([] if eq_on else ["--off"]), timeout=30)
        if "unchanged" not in out:
            print(f"sound: {out.strip()}", flush=True)
            time.sleep(1.5)
    eq_up = eq_present()
    default = EQ_NODE if eq_up else use
    _, cur = run(["pactl", "get-default-sink"], timeout=5)
    if force or cur.strip() != default or use != was:
        run(["pactl", "set-default-sink", default], timeout=5)
        if eq_up:
            run(["pactl", "set-sink-volume", EQ_NODE, "100%"], timeout=5)
            run(["pactl", "set-sink-mute", EQ_NODE, "0"], timeout=5)
        with lock:
            vol, muted = state["vol"], state["muted"]
        set_output_volume(vol, muted)
        print(f"output: {use} via {default}" + ("" if use == want or not want else f" ({want} not connected)"),
              flush=True)
    with lock:
        state["eqUp"] = eq_up
    publish()


def eq_present() -> bool:
    _, out = run(["pactl", "list", "short", "sinks"], timeout=5)
    return any(ln.split("\t")[1:2] == [EQ_NODE] for ln in out.splitlines())


# Settings > Sound: the driver's own balance, fader, bass, treble and EQ, in
# settings["sound"], which comand-eq.py reads. Ranges as the menu shows them.
SOUND_KEYS = {"balance": 10, "fader": 10, "bass": 10, "treble": 10}
EQ_BANDS = 5
sound_dirty = threading.Event()


def set_sound(change: dict) -> None:
    """Merge a change in; the push to the running stage is coalesced, so a knob
    turned quickly is a handful of updates, not one per detent."""
    cur = dict(settings.get("sound") or {})
    for k, v in change.items():
        try:
            if k in SOUND_KEYS:
                lim = SOUND_KEYS[k]
                cur[k] = max(-lim, min(lim, int(v)))
            elif k == "eq" and isinstance(v, list):
                cur["eq"] = [max(-10, min(10, int(x))) for x in (v + [0] * EQ_BANDS)[:EQ_BANDS]]
            elif k == "reset":
                cur = {}
        except (TypeError, ValueError):
            continue
    settings["sound"] = cur
    with lock:
        state["sound"] = dict(cur)
    sound_dirty.set()


def sound_pusher() -> None:
    while True:
        sound_dirty.wait()
        time.sleep(0.15)
        sound_dirty.clear()
        save_settings()
        with lock:
            on, use = state["eqOn"], state["outputActive"]
        code, out = run(["python3", EQ_TOOL, "--live", "--target", use]
                        + ([] if on else ["--off"]), timeout=20)
        if code != 0:
            print(f"sound: {out.strip()}", flush=True)


def set_eq(on: bool) -> None:
    """Settings > Sound > Correction: live, no restart."""
    settings["eq_on"] = on
    with lock:
        state["eqOn"] = on
        use = state["outputActive"]
    def work():
        code, out = run(["python3", EQ_TOOL, "--live", "--target", use]
                        + ([] if on else ["--off"]), timeout=20)
        print(f"sound: {out.strip()}", flush=True)
    threading.Thread(target=work, daemon=True).start()


# Per-source sound profiles: each source (Radio, Carplay, Live TV, Music...)
# remembers its own volume, tone (balance/fader/bass/treble/EQ) and whether
# the room correction is on. When "profile follows source" is on, leaving a
# source stores what it was last set to, and arriving at one restores it. The
# single filter-chain is untouched: only the loaded values change, live. Off
# by default, so the volume and tone stay global unless the user asks for this.
def remember_profile(src: str) -> None:
    if not settings.get("profile_per_source") or not src:
        return
    profs = dict(settings.get("source_profiles") or {})
    with lock:
        profs[src] = {"vol": int(state["vol"]),
                      "sound": dict(settings.get("sound") or {}),
                      "eq_on": bool(state["eqOn"])}
    settings["source_profiles"] = profs
    save_settings()


def restore_profile(src: str) -> None:
    if not settings.get("profile_per_source") or not src:
        return
    prof = (settings.get("source_profiles") or {}).get(src)
    if not isinstance(prof, dict):
        return
    with lock:
        state["vol"] = max(0, min(100, int(prof.get("vol", state["vol"]))))
        state["muted"] = False
        vol = state["vol"]
    set_output_volume(vol, False)
    tone = prof.get("sound")
    if isinstance(tone, dict):
        set_sound({"reset": True})
        set_sound(tone)
    set_eq(bool(prof.get("eq_on", True)))


def output_watch() -> None:
    last = None
    while True:
        names = tuple(s["name"] for s in output_sinks()) + (eq_present(),)
        if names != last:
            last = names
            apply_output()
        time.sleep(4)


def set_output(name: str) -> None:
    settings["output"] = name
    with lock:
        state["output"] = name
    threading.Thread(target=apply_output, args=(True,), daemon=True).start()


def umik_watch() -> None:
    """Whether the UMIK-1 is plugged in, from ALSA's card list. Also loads the
    saved correction file at start, fetching it if it was never downloaded."""
    serial = settings.get("mic_serial", "")
    if serial:
        have = umik_cal_read(serial)
        if have:
            with lock:
                state["micCal"] = have
        else:
            set_mic_serial(serial)
    while True:
        found = None
        try:
            with open("/proc/asound/cards") as fh:
                for line in fh:
                    m = re.search(r"Umik-1\s+Gain:\s*(\d+)\s*dB", line)
                    if m:
                        found = f"UMIK-1, gain {m.group(1)} dB"
                        break
        except OSError:
            pass
        with lock:
            changed = state["umik"] != found
            state["umik"] = found
        if changed:
            publish()
        time.sleep(3)


def load_settings() -> None:
    global settings
    try:
        with open(SETTINGS_PATH) as fh:
            settings.update(json.load(fh))
    except Exception:
        pass                            # first run, or unreadable: keep defaults


def save_settings() -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
        with open(SETTINGS_PATH, "w") as fh:
            json.dump(settings, fh)
    except Exception as e:
        print(f"could not save settings: {e}", file=sys.stderr)


SOURCES = {
    # Plex, in mpv. car/video-link.py starts and stops it.
    "Video": {"ws": "5"},
    # Sygic. It was Google Maps, and only because Maps was the one app here
    # that published its next turn where anything else could read it -- by
    # scraping notifications, which is what android/nav-watch.py does. That is
    # no longer the reason to choose a map: turn-by-turn now comes from the
    # phone over CarPlay as structured data, whichever app is guiding, so the
    # map on the Android side can be chosen for being the better map.
    "Navigation": {"ws": "1", "pkg": "com.sygic.aura"},
    # The phone's own screen over AirPlay. Everything the phone can show, with
    # the phone's own account behind it -- which is the half a media app on
    # the car could never have. It is a window like CarPlay's, so it gets a
    # workspace and nothing else.
    #
    # "outside" means the sound comes from the phone rather than from us, so
    # choosing it has to silence whatever we were playing. Without it the
    # mirrored phone's audio arrived on top of the radio and the two played
    # over each other. Radio and Music are not marked: they are the same MPD,
    # and standing one down would stop the other.
    "Mirror":     {"ws": "4", "outside": True, "is_phone": True},
    "Carplay":    {"ws": "2", "outside": True},
    # Radio has no window of its own: it is sound, and what you look at while
    # it plays is the menu. So it stays on the home workspace and is a source
    # only in the sense that it is what the head unit is doing.
    "Radio":      {"ws": HOME_WS, "radio": True},
    # Telephone has no window either: the dialler and the phone book are drawn
    # by our own page. It is a source so that choosing it keeps the panel's
    # screen ours -- without that the overlay goes click-through when the
    # knob's menu times out, and the keypad stops taking presses mid-dial.
    "Telephone":  {"ws": HOME_WS, "phone": True},
    # Music is the library -- the iPod today, local files when there are any.
    # Like radio it has no window: it is sound, and the menu is what you look
    # at while it plays.
    "Music":      {"ws": HOME_WS, "library": True},
    # The car itself: what the aux battery and its charger are doing. Drawn by
    # our own page, so like Radio and Telephone it has no window and stays on
    # the home workspace.
    "Car":        {"ws": HOME_WS, "car": True},
}
ANDROID_APP_ID = "Waydroid"          # sway app_id for the Waydroid surface


# Where the knob currently is in KNOB_TREE. A list so that Back is a pop
# rather than a lookup of what the parent was.
knob_where: list[str] = ["root"]


def knob_label(item: str) -> str:
    """The row as the driver should read it, value and all."""
    with lock:
        if item == "Battery monitor":
            return "Battery monitor: " + ("On" if state["ble"] else "Off")
        if item == "CarPlay":
            return "CarPlay: " + ("On" if state["carplay"] else "Off")
        if item == "Phone Bluetooth":
            return "Phone Bluetooth: " + ("On" if state["phoneBt"] else "Off")
        if item == "Start with":
            return "Start with: " + str(state["bootSource"]).title()
        if item == "Drive":
            return "Drive: " + ("Left hand" if state["hand"] == "left"
                                else "Right hand")
    return item


def knob_items(level: str) -> list[str]:
    return [knob_label(i) for i in KNOB_TREE.get(level, MENU)]


def knob_plain(level: str, shown: str) -> str:
    """The bare item behind a row the knob sent back.

    The knob returns the label it drew, which for a value row is the item plus
    its current value -- so matching on the label alone would stop working the
    moment the value changed.
    """
    for item in KNOB_TREE.get(level, MENU):
        if shown == item or shown == knob_label(item):
            return item
    return shown


def knob_show_menu(reset: bool = False) -> None:
    """Send the level the knob is on, and open it. reset puts the selection
    back to the top, which is right when the level changes but wrong for an
    in-place redraw (a setting toggled under the cursor stays put)."""
    level = knob_where[-1]
    push_knob({"menu": knob_menu_rows(level)})
    if reset:
        push_knob({"sel": 0})
    push_knob({"m": "menu"})


def knob_menu_choice(shown: str) -> bool:
    """Handle a knob menu activation. True if the menu handled it itself.

    False means "this is a source, treat it as one", which is what every
    root-level entry was before there was a tree.
    """
    level = knob_where[-1]
    if level.startswith(DYN):
        return knob_dyn_choice(level, shown)
    item = knob_plain(level, shown)

    if item == BACK:
        if len(knob_where) > 1:
            knob_where.pop()
        knob_show_menu()
        return True

    if item in KNOB_TREE and item != "root":
        knob_where.append(item)
        knob_show_menu()
        return True

    # Leaves that change a setting stay where they are, so the row can be seen
    # to have changed. Leaving the menu to prove a toggle worked would be
    # asking the driver to go back in and check.
    if item == "Battery monitor":
        with lock:
            settings["ble"] = not state["ble"]
            state["ble"] = settings["ble"]
            if not state["ble"]:
                state["aux"] = None
        save_settings()
        publish()
        knob_show_menu()
        return True

    if item == "Phone Bluetooth":
        set_phone_bt(not state["phoneBt"])
        save_settings()
        publish()
        knob_show_menu()
        return True

    if item == "CarPlay":
        set_carplay(not state["carplay"])
        save_settings()
        publish()
        knob_show_menu()
        return True

    if item == "Drive":
        with lock:
            hand = "left" if state["hand"] == "right" else "right"
            settings["hand"] = hand
            state["hand"] = hand
        save_settings()
        publish()
        knob_show_menu()
        return True

    if item == "Start with":
        order = ["menu", "Carplay", "Navigation"]
        with lock:
            i = order.index(state["bootSource"]) if state["bootSource"] in order else 0
            nxt = order[(i + 1) % len(order)]
            settings["boot_source"] = nxt
            state["bootSource"] = nxt
        save_settings()
        publish()
        knob_show_menu()
        return True

    if item in ("Vehicle", "Aux battery", "System"):
        knob_where[:] = ["root"]
        with lock:
            state["carPage"] = {"Vehicle": "veh", "System": "sys"}.get(item, "aux")
        threading.Thread(target=activate, args=("Car",), daemon=True).start()
        return True

    # The video branch: these leaves open a dynamic, fetched list rather than a
    # fixed KNOB_TREE node, so the knob can browse channels, films and YouTube.
    if level == "Video" and item in VIDEO_LEAVES:
        knob_enter_dynamic(VIDEO_LEAVES[item], item)
        return True

    # A source. Back to the top, so the next press of the knob starts where
    # the driver expects rather than three levels down from last time.
    knob_where[:] = ["root"]
    return False


def _knob_san(s: str) -> str:
    """A label the firmware can hold: ASCII (its font has no better), at most
    KNOB_LABEL_MAX chars (menuItems is char[20]). Matching happens on this same
    form, since this is exactly what the knob draws and sends back."""
    return (s or "").encode("ascii", "replace").decode("ascii")[:KNOB_LABEL_MAX]


def knob_menu_rows(level: str) -> list[str]:
    """The rows the knob should draw for a level: a static KNOB_TREE list, or a
    page of a dynamic video level -- up to KNOB_PAGE content rows, then More if
    another page follows, then Back."""
    if not level.startswith(DYN):
        return knob_items(level)
    entry = knob_dyn.get(level) or {"rows": [], "page": 0}
    rows, page = entry["rows"], entry.get("page", 0)
    window = rows[page * KNOB_PAGE: page * KNOB_PAGE + KNOB_PAGE]
    shown = []
    for r in window:
        r["_shown"] = _knob_san(r["label"])
        shown.append(r["_shown"])
    if (page + 1) * KNOB_PAGE < len(rows):
        shown.append(MORE)
    shown.append(BACK)
    return shown


def knob_fetch_rows(spec: dict) -> list[dict]:
    """Build a dynamic level's rows from car/video-link.py. Each row carries
    either a play payload (handed straight to /play, as the panel's page does)
    or a descend spec for a deeper level."""
    kind = spec.get("kind")
    if kind == "tv":
        lu = guest_vid("/tv", timeout=30)
        if isinstance(lu, dict) and lu.get("error"):
            raise RuntimeError(lu["error"])
        rows = []
        for g in (lu or {}).get("groups", []):
            for c in g.get("channels", []):
                rows.append({"label": c.get("name", "?"), "kind": "tv",
                             "play": {"tv": c.get("id"), "name": c.get("name", "")}})
        return rows
    if kind == "list":
        qs = "/list?what=" + urllib.parse.quote(spec["what"])
        if spec.get("key"):
            qs += "&key=" + urllib.parse.quote(str(spec["key"]))
        items = guest_vid(qs, timeout=30)
        if isinstance(items, dict) and items.get("error"):
            raise RuntimeError(items["error"])
        rows = []
        for it in items or []:
            if it.get("type") in ("show", "season"):
                rows.append({"label": it.get("name", "?"),
                             "descend": {"kind": "list", "what": "kids", "key": it.get("key")}})
            else:
                rows.append({"label": it.get("name", "?"), "play": it, "kind": "movie"})
        return rows
    if kind == "yt_root":
        return [{"label": "Subscriptions", "descend": {"kind": "yt", "what": "subs"}},
                {"label": "Watch later",   "descend": {"kind": "yt", "what": "later"}},
                {"label": "Playlists",     "descend": {"kind": "yt", "what": "playlists"}}]
    if kind in ("yt", "yt_url"):
        qs = ("/youtube?what=" + urllib.parse.quote(spec["what"])) if kind == "yt" \
            else ("/youtube?url=" + urllib.parse.quote(spec["url"]))
        items = guest_vid(qs, timeout=45)
        if isinstance(items, dict) and items.get("error"):
            raise RuntimeError(items["error"])
        rows = []
        for it in items or []:
            if it.get("type") == "ytlist":
                rows.append({"label": it.get("name", "?"),
                             "descend": {"kind": "yt_url", "url": it.get("url")}})
            else:
                rows.append({"label": it.get("name", "?"), "play": it, "kind": "youtube"})
        return rows
    return []


def knob_enter_dynamic(spec: dict, title: str) -> None:
    """Open a dynamic video level: show Loading at once, fetch its rows off the
    serial thread, then redraw if the knob is still sitting on it."""
    level = DYN + str(len(knob_where)) + ":" + title
    with knob_dyn_lock:
        knob_dyn[level] = {"rows": [{"label": "Loading…", "noop": True}], "page": 0}
        knob_where.append(level)
    knob_show_menu(reset=True)

    def work() -> None:
        try:
            rows = knob_fetch_rows(spec) or [{"label": "Nothing here", "noop": True}]
        except Exception as e:  # noqa: BLE001
            print(f"knob video {spec}: {e}", file=sys.stderr)
            rows = [{"label": "Not reachable", "noop": True}]
        with knob_dyn_lock:
            here = knob_dyn.get(level) is not None and knob_where and knob_where[-1] == level
            if here:
                knob_dyn[level] = {"rows": rows, "page": 0}
        if here:
            knob_show_menu(reset=True)

    threading.Thread(target=work, daemon=True).start()


def knob_dyn_choice(level: str, shown: str) -> bool:
    """A press inside a dynamic video level: Back pops it, More pages on, a
    content row either descends or plays."""
    entry = knob_dyn.get(level)
    if entry is None:
        knob_where[:] = ["root"]
        knob_show_menu(reset=True)
        return True
    if shown == BACK:
        with knob_dyn_lock:
            if len(knob_where) > 1:
                knob_dyn.pop(knob_where.pop(), None)
        knob_show_menu(reset=True)
        return True
    if shown == MORE:
        if (entry.get("page", 0) + 1) * KNOB_PAGE < len(entry["rows"]):
            entry["page"] = entry.get("page", 0) + 1
        knob_show_menu(reset=True)
        return True
    row = next((r for r in entry["rows"] if r.get("_shown") == shown), None)
    if row is None or row.get("noop"):
        return True
    if "descend" in row:
        knob_enter_dynamic(row["descend"], row["label"])
        return True
    if "play" in row:
        payload = row["play"]
        title = payload.get("name") or payload.get("title") or row["label"]
        threading.Thread(target=lambda: guest_vid("/play", payload), daemon=True).start()
        # The knob holds one now-playing face, driven by state["nowvideo"]; the
        # panel sets it for YouTube but nothing did for a knob-started film, so
        # the knob kept the last radio logo. Set it here (with the kind, so the
        # face shows a film/TV/YouTube mark, not always YouTube) and redraw.
        with lock:
            state["nowvideo"] = {"title": title, "playing": True, "kind": row.get("kind")}
        with knob_dyn_lock:
            knob_where[:] = ["root"]
            knob_dyn.clear()
        threading.Thread(target=knob_resting, daemon=True).start()
        publish()
        return True
    return True


def run(cmd: list[str], timeout: float = 8.0) -> tuple[int, str]:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def sway_sock() -> str | None:
    """Find sway's IPC socket.

    Not inherited: this runs as a system service started before sway, so
    SWAYSOCK is not in the environment and has to be looked up each time --
    the socket name carries sway's pid and changes every restart.
    """
    if os.environ.get("SWAYSOCK"):
        return os.environ["SWAYSOCK"]
    import glob
    socks = sorted(glob.glob(f"/run/user/{os.getuid()}/sway-ipc.*.sock"),
                   key=os.path.getmtime, reverse=True)
    return socks[0] if socks else None


def sway(cmd: str) -> None:
    sock = sway_sock()
    if not sock:
        return
    import subprocess
    try:
        subprocess.run(["swaymsg", cmd], capture_output=True, timeout=5,
                       env=dict(os.environ, SWAYSOCK=sock))
    except Exception:
        pass


def sway_query(what: str) -> object | None:
    """Ask sway something, rather than only telling it things."""
    sock = sway_sock()
    if not sock:
        return None
    import subprocess
    try:
        r = subprocess.run(["swaymsg", "-t", what], capture_output=True,
                           text=True, timeout=5,
                           env=dict(os.environ, SWAYSOCK=sock))
        return json.loads(r.stdout)
    except Exception:
        return None


def window_workspace(app_id: str) -> str | None:
    """Which workspace a window is on, by app_id, or None if it is not there."""
    tree = sway_query("get_tree")
    if not isinstance(tree, dict):
        return None
    found: list[str] = []

    def walk(node: dict, ws: str | None) -> None:
        if node.get("type") == "workspace":
            ws = node.get("name")
        if node.get("app_id") == app_id and ws:
            found.append(ws)
        for key in ("nodes", "floating_nodes"):
            for child in node.get(key) or []:
                walk(child, ws)

    walk(tree, None)
    return found[0] if found else None


def collect_mirror_window(ws: str, tries: int = 25) -> None:
    """Make sure uxplay's window is on the mirror workspace.

    The config's `assign` rule normally does this. It did not, and for a
    reason worth writing down: sway reads its config once, at start. The rule
    was added to the file while sway was already running, so the compositor
    on screen had never seen it, and the window opened on whatever workspace
    happened to be focused. The file said one thing and the running
    compositor another, which is why `get_tree` looked like the rule should
    have caught it.

    The effect was a phone mirroring perfectly onto a workspace nobody was
    looking at: full frame rate, correct geometry, no errors, no picture --
    and it appeared to work whenever the driver already happened to be on
    workspace 4 when the phone connected.

    A reload fixed that instance. This stays as the belt to its braces,
    because the failure is silent and looks like broken video: cheap to check,
    expensive to diagnose. Repeatedly, because "Begin streaming" is the
    earliest we hear of a session and the window can take a moment more to
    exist.
    """
    for _ in range(tries):
        time.sleep(0.3)
        where = window_workspace("uxplay")
        if where is None:
            continue                     # not mapped yet
        if where == ws:
            return
        sway(f'[app_id="uxplay"] move container to workspace {ws}')
        sway(f"workspace {ws}")


def show_home() -> None:
    """Back to the menu: an empty workspace with the overlay's menu on it."""
    sway(f"workspace {HOME_WS}")
    with lock:
        state["source"] = "home"
        state["screen"] = "menu"
    publish()


def app_installed(pkg: str) -> bool:
    """False only when Android answers and the app isn't there."""
    import subprocess
    try:
        r = subprocess.run(["sudo", "-n", "waydroid", "shell", "--", "pm", "path", pkg],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001 - can't tell: let the launch try
        return True
    return r.returncode == 0 and "package:" in r.stdout or "password" in r.stderr


def activate(label: str) -> None:
    if label == "Navigation" and state["navApp"] == "carplay":
        activate("Carplay")
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(b"maps", ("127.0.0.1", 7340))
        return
    if label == "Navigation" and not app_installed(SOURCES[label]["pkg"]):
        # The launch would leave an empty screen with nothing to say why.
        print("menu: Navigation has no app installed", flush=True)
        with lock:
            state["toast"] = {"icon": "bluetooth", "text": "Sygic is not installed",
                              "ms": 3000, "at": time.time()}
        publish()
        return
    if label == "Mirror" and window_workspace("uxplay") is None:
        # Nothing is casting to uxplay, so the mirror workspace is a black
        # screen with nothing to say why. Tell the driver instead of switching
        # to it -- the same courtesy as Navigation with no app installed.
        print("menu: nothing is mirroring", flush=True)
        with lock:
            state["toast"] = {"icon": "bluetooth", "text": "Not connected",
                              "ms": 3000, "at": time.time()}
        publish()
        return
    if label == "Carplay":
        # Off in Settings, or no phone on it: CarPlay's workspace is a black screen, and RET
        # there talks to a phone that isn't listening. Say why instead of switching.
        with lock:
            why = ("CarPlay is off in Settings" if not state["carplay"]
                   else None if state["carplayConn"] else "No phone on CarPlay")
            if why:
                state["toast"] = {"icon": "bluetooth", "text": why, "ms": 3000, "at": time.time()}
        if why:
            print(f"menu: Carplay not shown: {why}", flush=True)
            publish()
            return
    if label == "Home":
        show_home()
        return

    src = SOURCES.get(label)
    if not src:
        print(f"menu: {label} has no destination yet")
        return
    print(f"menu: {label} -> workspace {src['ws']}"
          + (f", {src['pkg']}" if src.get("pkg") else ""))

    # Bring the source's workspace up first, so the switch is immediate and
    # any app launch happens on a screen the user is already looking at.
    sway(f"workspace {src['ws']}")

    # A source that brings its own sound gets the speakers to itself. Both
    # stand-downs fade rather than cut, and both are no-ops when the thing
    # they would stop is not playing, so this is safe to call unconditionally.
    if src.get("outside"):
        threading.Thread(target=radio_stand_down, daemon=True).start()
        # ...but not the phone, when the phone is the source. bt_stand_down
        # pauses it over AVRCP, and the thing it would pause while mirroring
        # is the video the driver just put on the panel.
        if not src.get("is_phone"):
            threading.Thread(target=bt_stand_down, daemon=True).start()

    # uxplay's window does not obey the config's assign rule -- see
    # collect_mirror_window -- so it is fetched onto this workspace by hand.
    if label == "Mirror":
        threading.Thread(target=collect_mirror_window, args=(src["ws"],),
                         daemon=True).start()
        with lock:
            media = state["btmedia"]
        threading.Thread(target=push_knob_mirror, args=(media,),
                         daemon=True).start()
    with lock:
        prev_source = state["source"]
        state["source"] = label
        state["screen"] = ("menu" if (src.get("radio") or src.get("library")
                                      or src.get("phone") or src.get("car"))
                           else "app")
    publish()

    # Per-source sound profile: store what the source we are leaving was set to,
    # then load the one we are arriving at. No-ops unless the setting is on.
    if prev_source != label:
        remember_profile(prev_source)
        restore_profile(label)

    if src.get("radio"):
        # Choosing "Radio" with nothing playing resumes the last station rather
        # than presenting silence and a menu; the list is one press away.
        with lock:
            last, playing = state["radio"], state["playing"]
        if not last:
            sts = load_stations()
            if sts:
                play_station(sts[0]["name"])
        else:
            # A station is remembered. It may or may not still be playing: a
            # call ducks it, and the phone's music pauses it outright -- so
            # choosing Radio has to start it again rather than assume it is
            # already running. Assuming was why the phone faded out and
            # nothing replaced it.
            threading.Thread(target=resume_station, args=(last,),
                             daemon=True).start()
        return

    # Navigation has no sound of its own, so unless something is playing the
    # knob has nothing to show for it -- and a blank screen while the car is
    # guiding says less than the mark does. knob_resting decides: it leaves a
    # playing station or track alone.
    if label == "Navigation":
        threading.Thread(target=knob_resting, daemon=True).start()

    if src.get("pkg"):
        bring_forward(src["pkg"])


def foreground_pkg() -> str | None:
    """Which package is in front, or None if we cannot tell.

    `waydroid shell` needs root and this runs as the session user, so the
    answer is usually None. Distinguishing "not it" from "cannot tell" matters:
    treating an unanswerable check as a negative made bring_forward relaunch
    six times and then report failure, while the launches themselves had been
    working.
    """
    code, out = run(["waydroid", "shell", "--", "dumpsys", "window"], timeout=10)
    if code != 0:
        return None
    for line in out.splitlines():
        if "mCurrentFocus" in line and "/" in line:
            for tok in line.split():
                if "/" in tok:
                    return tok.split("/")[0].lstrip("{")
    return ""


def bring_forward(pkg: str, tries: int = 6) -> None:
    """Launch until the app is actually in front.

    One launch is not enough. Android takes a while to be ready after the
    container starts, and a launch that arrives too early is accepted and does
    nothing -- leaving the launcher on screen, which is the one thing a head
    unit should never show. So check, and try again.

    A no-op once the app is in front, which matters: relaunching Sygic
    mid-route would discard the route.
    """
    for _ in range(tries):
        front = foreground_pkg()
        if front == pkg:
            return
        code, out = run(["waydroid", "app", "launch", pkg], timeout=20)
        if front is None:
            # Cannot verify, so trust one successful launch rather than
            # hammering the app -- relaunching Sygic mid-route would discard
            # the route.
            if code == 0:
                return
        time.sleep(2)
    print(f"gave up bringing {pkg} forward", file=sys.stderr)

# One state object, owned by the lock, mirrored to both ends.
state = {
    "vol": 30,
    "muted": False,
    "speedVolume": False,
    "speedVolumeStrength": "medium",
    "profilePerSource": False,
    "sel": 0,
    "menuOpen": False,
    "knob": False,          # is the knob connected
    "nav": False,           # is the nav display connected
    "bootSource": "menu",   # what the car starts on
    # Which source is on screen. Separate from menuOpen on purpose: the knob's
    # menu is a transient overlay that times out by design, while "home" is a
    # source you stay on until you choose another. Sharing one flag meant the
    # knob's 5-second timeout blanked the head unit's home screen a few seconds
    # after every boot.
    "source": "home",
    "radio": "",            # station name, or empty when not playing
    "song": "",             # what the stream says it is playing
    "playing": False,       # is sound actually coming out, or is it paused
    # Whether the current source has a window of its own. Radio does not -- it
    # is sound -- so the screen stays on the menu while it plays. Without this
    # the page hid the menu for a source that draws nothing, leaving a black
    # panel with music coming out of it.
    "screen": "menu",       # "menu" or "app"
    # The pairing code while one is being shown, as {"code", "entered", "who"}.
    # Set by phone/bt-agent.py, which is the only thing that knows a pairing is
    # under way, and cleared by it when the code expires.
    "pairing": None,
    "ooono": None,
    "warning": None,
    # A passing announcement -- the phone connecting, so far -- as
    # {"icon", "text", "at"}, or None. "at" is what the page reacts to: the
    # event stream carries the whole state on every change, so a page that
    # watched the text would replay the announcement every time the volume
    # moved. Cleared after TOAST_S so a page that loads later does not show
    # something that already happened.
    "toast": None,
    # What the auxiliary battery is doing, from car/aux-battery.py: the Daly
    # BMS's pack readings and, under "charger", the Orion-Tr Smart's state.
    # None until something reports, and cleared when the reports stop -- a
    # stale number about a battery reads as the present and is worse than an
    # honest gap.
    "aux": None,
    "vehicle": None,        # the car's own state, from its Home Assistant (car/vehicle.py)
    # Which of the Car screens to show: "aux" for the battery readouts, "sys"
    # for the one-line diagram. The knob picks it, and the panel follows --
    # otherwise choosing System on the knob would land on whichever page the
    # panel happened to be showing last.
    "carPage": "aux",
    # Standby: the panel dark, the knob dark, nothing playing. Not sleep --
    # this board offers the kernel no sleep states at all, /sys/power/state is
    # empty and systemctl suspend has nothing to call -- so what "off" can
    # mean here is everything that draws power being told to stop, with the
    # machine still running underneath to be woken by.
    "standby": False,
    # "left" or "right": which side of the car the driver sits on, and so
    # which side of the panel their controls belong on.
    "hand": "right",
    # The speed limit table: how often it updates, for where, and how that's going.
    "mapFreq": "monthly",
    "mapCountries": ["germany"],
    "mapUpdated": "",
    "mapState": "",
    # The layout of the warning screen in the COMAND styles: one of WARN_STYLES.
    "warnStyle": "a",
    # The sound car/voice.py makes when it starts listening.
    "voiceSound": "current",
    # A phone is connected to CarPlay.
    "carplayConn": False,
    # Settings > CarPlay: off, the dongle isn't allowed to take the phone.
    "carplay": True,
    # Settings > Phone Bluetooth: off, the phone can't use the car for calls or music.
    "phoneBt": True,
    # Settings > Sound calibration: the UMIK-1 if plugged in ("Umik-1 18dB"),
    # its serial, and the state of its miniDSP correction file.
    "umik": None,
    # Settings > Sound > Output: the outputs present, the one chosen (by sink
    # name), and the one actually in use, which differs while it's unplugged.
    "outputs": [],
    "output": "",
    "outputActive": "",
    # Settings > Sound > Correction, whether a calibration exists, and whether
    # the correction stage is running (the sound goes round it if not).
    "eqOn": True,
    "eqProfile": False,
    "eqUp": False,
    # Settings > Sound > Calibrate: {"step": "measuring"|"analysing"|"done"|"error", ...}
    "cal": None,
    # Sound presets (one calibration per listening position): the one in use,
    # and {id: "yyyy-mm-dd hh:mm"} for those calibrated.
    "soundPreset": "",
    "presets": {},
    # Settings > Sound: {"balance": -10..10, "fader", "bass", "treble", "eq": [5]}
    "sound": {},
    "micSerial": "",
    "micCal": None,
    # Where Navigation goes: Maps in CarPlay, or Sygic on Android.
    "navApp": "carplay",
    # What car/voice.py listens for: one of WAKE_WORDS.
    "wakeWord": "off",
    # How loud road warnings are, on the same scale as the volume; 0 is silent.
    "warnVol": 70,
    # The volume overlay in the COMAND styles: "band", "page" or "box".
    "volStyle": "band",
    # The page's look: "edge" (our own), "night" or "day" (COMAND's own
    # screens), and per-style colour overrides ({"night": {"--bg": "#..."}}),
    # which is how the theme is fitted to what COMAND's video input does to it.
    "style": "edge",
    "styleTune": {},
    # Whether car/aux-battery.py may scan and connect. On unless the driver
    # has turned it off: the battery page is the point of it, and a monitor
    # that has to be remembered is a monitor that is off when it matters. The
    # switch exists for the case it was built for -- the phone's audio on the
    # same radio -- not as a thing to arm.
    "ble": True,
    # The picture calibration card is up (set_card).
    "calib": False,
    # COMAND picture settings (see VIDEO_PATH), -100..100 each.
    "video": {k: 0 for k in ("brightness", "contrast", "gamma", "red", "green", "blue")},
    # What the phone is playing over Bluetooth, as {"title","artist","album",
    # "status","duration","art"}, or None. Reported by phone/bt-audio.py.
    "btmedia": None,
    # The YouTube video on the panel, as {"title","playing"}, or None. Reported
    # by car/video-link.py.
    "nowvideo": None,
    "phone": "",            # the connected phone's name, or empty
    "signal": -1,           # 0-100 as the phone reports it, -1 when unknown
    "phonebat": -1,         # percent, from phone/bt-audio.py, -1 when unknown
    "phonecharging": False, # from an automation on the phone: Bluetooth doesn't say
    "operator": "",         # network name, for the same reason
    "micmuted": False,      # the call's microphone, separate from the car's
    "call": None,           # {"who": ..., "status": ...} while one is in progress
    "ipod": False,          # is an iPod mounted and indexed
    "tracks": 0,            # how many tracks the library has
}
lock = threading.Lock()

# Every connected page gets a queue; the serial thread fans out into them.
subscribers: list[queue.Queue] = []
subs_lock = threading.Lock()

to_knob: queue.Queue = queue.Queue()


def publish() -> None:
    """Send the current state to every connected page."""
    with lock:
        snap = json.dumps(dict(state))
    with subs_lock:
        for q in list(subscribers):
            try:
                q.put_nowait(snap)
            except queue.Full:
                pass            # a page that cannot keep up gets the next one


def push_knob(obj: dict) -> None:
    to_knob.put(json.dumps(obj))


# ------------------------------------------------------------------ serial ----
# Two boards, one bus. Both are 0x303A and either may enumerate first, so they
# are not told apart by device order or by a serial number written down
# somewhere -- each announces a `role` in its `hello` and is addressed by that.
# A board that does not send one is treated as the knob, which is what the
# older firmware is.

devices: dict[str, "Device"] = {}      # role -> Device
dev_lock = threading.Lock()


class Device:
    def __init__(self, path: str):
        self.path = path
        self.role = None
        self.out: queue.Queue = queue.Queue()

    def send(self, obj: dict) -> None:
        self.out.put(json.dumps(obj))


def send_to(role: str, obj: dict) -> bool:
    """Send to whichever board holds that role, if one is connected."""
    with dev_lock:
        dev = devices.get(role)
    if not dev:
        return False
    dev.send(obj)
    return True


def push_knob(obj: dict) -> None:
    send_to("knob", obj)


def find_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [p.device for p in list_ports.comports() if p.vid == ESPRESSIF_VID]


def scan_thread() -> None:
    """Open every ESP32 that appears, and keep doing so.

    Boards are unplugged and replugged on a bench and in a car, and a bridge
    that only looks once is a bridge someone has to restart.
    """
    running: dict[str, threading.Thread] = {}
    while True:
        for path in find_ports():
            t = running.get(path)
            if t and t.is_alive():
                continue
            th = threading.Thread(target=port_thread, args=(path,), daemon=True)
            running[path] = th
            th.start()
        time.sleep(2)


def port_thread(path: str) -> None:
    try:
        import serial
    except ImportError:
        print("pyserial missing: apt install python3-serial", file=sys.stderr)
        return

    try:
        port = serial.Serial(path, BAUD, timeout=1)
    except Exception as e:
        print(f"{path}: {e}", file=sys.stderr)
        return

    dev = Device(path)
    print(f"opened {path}", flush=True)

    writer = threading.Thread(target=writer_thread, args=(port, dev), daemon=True)
    writer.start()

    # Ask, rather than wait. The hello a board sends from setup() is always
    # lost because the port is not open that early, and a board that has been
    # running for hours has no reason to send another.
    dev.send({"ping": True})

    try:
        while True:
            raw = port.readline()
            if not raw:
                continue
            try:
                ev = json.loads(raw.decode("utf-8", "replace").strip())
            except (ValueError, UnicodeDecodeError):
                continue
            handle_event(ev, dev)
    except Exception as e:
        print(f"{path} lost: {e}", file=sys.stderr)
    finally:
        with dev_lock:
            if dev.role and devices.get(dev.role) is dev:
                del devices[dev.role]
                state[dev.role] = False
        publish()
        try:
            port.close()
        except Exception:
            pass


def writer_thread(port, dev: Device) -> None:
    while True:
        line = dev.out.get()
        try:
            port.write((line + "\n").encode())
            port.flush()
        except Exception:
            return          # the reader will notice and the scanner reopens


def adopt(dev: Device, role: str) -> None:
    global knob_picture
    with dev_lock:
        devices[role] = dev
    dev.role = role
    state[role] = True
    print(f"{dev.path} is the {role}", flush=True)

    if role == "knob":
        with lock:
            dev.send({"vol": state["vol"], "muted": state["muted"]})
            station, playing = state["radio"], state["playing"]
        dev.send({"menu": knob_items(knob_where[-1])})
        # Firmware from before touch came up at boot needs asking. Harmless to
        # newer firmware, which has already done it and just says so again.
        dev.send({"tpinit": True})
        # A knob that was replugged or reset has no logo and no idea what is
        # playing, so send it again rather than waiting for the next station
        # change -- which may never come.
        with lock:
            video = state["nowvideo"]
        if video:
            knob_picture = ""
            threading.Thread(target=push_knob_video, args=(video,),
                             daemon=True).start()
        elif station:
            threading.Thread(target=push_knob_radio,
                             args=(station, playing, True), daemon=True).start()
    publish()


# What the Ooono app's command bytes mean, from the real box's lights: what the
# screen says, the shorter word for the knob's round face, the box's colour,
# and how long it stays up. While one is up the knob's press is the Ooono button.
OOONO = {
    # Held: the app sends these once and they last until it writes 02, with
    # two minutes as the limit in case that never comes.
    "10": {"kind": "camera", "text": "Camera ahead", "knob": "Speed camera",
           "rgb": [40, 110, 255], "ms": 120000, "hold": True},
    "11": {"kind": "danger", "text": "Danger ahead", "knob": "Danger",
           "rgb": [232, 60, 60], "ms": 120000, "hold": True},
    "12": {"kind": "reported", "text": "Report received", "knob": "Reported",
           "rgb": [58, 190, 120], "ms": 2500},
    "13": {"kind": "speed", "text": "Speed limit exceeded", "knob": "Too fast",
           "rgb": [240, 176, 64], "ms": 3000},   # a glance: 6 s felt too long on the road (2026-11-09)
}


# The car's position, for the speed limit: gpsd's latest fix, kept current by
# gps_watch so a warning never waits for the next one.
gps_fix: dict = {}
gps_test_until = 0.0      # a position from {"gps_test": ...} holds until then
_speed_db = None


def gps_watch() -> None:
    import socket
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 2947), 5) as s:
                s.sendall(b'?WATCH={"enable":true,"json":true}\n')
                for line in s.makefile():
                    if '"TPV"' in line:
                        d = json.loads(line)
                        if d.get("mode", 0) >= 2 and "lat" in d and time.time() > gps_test_until:
                            gps_fix.update(lat=d["lat"], lon=d["lon"], track=d.get("track"),
                                           at=time.time())
        except Exception as e:  # noqa: BLE001 - gpsd restarting, no receiver
            print(f"gps: {e}", flush=True)
        time.sleep(5)


def camera_ahead() -> tuple[float, int | None] | None:
    """(metres, limit) of the mapped camera ahead of the car, or None."""
    if not gps_fix or time.time() - gps_fix["at"] > 5:
        return None
    try:
        import sqlite3
        import cameras                         # car/cameras.py, installed beside this
        if not os.path.exists(cameras.DB):
            return None
        db = sqlite3.connect(cameras.DB)
        try:
            return cameras.ahead(db, gps_fix["lat"], gps_fix["lon"], gps_fix.get("track"))
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001 - no distance is no reason to lose the warning
        print(f"camera: {e}", flush=True)
        return None


CAMERA_PASSED_M = 40        # this close and then gone from "ahead" = the camera has been passed
CAMERA_HOLD_CAP_S = 900     # a tracked camera warning never outlasts this, whatever happens


def camera_tracking(w: dict | None) -> bool:
    """A camera warning whose distance the GPS is following: the app's "all clear" doesn't end it."""
    return bool(w and w.get("kind") == "camera" and w.get("dist") is not None and not w.get("preview"))


def follow_camera(warning: dict) -> None:
    """Count the distance down until the camera has been reached.

    While the camera is tracked the warning is stretched (ms) so it stays on the
    screen and the knob, and the app's 02 is ignored; it ends here once the car is
    past the camera. Without a position it falls back to words, and the app ends it.
    """
    closest = None
    while True:
        time.sleep(1)
        with lock:
            if state["warning"] is not warning:
                return
        fresh = bool(gps_fix) and time.time() - gps_fix.get("at", 0) <= 5
        found = camera_ahead() if fresh else None
        passed = closest is not None and fresh and (
            (not found and closest <= CAMERA_PASSED_M + 150) or (found and found[0] > closest + 150))
        age = time.time() - warning["at"]
        if passed or (closest is not None and age > CAMERA_HOLD_CAP_S):
            print(f"camera: reached (closest {int(closest)} m)", flush=True)
            with lock:
                if state["warning"] is not warning:
                    return
                state["warning"] = None
            publish()
            send_to("knob", {"alert": None})
            return
        if not found:
            # No fix, or the camera is not (yet) ahead: words only; the app's 02 can end it.
            if closest is not None and age * 1000 > warning["ms"]:
                with lock:              # lost the camera and its stretched time has run out
                    if state["warning"] is warning:
                        state["warning"] = None
                publish()
                send_to("knob", {"alert": None})
                return
            if warning.get("dist") is not None or warning.get("metres") is not None:
                with lock:
                    if state["warning"] is not warning:
                        return
                    warning["metres"] = warning["dist"] = None
                    warning["text"] = "Camera ahead"
                publish()
            continue
        closest = min(found[0], closest) if closest is not None else found[0]
        metres = int(found[0] // 50 * 50)
        limit = found[1] or speed_limit() or warning.get("limit")
        with lock:
            if state["warning"] is not warning:
                return
            warning["start"] = max(warning.get("start") or 0, int(found[0]))
            warning["dist"] = int(found[0])
            warning["metres"] = metres
            warning["limit"] = limit
            warning["text"] = f"Camera in {metres} m" if metres else "Camera now"
            # keep it up for as long as the camera is ahead
            warning["ms"] = max(warning["ms"], int((age + 30) * 1000))
        publish()


def speed_limit() -> int | None:
    """The limit on the road the car is on, from the offline OSM table, or None."""
    global _speed_db
    if not gps_fix or time.time() - gps_fix["at"] > 5:
        return None
    try:
        import sqlite3
        import speedlimits                      # car/speedlimits.py, installed beside this
        if _speed_db is None:
            if not os.path.exists(speedlimits.DB):
                return None
            _speed_db = sqlite3.connect(speedlimits.DB, check_same_thread=False)
        return speedlimits.lookup(_speed_db, gps_fix["lat"], gps_fix["lon"], gps_fix.get("track"))
    except Exception as e:  # noqa: BLE001 - a missing table is no reason to lose the warning
        print(f"speed limit: {e}", flush=True)
        return None


# ------------------------------------------------------------ warning sound --
WARN_DIR = "/var/lib/comand/warn-sounds"
# (frequency Hz, seconds), 0 Hz being silence. Told apart by ear, without looking.
WARN_TONES = {
    "camera":   [(880, .16), (0, .08), (880, .16), (0, .25), (880, .16), (0, .08), (880, .16)],
    "danger":   [(1320, .12), (990, .12)] * 4,
    "speed":    [(660, .30), (0, .12), (660, .30)],
    "reported": [(660, .09), (880, .09), (1100, .16)],
}
_warn_playing = threading.Lock()


def warn_file(kind: str) -> str:
    """The sound for a warning, made the first time it is needed."""
    import math
    import struct
    import wave
    path = os.path.join(WARN_DIR, kind + ".wav")
    if not os.path.exists(path):
        os.makedirs(WARN_DIR, exist_ok=True)
        rate = 48000
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            for freq, secs in WARN_TONES[kind]:
                n = int(rate * secs)
                edge = int(rate * .005)        # no clicks at the ends of a tone
                w.writeframes(b"".join(struct.pack("<h", int(
                    26000 * min(1, i / edge, (n - i) / edge) * math.sin(2 * math.pi * freq * i / rate)
                    if freq else 0)) for i in range(n)))
    return path


def warn_sound(kind: str) -> None:
    """Play a warning at its own volume, whatever the music's is, even when muted.

    The sink is the only way to go louder than the current volume, so it goes to
    the higher of the two for the moment of the sound, and the other streams are
    scaled down so the music doesn't jump with it.
    """
    import subprocess
    if kind not in WARN_TONES or not _warn_playing.acquire(blocking=False):
        return
    others: list[str] = []
    try:
        with lock:
            if state["call"]:
                return
            warn, master = state["warnVol"], 0 if state["muted"] else state["vol"]
        if warn <= 0:
            return
        path = warn_file(kind)
        top = max(warn, master, 1)
        # pactl can hang for 30 s on one stream, and a warning can't wait.
        others = duckable_streams()
        for idx in others:
            subprocess.Popen(["timeout", "2", "pactl", "set-sink-input-volume", idx,
                              f"{int(100 * master / top * WARN_DUCK)}%"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        run(["pactl", "set-sink-volume", hw_sink(), f"{top}%"])
        run(["pactl", "set-sink-mute", hw_sink(), "0"])
        run(["paplay", f"--volume={int(65536 * warn / top)}", path], timeout=6)
    except Exception as e:  # noqa: BLE001 - the warning is still on the screen
        print(f"warning sound: {e}", flush=True)
    finally:
        for idx in others:
            subprocess.Popen(["timeout", "2", "pactl", "set-sink-input-volume", idx, "100%"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with lock:
            vol, muted = state["vol"], state["muted"]
        set_output_volume(vol, muted)
        _warn_playing.release()


WARN_DUCK = 0.4


def ooono_warning(w: str, limit: int | None = None) -> None:
    """Put a warning from the Ooono app on the screen and on the knob."""
    o = dict(OOONO[w])
    if o["kind"] not in ("speed", "camera"):
        limit = None
    elif limit is None:
        limit = speed_limit()
    if limit and o["kind"] == "speed":
        o["text"] = "Over the limit"          # the number is on the sign beside it
    found = camera_ahead() if o["kind"] == "camera" else None
    if found:
        limit = found[1] or limit
        o["text"] = f"Camera in {int(found[0] // 50 * 50)} m"
    warning = {"kind": o["kind"], "text": o["text"], "ms": o["ms"], "at": time.time(),
               "colour": "#%02x%02x%02x" % tuple(o["rgb"]), "limit": limit}
    if found:
        warning["metres"] = int(found[0] // 50 * 50)
        warning["dist"] = warning["start"] = int(found[0])
    with lock:
        state["warning"] = warning
        state["ooono"] = {**(state["ooono"] or {}), "last": w, "at": warning["at"]}
    publish()
    if o["kind"] == "camera":
        threading.Thread(target=follow_camera, args=(warning,), daemon=True).start()
    threading.Thread(target=warn_sound, args=(o["kind"],), daemon=True).start()
    if o.get("hold"):
        threading.Thread(target=hold_on_knob, args=(warning, o, limit), daemon=True).start()
    else:
        send_to("knob", {"alert": {"text": o["knob"], "rgb": o["rgb"], "ms": o["ms"],
                                   "limit": limit or 0}})
        # Over when its time is up, whether or not the app ever writes 02 (it often doesn't until it
        # reconnects), so every screen agrees it has gone.
        threading.Timer(o["ms"] / 1000, warning_expired, args=(warning,)).start()


def warning_expired(warning: dict) -> None:
    with lock:
        if state["warning"] is not warning:
            return
        state["warning"] = None
    publish()


def hold_on_knob(warning: dict, o: dict, limit: int | None) -> None:
    """Keep a held warning on the knob, whose own alerts last at most 60 s."""
    while True:
        left = warning["at"] + warning["ms"] / 1000 - time.time()
        with lock:
            current = state["warning"] is warning
        if not current or left <= 0:
            return
        send_to("knob", {"alert": {"text": o["knob"], "rgb": o["rgb"],
                                   "ms": int(min(left, 60) * 1000), "limit": limit or 0}})
        time.sleep(min(left, 50))


def ooono_clear() -> None:
    """The app says the warning is over (it writes 02 when it is back to normal)."""
    with lock:
        w = state["warning"]
        if not w:
            return
        if camera_tracking(w):
            print("ooono: app says over, camera still ahead -- keeping it", flush=True)
            return
        state["warning"] = None
    print("ooono: warning over", flush=True)
    publish()
    send_to("knob", {"alert": None})


def handle_event(ev: dict, dev: Device) -> None:
    e = ev.get("e")

    if e == "logoack":
        logo_ack.set()
        return

    if e == "ooono":
        # The knob poses as an Ooono co-driver (esp32-knob/knob-ui/ooono.h):
        # the Ooono app on the phone connects to it and writes one command
        # byte per warning. 04 xx before each one is a setting, not a warning.
        if "conn" in ev:
            with lock:
                state["ooono"] = {"conn": bool(ev["conn"])}
            print(f"ooono: app {'connected' if ev['conn'] else 'gone'}", flush=True)
            publish()
        w = str(ev.get("w") or "")
        if w:
            print(f"ooono: app wrote {w}", flush=True)
        if w in OOONO:
            ooono_warning(w)
        if w == "02":
            ooono_clear()
        if "btn" in ev:
            print(f"ooono: knob pressed as the box's button ({ev['btn']} ms)", flush=True)
        return

    if e == "radio" and ev.get("step") and source_kind() != "radio":
        step = int(ev["step"]) if str(ev["step"]).lstrip("-").isdigit() else 1
        threading.Thread(target=source_step, args=(step,), daemon=True).start()
        return

    if e == "radio" and ev.get("toggle") and source_kind() != "radio":
        threading.Thread(target=source_toggle, daemon=True).start()
        return

    if e == "radio" and ev.get("step"):
        # A swipe on the knob. Wraps, like the panel's grid: a list of three
        # has no meaningful end to stop at, and stopping at one would strand
        # the last station behind two swipes back.
        try:
            step = int(ev["step"])
        except (TypeError, ValueError):
            return
        sts = load_stations()
        if not sts:
            return
        with lock:
            cur = state["radio"]
        i = next((n for n, x in enumerate(sts) if x["name"] == cur), 0)
        nxt = sts[(i + step) % len(sts)]["name"]
        threading.Thread(target=play_station, args=(nxt,), daemon=True).start()
        return

    if e == "radio" and ev.get("toggle"):
        # The knob's transport button. It has already moved its own glyph, so
        # this only has to make the audio agree -- and toggle_radio publishes,
        # which corrects the knob if the two had drifted apart.
        threading.Thread(target=toggle_radio, daemon=True).start()
        return

    if e == "hello":
        # Older knob firmware sends no role; it can only be the knob.
        adopt(dev, str(ev.get("role") or "knob"))
        return

    # Only the knob produces input. A nav display sending these would mean
    # something is wrong, and acting on it would move the menu from a screen
    # that has no control on it.
    if dev.role not in (None, "knob"):
        return

    if e == "call":
        # The knob's call button: dial what is typed on the panel, or end the
        # call. The knob only offers the one that makes sense, but it is asked
        # again here -- a stale face is drawn on a screen, not on the phone.
        a = ev.get("a")
        with lock:
            in_call = bool(state["call"])
        if a == "dial" and dial_typed and not in_call:
            threading.Thread(target=dial, args=(dial_typed,), daemon=True).start()
        elif a == "hangup" and in_call:
            threading.Thread(target=phone_command, args=("hangup",),
                             daemon=True).start()
        elif a == "hangup":
            # Nothing to hang up: the knob is showing a call that is not
            # there. Put it back to what it should be showing.
            threading.Thread(target=knob_resting, daemon=True).start()
        return

    if e == "rot":
        d = int(ev.get("d", 0))
        with lock:
            if state["menuOpen"]:
                state["sel"] = max(0, min(len(MENU) - 1, state["sel"] + d))
            else:
                if state["muted"]:
                    state["muted"] = False
                state["vol"] = max(0, min(100, state["vol"] + d))
            vol, mute = state["vol"], state["muted"]
        set_output_volume(vol, mute)
        publish()
        return

    if e == "press":
        t = ev.get("t")
        # Five seconds on the knob, which is the only gesture standby answers
        # to and the only one it leaves working. Toggling rather than two
        # gestures: the car is either dark or it is not, and the driver can
        # see which.
        with lock:
            asleep = state["standby"]
        # Waking is a plain press, because reaching for a dark car's knob
        # means "come back" and nothing else. Going to sleep is the five
        # second hold, because it takes the whole car's screen away and should
        # not be reachable by leaning on the knob.
        if t == "wake":
            if asleep:
                threading.Thread(target=set_standby, args=(False,),
                                 daemon=True).start()
            return
        if t == "hold":
            threading.Thread(target=set_standby, args=(not asleep,),
                             daemon=True).start()
            return
        if asleep:
            return              # asleep, the knob does nothing else
        with lock:
            if t == "mute":
                state["muted"] = True
            elif t == "unmute":
                state["muted"] = False
            vol, mute = state["vol"], state["muted"]
        set_output_volume(vol, mute)
        publish()
        return

    if e == "menu":
        a = ev.get("a")
        label = ev.get("label")
        with lock:
            if "sel" in ev:
                state["sel"] = int(ev["sel"])
            if a == "open":
                state["menuOpen"] = True
            elif a in ("activate", "cancel", "timeout"):
                state["menuOpen"] = False
        publish()
        # After publishing, so the overlay has already dropped the menu before
        # the app comes forward.
        if a == "activate" and label:
            if not booted.is_set():
                print(f"ignoring {label} during startup", file=sys.stderr)
                return
            # The menu gets first refusal: it may be a submenu to walk into or
            # a setting to change, in which case nothing is activated at all.
            if knob_menu_choice(str(label)):
                return
            threading.Thread(target=activate, args=(label,), daemon=True).start()
        elif a in ("cancel", "timeout"):
            # Leaving the menu leaves it at the top. A menu that reopens three
            # levels down, where it was abandoned a drive ago, is a menu that
            # has to be read before it can be used.
            knob_where[:] = ["root"]
        return


# ------------------------------------------------------------ guest remote ----
# Settings > App control (or "app control" / "show remote" to voice.py) puts a QR code on COMAND's screen. It opens
# ui/guest.html on a passenger's phone on the car Wi-Fi: now playing, volume, mute, play/pause, previous, next --
# nothing else. Its calls carry a code that does not expire; Settings > App control > New code replaces it, which
# shuts out every phone that scanned the old one.
GUEST_TOKEN_PATH = os.path.expanduser("~/.config/comand/guest-token")
GUEST_PAGE = os.path.join(HERE, "guest.html")
QR_JS = os.path.join(HERE, "qrcode.js")
GUEST_KEYS = ("play_pause", "next", "previous", "volume_up", "volume_down", "mute")


def guest_token(renew: bool = False) -> str:
    import secrets
    if not renew:
        try:
            with open(GUEST_TOKEN_PATH) as fh:
                t = fh.read().strip()
            if len(t) >= 10:
                return t
        except OSError:
            pass
    t = secrets.token_urlsafe(9)            # 12 characters: short, so the QR stays coarse enough for COMAND's panel
    os.makedirs(os.path.dirname(GUEST_TOKEN_PATH), exist_ok=True)
    with open(GUEST_TOKEN_PATH + ".new", "w") as fh:
        fh.write(t)
    os.chmod(GUEST_TOKEN_PATH + ".new", 0o600)
    os.replace(GUEST_TOKEN_PATH + ".new", GUEST_TOKEN_PATH)
    return t


def guest_ok(t: str) -> bool:
    import hmac
    return bool(t) and hmac.compare_digest(t.encode(), guest_token().encode())


def guest_url(renew: bool = False) -> str:
    """The page's address as a phone in the car sees it: this box's address on its default route."""
    import socket
    ip = "127.0.0.1"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))           # no packet is sent; it only picks the outgoing interface
        ip = s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()
    return f"http://{ip}:8080/guest?t={guest_token(renew)}"


GUEST_VID = "http://127.0.0.1:8092"        # car/video-link.py: Plex + YouTube browser and the mpv player
GUEST_VIDEO_ACTIONS = ("toggle", "forward", "back30", "stop")
GUEST_YT_FEEDS = ("subs", "later", "playlists")
GUEST_MA = "http://127.0.0.1:8091"         # car/ma-link.py: Music Assistant library, search, covers, playback
GUEST_MA_KINDS = ("albums", "artists", "playlists", "radios")
GUEST_MA_ORDERS = ("sort_name", "last_played_desc", "timestamp_added_desc")


def guest_fetch(url: str, body: dict | None = None, timeout: float = 70):
    """JSON from a local helper (video-link, ma-link); errors come back as {"error": ...}."""
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") or {"error": f"error {e.code}"}
        except ValueError:
            return {"error": f"error {e.code}"}
    except (OSError, ValueError) as e:
        return {"error": f"not reachable ({type(e).__name__})"}


# Covers the guest page has asked for, kept on this box: each crosses the car's internet once, and phones get
# them instantly afterwards (also while the car is offline). Least recently used go first past COVER_CACHE_MAX.
COVER_CACHE = os.path.expanduser("~/.cache/comand/covers")
COVER_CACHE_MAX = 200 * 1024 * 1024
_cover_writes = [0]


def cover_cached(key: str, fetch) -> bytes | None:
    import hashlib
    path = os.path.join(COVER_CACHE, hashlib.sha1(key.encode()).hexdigest())
    try:
        with open(path, "rb") as fh:
            pic = fh.read()
        os.utime(path)                       # recently used: kept longest
        return pic
    except OSError:
        pass
    pic = fetch()
    # Only real pictures: an error page or an empty answer would be served from here for good.
    if not pic or len(pic) < 200 or not (pic[:3] == b"\xff\xd8\xff" or pic[:8] == b"\x89PNG\r\n\x1a\n"
                                         or pic[:4] == b"RIFF" or pic[:5] in (b"<?xml", b"<svg ")):
        return pic
    try:
        os.makedirs(COVER_CACHE, exist_ok=True)
        with open(path + ".tmp", "wb") as fh:
            fh.write(pic)
        os.replace(path + ".tmp", path)
        _cover_writes[0] += 1
        if _cover_writes[0] % 50 == 1:
            threading.Thread(target=cover_prune, daemon=True).start()
    except OSError as e:
        print(f"cover cache: {e}", file=sys.stderr)
    return pic


def cover_prune() -> None:
    try:
        files = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(COVER_CACHE) if e.is_file()]
    except OSError:
        return
    total = sum(f[1] for f in files)
    for _, size, path in sorted(files):     # oldest use first
        if total <= COVER_CACHE_MAX:
            break
        try:
            os.remove(path); total -= size
        except OSError:
            pass


def guest_image(q: dict) -> bytes | None:
    """A cover for the guest page, fetched here: Plex posters (the token stays on this box), YouTube thumbnails,
    Music Assistant art and IPTV channel logos."""
    src = q.get("src")
    if src == "plex" and re.fullmatch(r"/library/[\w/:.-]+", q.get("path", "")):
        url = GUEST_VID + "/image?" + urllib.parse.urlencode({"path": q["path"], "w": 240})
    elif src == "yt" and re.fullmatch(r"[A-Za-z0-9_-]{11}", q.get("id", "")):
        url = GUEST_VID + "/ytimg?id=" + q["id"]
    elif src == "ma" and q.get("id") and len(q["id"]) < 2000:
        url = GUEST_MA + "/image?" + urllib.parse.urlencode({"id": q["id"]})
    elif src == "tv" and re.fullmatch(r"[\w-]{1,64}", q.get("id", "")):
        url = GUEST_VID + "/tvlogo?id=" + q["id"]
    else:
        return None

    def fetch():
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return r.read()
        except OSError:
            return None
    return cover_cached(url, fetch)


def guest_vid(path: str, body: dict | None = None, timeout: float = 70):
    """One call to video-link for the guest page; its errors come back as {"error": ...}."""
    req = urllib.request.Request(GUEST_VID + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read() or b"{}") or {"error": f"video {e.code}"}
        except ValueError:
            return {"error": f"video {e.code}"}
    except (OSError, ValueError) as e:
        return {"error": f"video player not reachable ({type(e).__name__})"}


def guest_state() -> dict:
    with lock:
        out = {"song": state.get("song") or "", "radio": state.get("radio") or "", "source": state.get("source") or "",
               "playing": bool(state.get("playing")), "vol": state.get("vol"), "muted": bool(state.get("muted")),
               "carplay": bool(state.get("carplayConn")), "bt": bool(state.get("btmedia"))}
    now = guest_vid("/now", timeout=1.5)
    if isinstance(now, dict) and now.get("playing"):
        out["video"] = {"name": now["playing"], "paused": bool(now.get("paused")), "moving": bool(now.get("moving")),
                        "pos": now.get("pos"), "tv": bool(now.get("tv"))}
    return out


def guest_get(path: str, q: dict):
    """The lists the guest page browses: stations, Plex (continue/recent/movie/show, or a show's or season's
    children by key) and YouTube (the signed-in feeds, or a playlist link from one of them)."""
    if path == "/guest/radio":
        with lock:
            now = state.get("radio") or ""
        return {"stations": [st["name"] for st in load_stations()], "now": now}
    if path == "/guest/video":
        what, key = q.get("what", ""), q.get("key", "")
        if what not in ("continue", "recent", "movie", "show", "") or (not what and not key.isdigit()):
            return None
        return guest_vid("/list?" + urllib.parse.urlencode({"what": what, "key": key, "offset": int(q.get("offset", 0) or 0)}))
    if path == "/guest/youtube":
        what, url = q.get("what", ""), q.get("url", "")
        if what not in GUEST_YT_FEEDS and not url.startswith("https://www.youtube.com/"):
            return None
        return guest_vid("/youtube?" + urllib.parse.urlencode({"what": what, "url": url}))
    if path == "/guest/search":
        scope, text = q.get("scope", ""), " ".join(q.get("q", "").split())[:100]
        if not text:
            return []
        if scope == "video":
            return guest_vid("/search?" + urllib.parse.urlencode({"q": text}))
        if scope == "youtube":
            return guest_vid("/youtube?" + urllib.parse.urlencode({"what": "search", "q": text}))
        if scope == "music":
            return guest_fetch(GUEST_MA + "/search?" + urllib.parse.urlencode({"q": text}))
        return None
    if path == "/guest/music":
        if q.get("kids") in ("album", "artist", "playlist"):
            return guest_fetch(GUEST_MA + "/kids?" + urllib.parse.urlencode(
                {"kind": q["kids"], "id": q.get("id", "")[:200], "provider": q.get("provider", "library")[:100]}))
        if q.get("kind") in GUEST_MA_KINDS:
            order = q.get("order") if q.get("order") in GUEST_MA_ORDERS else "sort_name"
            return guest_fetch(GUEST_MA + "/list?" + urllib.parse.urlencode({"kind": q["kind"], "order": order}))
        return None
    if path == "/guest/tv":
        return guest_vid("/tv", timeout=20)
    if path == "/guest/epg":
        return guest_vid("/epg", timeout=20)
    if path == "/guest/favourites":
        return {"favourites": load_favourites()}
    return None


def guest_command(cmd: dict) -> bool:
    k = cmd.get("k")
    if "radio" in cmd:                       # a station by name, or null to stop
        name = cmd["radio"]
        if name is None:
            threading.Thread(target=stop_radio, daemon=True).start(); return True
        if name in [st["name"] for st in load_stations()]:
            threading.Thread(target=play_station, args=(name,), daemon=True).start(); return True
        return False
    if isinstance(cmd.get("play"), dict):     # a Plex item (key) or a YouTube video (url) from the lists
        it = cmd["play"]
        if it.get("url"):
            if not str(it["url"]).startswith("https://www.youtube.com/"):
                return False
            body = {"url": str(it["url"]), "name": str(it.get("name") or "")[:200]}
        elif str(it.get("key") or "").isdigit():
            body = {"key": str(it["key"]), "offset": int(it.get("offset") or 0), "duration": int(it.get("duration") or 0),
                    "name": str(it.get("name") or "")[:200]}
        else:
            return False
        threading.Thread(target=guest_vid, args=("/play", body), daemon=True).start()
        return True
    if isinstance(cmd.get("music"), str) and cmd["music"] and len(cmd["music"]) < 500:   # a Music Assistant uri
        threading.Thread(target=guest_fetch, args=(GUEST_MA + "/play", {"uri": cmd["music"]}), daemon=True).start()
        return True
    if isinstance(cmd.get("tv"), dict) and re.fullmatch(r"[\w-]{1,64}", str(cmd["tv"].get("id") or "")):
        threading.Thread(target=guest_vid, args=("/play", {"tv": cmd["tv"]["id"], "name": str(cmd["tv"].get("name") or "")[:120]}),
                         daemon=True).start()
        return True
    if isinstance(cmd.get("favourite"), dict):     # add / remove / play one favourite
        handle_favourite(cmd["favourite"])
        return True
    if cmd.get("video") in GUEST_VIDEO_ACTIONS:
        guest_vid("/ctl", {"action": cmd["video"]}, timeout=5)
        return True
    if k in GUEST_KEYS:
        threading.Thread(target=head_control, args=(k,), daemon=True).start()
        return True
    if "vol" in cmd:
        try:
            v = max(0, min(100, int(cmd["vol"])))
        except (TypeError, ValueError):
            return False
        with lock:
            state["vol"], state["muted"] = v, False
        set_output_volume(v, False)
        publish()
        return True
    return False


# -------------------------------------------------------------------- http ----

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):      # one line per knob turn is not useful
        pass

    def _send(self, code, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        # An exception in the page, which it reports here: in the overlay its
        # console goes nowhere, and a state update that throws looks only like
        # a screen that stopped drawing.
        if path == "/page-error":
            print("page error: " + urllib.parse.unquote(self.path.partition("?")[2])[:400], flush=True)
            self._send(204, b"", "text/plain")
            return

        if path == "/guest":
            try:
                with open(GUEST_PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, b"page missing", "text/plain")
            return
        if path.startswith("/guest/"):
            q = {k: v[0] for k, v in urllib.parse.parse_qs(self.path.partition("?")[2]).items()}
            if not guest_ok(q.get("t", "")):
                self._send(403, b"{}", "application/json"); return
            if path == "/guest/img":
                pic = guest_image(q)
                if pic is None:
                    self._send(404, b"", "text/plain"); return
                self.send_response(200)
                self.send_header("Content-Type", "image/png" if pic[:4] == b"\x89PNG" else "image/webp" if pic[:4] == b"RIFF"
                                 else "image/svg+xml" if pic[:5] in (b"<?xml", b"<svg ") else "image/jpeg")
                self.send_header("Content-Length", str(len(pic)))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                self.wfile.write(pic)
                return
            out = guest_state() if path == "/guest/state" else guest_get(path, q)
            if out is None:
                self._send(404, b"{}", "application/json"); return
            self._send(502 if isinstance(out, dict) and out.get("error") else 200, json.dumps(out).encode(), "application/json")
            return
        if path == "/qrcode.js":
            try:
                with open(QR_JS, "rb") as f:
                    self._send(200, f.read(), "text/javascript; charset=utf-8")
            except OSError:
                self._send(404, b"missing", "text/plain")
            return

        if path in ("/", "/main-menu.html"):
            try:
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, b"page missing", "text/plain")
            return

        # What a reading has been, for the detail popups and for anything
        # that wants to chart it later.
        #   /history?name=soc&hours=24         the series
        #   /history?name=soc&hours=24&summary=1   low, high, latest
        #   /history                           what is being recorded
        if path == "/history":
            if not history:
                self._send(503, b'{"error":"no history log"}', "application/json")
                return
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            name = (q.get("name") or [""])[0]
            try:
                hours = float((q.get("hours") or ["24"])[0])
            except ValueError:
                hours = 24.0
            since = time.time() - hours * 3600
            try:
                if not name:
                    body = json.dumps({"names": history.names()})
                elif (q.get("summary") or [""])[0]:
                    body = json.dumps(history.summary(name, since))
                else:
                    body = json.dumps({"name": name,
                                       "points": history.series(name, since)})
            except Exception as e:
                body = json.dumps({"error": str(e)})
            self._send(200, body.encode(), "application/json")
            return

        if path == "/artists":
            self._send(200, json.dumps(mpd_list("artist")).encode(),
                       "application/json")
            return

        if path.startswith("/albums"):
            artist = urllib.parse.unquote(self.path.split("=", 1)[1]) \
                if "=" in self.path else ""
            self._send(200, json.dumps(mpd_list("album", "artist", artist)).encode(),
                       "application/json")
            return

        if path.startswith("/cover/"):
            name = os.path.basename(urllib.parse.unquote(path[len("/cover/"):]))
            full = os.path.join(COVER_DIR, name)
            if not os.path.isfile(full):
                self._send(404, b"no cover", "text/plain")
                return
            ctype = {".jpg": "image/jpeg", ".png": "image/png",
                     ".webp": "image/webp"}.get(
                         os.path.splitext(name)[1].lower(), "image/jpeg")
            with open(full, "rb") as fh:
                self._send(200, fh.read(), ctype)
            return

        if path.startswith("/logo/"):
            # Served from here rather than as file:// URLs because the page is
            # served over http and a file:// image on an http page is blocked.
            name = os.path.basename(urllib.parse.unquote(path[len("/logo/"):]))
            full = os.path.join(LOGO_DIR, name)
            if not os.path.isfile(full):
                self._send(404, b"no logo", "text/plain")
                return
            ctype = {".png": "image/png", ".jpg": "image/jpeg",
                     ".ico": "image/x-icon", ".svg": "image/svg+xml",
                     ".webp": "image/webp"}.get(
                         os.path.splitext(name)[1].lower(), "application/octet-stream")
            with open(full, "rb") as fh:
                self._send(200, fh.read(), ctype)
            return

        if path == "/recents":
            self._send(200, json.dumps(load_recents()).encode(),
                       "application/json")
            return

        if path == "/contacts":
            self._send(200, json.dumps(all_contacts()).encode(),
                       "application/json")
            return

        if path == "/stations":
            self._send(200, json.dumps(load_stations()).encode(),
                       "application/json")
            return

        if path == "/favourites":
            self._send(200, json.dumps({"favourites": load_favourites()}).encode(),
                       "application/json")
            return

        # Settings > Sound > Graph: the last calibration's curves (before,
        # after, target) and the filters it chose.
        if path == "/measurement":
            try:
                with open(os.path.expanduser("~/.config/comand/sound/measurement.json")) as fh:
                    m = json.load(fh)
                try:
                    with open(os.path.expanduser("~/.config/comand/sound/eq.json")) as fh:
                        m["profile"] = json.load(fh)
                except (OSError, ValueError):
                    pass
                self._send(200, json.dumps(m).encode(), "application/json")
            except (OSError, ValueError):
                self._send(404, b'{"error":"not calibrated"}', "application/json")
            return

        if path == "/state":
            with lock:
                self._send(200, json.dumps(dict(state)).encode(), "application/json")
            return

        if path == "/events":
            self.stream_events()
            return

        self._send(404, b"not found", "text/plain")

    def stream_events(self):
        q: queue.Queue = queue.Queue(maxsize=8)
        with subs_lock:
            subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            with lock:
                first = json.dumps(dict(state))
            self.wfile.write(f"data: {first}\n\n".encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                    self.wfile.write(f"data: {msg}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # keeps proxies honest
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with subs_lock:
                if q in subscribers:
                    subscribers.remove(q)

    def do_POST(self):
        if self.path.split("?", 1)[0] == "/guest/cmd":
            q = urllib.parse.parse_qs(self.path.partition("?")[2])
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(min(n, 1024))
            if not guest_ok((q.get("t") or [""])[0]):
                self._send(403, b"{}", "application/json"); return
            try:
                ok = guest_command(json.loads(body.decode("utf-8") or "{}"))
            except (ValueError, UnicodeDecodeError, AttributeError):
                ok = False
            self._send(200 if ok else 400, json.dumps(guest_state()).encode(), "application/json")
            return
        if self.path != "/cmd":
            self._send(404, b"not found", "text/plain")
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            cmd = json.loads(self.rfile.read(n).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, b"bad json", "text/plain")
            return

        with lock:
            if "vol" in cmd:
                state["vol"] = max(0, min(100, int(cmd["vol"])))
            if "muted" in cmd:
                state["muted"] = bool(cmd["muted"])
            if "sel" in cmd:
                state["sel"] = max(0, min(len(MENU) - 1, int(cmd["sel"])))
            if "menuOpen" in cmd:
                state["menuOpen"] = bool(cmd["menuOpen"])
            # What the page shows, for a remote: {"screenInfo": {"path": [menu titles], "focus": row}}.
            # Not state["screen"]: that one ("menu", ...) decides whether the overlay shows the menu at all.
            if isinstance(cmd.get("screenInfo"), dict):
                state["screenInfo"] = {"path": list(cmd["screenInfo"].get("path") or []), "focus": cmd["screenInfo"].get("focus")}
            # Remote knob for the page: {"ui": {"key": "down"}} (up, down, enter,
            # back) reaches main-menu.html as the knob's own keys would. For
            # driving the screen from elsewhere -- a screen tour, a phone. The
            # sequence number makes a repeated key a new event.
            if isinstance(cmd.get("ui"), dict) and (cmd["ui"].get("key") in ("up", "down", "enter", "back", "track+", "track-")
                                                    or str(cmd["ui"].get("key")) in [f"d{c}" for c in "0123456789*#+"]):
                state["ui"] = {"seq": (state.get("ui") or {}).get("seq", 0) + 1, "key": cmd["ui"]["key"]}
        # Navigation from nav-watch.py. Straight through to the knob: the
        # knob's nav screen already exists and the host is only a courier here.
        # Not kept in `state`, because it is not something the page shows and
        # the page does not need waking for every distance tick.
        # Settings > App control, or voice: show the guest QR code ({"guest": "show"}), or make a new code first
        # ({"guest": "renew"}). The page opens its card when the sequence number moves.
        if cmd.get("guest") in ("show", "renew"):
            url = guest_url(renew=cmd["guest"] == "renew")
            with lock:
                state["guest"] = {"seq": (state.get("guest") or {}).get("seq", 0) + 1, "url": url}
                state["menuOpen"] = True        # over CarPlay too: the card is part of the menu layer
            publish()
        if isinstance(cmd.get("ipod"), dict):
            with lock:
                state["ipod"] = bool(cmd["ipod"].get("connected"))
                state["tracks"] = int(cmd["ipod"].get("tracks") or 0)
            publish()

        if isinstance(cmd.get("play"), dict):
            pl = cmd["play"]
            threading.Thread(target=play_album,
                             args=(str(pl.get("artist", "")),
                                   str(pl.get("album", ""))),
                             daemon=True).start()

        if isinstance(cmd.get("radio"), dict):
            r = cmd["radio"]
            if r.get("stop"):
                threading.Thread(target=stop_radio, daemon=True).start()
            elif r.get("toggle"):
                threading.Thread(target=toggle_radio, daemon=True).start()
            elif r.get("play"):
                threading.Thread(target=play_station,
                                 args=(str(r["play"]),), daemon=True).start()

        # Favourites from the COMAND screen (main-menu.html): add / remove / play.
        if isinstance(cmd.get("favourite"), dict):
            handle_favourite(cmd["favourite"])

        # The phone deciding to mirror is the driver choosing a source, the
        # same as pressing Mirror in the menu -- so it is handled the same
        # way, which is what stands the radio down. mirror-watch.py sends
        # this; uxplay has no hook of its own.
        if isinstance(cmd.get("mirror"), dict):
            on = bool(cmd["mirror"].get("active"))
            if on:
                threading.Thread(target=activate, args=("Mirror",),
                                 daemon=True).start()
            else:
                with lock:
                    was_mirror = state["source"] == "Mirror"
                if was_mirror:
                    # Back to the menu rather than to the last source: the
                    # phone has gone, and the workspace behind it is empty.
                    threading.Thread(target=show_home, daemon=True).start()
                    threading.Thread(target=knob_resting,
                                     daemon=True).start()

        # Play the startup animation again. It runs at power-on, which on a
        # car means the moment the ignition comes on -- so the only way to
        # look at it deliberately is to ask.
        # true plays the animation; a number holds the star still for that
        # many seconds, which is how you look at it without standing in front
        # of the car at the moment the ignition comes on.
        if cmd.get("splash"):
            want = cmd["splash"]
            want = int(want) if isinstance(want, (int, float)) and not isinstance(want, bool) else True
            threading.Thread(target=push_knob, args=({"splash": want},),
                             daemon=True).start()

        # Standby from anywhere, not only from the knob's five second hold:
        # the panel can offer it, and it can be tested without a thumb.
        if "standby" in cmd:
            threading.Thread(target=set_standby,
                             args=(bool(cmd["standby"]),), daemon=True).start()

        if isinstance(cmd.get("aux"), dict):
            # Logged before it is published: what the screens do with a
            # reading is their business, and a reading that never reached a
            # screen is still a reading that happened.
            if history:
                threading.Thread(target=log_aux, args=(cmd["aux"],),
                                 daemon=True).start()
            with lock:
                # Refused while the monitor is off. The service notices the
                # switch within a cycle, but a reading already on its way
                # arrives after the clear and puts half a battery page back --
                # numbers with nothing behind them, which is the thing the
                # clear was for.
                if state["ble"]:
                    state["aux"] = cmd["aux"]
            publish()

        # The car's own state: what car/vehicle.py reads from the car's Home
        # Assistant (engine CAN and K-line). Replaced whole, like aux; None when
        # that Home Assistant cannot be reached.
        if "vehicle" in cmd:
            veh = cmd["vehicle"] if isinstance(cmd["vehicle"], dict) else None
            # Trip log: the car's readings go into the same history as the aux
            # bank, under veh.* names, so a drive can be looked at afterwards.
            # Logged whole; the logger's deadbands keep it small.
            if veh and history:
                threading.Thread(target=log_aux, args=({"veh": veh},),
                                 daemon=True).start()
            with lock:
                state["vehicle"] = veh
            publish()

        # A position to test with, for a while: {"gps_test": {"lat", "lon", "track"}}.
        if isinstance(cmd.get("gps_test"), dict):
            global gps_test_until
            gps_test_until = time.time() + 30
            gps_fix.update(lat=float(cmd["gps_test"]["lat"]), lon=float(cmd["gps_test"]["lon"]),
                           track=cmd["gps_test"].get("track"), at=time.time())
        # From car/map-update.py; a new table means reopening it.
        if isinstance(cmd.get("map_status"), dict):
            ms = cmd["map_status"]
            with lock:
                state["mapState"] = str(ms.get("state", ""))
                if ms.get("updated"):
                    state["mapUpdated"] = str(ms["updated"])
            if ms.get("done"):
                global _speed_db
                _speed_db = None
            publish()
        if cmd.get("map_update_now"):
            with open(MAP_FORCE, "w"):
                pass
            run(["sudo", "-n", "systemctl", "start", "--no-block", "comand-map-update"])
            with lock:
                state["mapState"] = "Starting"
            publish()
        # Whether a phone is on CarPlay, from carplay.py.
        if "carplay_conn" in cmd:
            now = bool(cmd["carplay_conn"])
            with lock:
                changed = state["carplayConn"] != now
                state["carplayConn"] = now
            if changed:
                publish()
        # Music Assistant started playing here: everything else stands down.
        if cmd.get("ma_music"):
            from_video = bool(cmd.get("from_video"))   # the video itself asking
            def stand_down():
                radio_stand_down()
                bt_stand_down()
                if not from_video:
                    video_stand_down()
                import socket
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.sendto(b"pause", ("127.0.0.1", 7340))
            threading.Thread(target=stand_down, daemon=True).start()
        # CarPlay's music started: one music source at a time.
        if cmd.get("carplay_music"):
            threading.Thread(target=radio_stand_down, daemon=True).start()
            threading.Thread(target=video_stand_down, daemon=True).start()
        # Whether the Ooono app is connected to car/ooono-ble.py: {"ooono_conn": true}.
        if "ooono_conn" in cmd:
            with lock:
                state["ooono"] = {**(state["ooono"] or {}), "conn": bool(cmd["ooono_conn"])}
            publish()
        # The Ooono's button, pressed from here: {"ooono": {"btn": 300}}.
        if isinstance(cmd.get("ooono"), dict):
            send_to("knob", {"ooono": cmd["ooono"]})
        # As if the Ooono app had written this command byte, for testing
        # without a phone: {"ooono_test": "10"}; for a speed warning away from
        # a road, {"ooono_test": "13", "limit": 50}.
        if cmd.get("ooono_test") == "02":
            ooono_clear()
        if cmd.get("ooono_test") in OOONO:
            print(f"ooono: test {cmd['ooono_test']}", flush=True)
            ooono_warning(str(cmd["ooono_test"]), cmd.get("limit"))

        if isinstance(cmd.get("toast"), dict):
            t = dict(cmd["toast"])
            t["at"] = time.time()
            with lock:
                state["toast"] = t
            publish()
            threading.Thread(target=clear_toast, args=(t["at"],),
                             daemon=True).start()
            if t.get("knob", True):
                threading.Thread(target=push_knob_toast,
                                 args=(str(t.get("text", "")),),
                                 daemon=True).start()

        if "pairing" in cmd:
            p = cmd["pairing"]
            with lock:
                state["pairing"] = p if isinstance(p, dict) else None
            publish()

        # The YouTube video on the panel: {"video_playing": {"title": ...,
        # "playing": true}}, or null when it has stopped. Resent while it plays.
        if "video_playing" in cmd:
            global video_held
            v = cmd["video_playing"] if isinstance(cmd["video_playing"], dict) else None
            with lock:
                changed = v != state["nowvideo"]
                state["nowvideo"] = v
                # Playing again by hand, or gone: nothing left to carry on. Not
                # straight after the pause, when this can be a report from before it.
                if not v or (v.get("playing") and time.monotonic() - video_held_at > 3):
                    video_held = False
                in_call = bool(state["call"])
            if changed and not in_call:
                threading.Thread(target=knob_resting, daemon=True).start()
                publish()

        if cmd.get("btctl"):
            threading.Thread(target=bt_control, args=(str(cmd["btctl"]),),
                             daemon=True).start()

        # The phone's battery in percent, -1 when unknown: {"phonebat": 53}.
        if isinstance(cmd.get("phonebat"), int):
            with lock:
                changed = cmd["phonebat"] != state["phonebat"]
                state["phonebat"] = cmd["phonebat"]
                # Gone from Bluetooth: it can't tell us it was unplugged any more.
                if cmd["phonebat"] < 0:
                    state["phonecharging"] = False
            if changed:
                publish()

        # {"phonecharging": true} when the phone goes on the charger, false off it.
        if isinstance(cmd.get("phonecharging"), bool):
            with lock:
                state["phonecharging"] = cmd["phonecharging"]
            publish()

        # COMAND's << and >> from the page, with something playing: {"track": 1}.
        if cmd.get("track") in (1, -1):
            threading.Thread(target=source_step, args=(cmd["track"],), daemon=True).start()

        # Where the phone is in its track, on its own: a new position is not a new
        # track, and the knob and the cover have nothing to do for it.
        if "btpos" in cmd:
            with lock:
                if state["btmedia"]:
                    state["btmedia"] = {**state["btmedia"], "position": int(cmd["btpos"])}
            publish()

        if "btmedia" in cmd:
            m = cmd["btmedia"]
            m = m if isinstance(m, dict) else None
            if m:
                # The art is looked up by name, in a thread: it is a web
                # request, and the track has already changed on screen by the
                # time it answers.
                m["art"] = cover_name(m.get("artist", ""), m.get("album", ""),
                                      m.get("title", ""))
                if not m["art"]:
                    threading.Thread(target=fetch_cover,
                                     args=(m.get("artist", ""),
                                           m.get("album", ""),
                                           m.get("title", "")),
                                     daemon=True).start()
            with lock:
                was = state["btmedia"]
                state["btmedia"] = m
            # Two music sources cannot share the speakers. A phone that starts
            # playing is the driver choosing it, so the radio gets out of the
            # way -- and stays out: a phone pausing between tracks is not a
            # request for the radio back, and a car that lunged back to the
            # radio in every gap would be unusable.
            started = (m and m.get("status") == "playing"
                       and not (was and was.get("status") == "playing"))
            # A phone already saying "playing" that moves to another track is
            # starting too: it can report that for an app that was making no sound.
            new_track = (m and was and m.get("status") == "playing"
                         and m.get("title") != was.get("title"))
            if started:
                threading.Thread(target=radio_stand_down, daemon=True).start()
            if started or new_track:
                video_stand_down(hold=True)
            elif was and was.get("status") == "playing" and not (m and m.get("status") == "playing"):
                threading.Thread(target=video_resume_later, daemon=True).start()
            if m:
                # Which face the knob is on depends on what the panel is
                # doing: while the phone's screen is up, its track is what is
                # being mirrored, not a record we are playing, and the mark
                # says so.
                with lock:
                    mirroring = state["source"] == "Mirror"
                threading.Thread(
                    target=push_knob_mirror if mirroring else push_knob_cover,
                    args=(m,), daemon=True).start()
            elif was:
                # The phone has stopped, so the knob goes back to whatever the
                # car is doing instead -- which is knob_resting's decision,
                # not one to make again here.
                threading.Thread(target=knob_resting, daemon=True).start()
            publish()

        if isinstance(cmd.get("phone"), dict):
            ph = cmd["phone"]
            if ph.get("dial"):
                threading.Thread(target=dial, args=(str(ph["dial"]),),
                                 daemon=True).start()
            elif ph.get("answer"):
                threading.Thread(target=phone_command, args=("answer",),
                                 daemon=True).start()
            elif ph.get("hangup"):
                threading.Thread(target=phone_command, args=("hangup",),
                                 daemon=True).start()
            elif "typed" in ph:
                threading.Thread(target=knob_typed, args=(str(ph["typed"] or ""),),
                                 daemon=True).start()
            elif "micmute" in ph:
                threading.Thread(target=phone_mute,
                                 args=("mic", bool(ph["micmute"])),
                                 daemon=True).start()
            elif "spkmute" in ph:
                threading.Thread(target=phone_mute,
                                 args=("speaker", bool(ph["spkmute"])),
                                 daemon=True).start()

        if isinstance(cmd.get("nav"), dict):
            # To the nav display, not the knob. The knob is a control with a
            # screen; this is a screen with one job, and turn-by-turn belongs
            # on it.
            if not send_to("nav", {"m": "nav", "nav": cmd["nav"]}):
                print("nav update with no nav display connected", file=sys.stderr)

        # Settings the driver changes from the menu. Persisted immediately:
        # the next thing that happens to a car setting is usually the ignition
        # being switched off.
        if isinstance(cmd.get("setting"), dict):
            for k, v in cmd["setting"].items():
                if k == "boot_source":
                    settings["boot_source"] = str(v)
                    with lock:
                        state["bootSource"] = settings["boot_source"]
                elif k == "car_page":
                    # Not persisted: which Car screen you were last on is not
                    # a setting, it is where you are.
                    with lock:
                        state["carPage"] = str(v) if str(v) in ("sys", "veh") else "aux"
                elif k == "ble":
                    # Whether the aux battery monitor may use the radio at
                    # all. Off by default and off after a fresh install: there
                    # is one Bluetooth radio in this car and the phone's calls
                    # and music are on it, so anything else that wants it is
                    # something the driver turns on deliberately.
                    settings["ble"] = bool(v)
                    with lock:
                        state["ble"] = settings["ble"]
                        # Switching it off takes the readings with it. Nothing
                        # is updating them any more, and a battery page still
                        # showing 75% an hour later is not a stale number, it
                        # is a wrong one.
                        if not settings["ble"]:
                            state["aux"] = None
                elif k == "carplay":
                    set_carplay(bool(v))
                elif k == "phone_bt":
                    set_phone_bt(bool(v))
                elif k == "mic_serial":
                    set_mic_serial(str(v))
                elif k == "output":
                    set_output(str(v))
                elif k == "eq_on":
                    set_eq(bool(v))
                elif k == "sound_preset":
                    activate_preset(str(v))
                elif k == "style":
                    settings["style"] = str(v)
                    with lock:
                        state["style"] = settings["style"]
                elif k == "style_tune" and isinstance(v, dict):
                    # {"night": {"--bg": "#..."}}: merged per style; a value of
                    # null drops that override.
                    tune = {sk: dict(sv) for sk, sv in (settings.get("style_tune") or {}).items()}
                    for sk, sv in v.items():
                        if not isinstance(sv, dict): continue
                        t = tune.setdefault(str(sk), {})
                        for tk, tv in sv.items():
                            if tv is None: t.pop(tk, None)
                            else: t[str(tk)] = str(tv)
                    settings["style_tune"] = tune
                    with lock:
                        state["styleTune"] = tune
                elif k == "map_freq":
                    if v in MAP_FREQS:
                        settings["map_freq"] = v
                        with lock:
                            state["mapFreq"] = v
                elif k == "map_countries":
                    if isinstance(v, list) and v:
                        settings["map_countries"] = [str(c) for c in v]
                        with lock:
                            state["mapCountries"] = settings["map_countries"]
                elif k == "warn_style":
                    if v in WARN_STYLES:
                        settings["warn_style"] = v
                        # A look at it: on the screen only, no sound, and not on
                        # the knob, where a press would be a real report.
                        with lock:
                            state["warnStyle"] = v
                            state["warning"] = {"kind": "camera", "text": "Camera in 250 m",
                                                "ms": 3000, "at": time.time(),
                                                "colour": "#286eff", "limit": 80, "preview": True,
                                                "dist": 270, "start": 540}
                elif k == "voice_sound":
                    if v in VOICE_SOUNDS:
                        settings["voice_sound"] = v
                        with lock:
                            state["voiceSound"] = v
                        # The sound itself, so it can be chosen by ear.
                        import socket
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                            s.sendto(f"preview {v}".encode(), ("127.0.0.1", 7342))
                elif k == "nav_app":
                    if v in NAV_APPS:
                        settings["nav_app"] = v
                        with lock:
                            state["navApp"] = v
                elif k == "wake_word":
                    if v in WAKE_WORDS:
                        settings["wake_word"] = v
                        with lock:
                            state["wakeWord"] = v
                elif k == "warn_vol":
                    v = max(0, min(100, int(v)))
                    settings["warn_vol"] = v
                    with lock:
                        state["warnVol"] = v
                    # The sound, so the level can be set by ear.
                    threading.Thread(target=warn_sound, args=("camera",), daemon=True).start()
                elif k == "vol_style":
                    if v in VOL_STYLES:
                        settings["vol_style"] = v
                        with lock:
                            state["volStyle"] = v
                elif k == "hand":
                    # Which side the driver sits on. The controls a driver
                    # reaches for should be on the driver's side of the panel,
                    # and which side that is depends on the car rather than on
                    # anything the software can work out.
                    hand = "left" if str(v) == "left" else "right"
                    settings["hand"] = hand
                    with lock:
                        state["hand"] = hand
                elif k == "speed_volume":
                    settings["speed_volume"] = bool(v)
                    with lock:
                        state["speedVolume"] = settings["speed_volume"]
                        vol, muted = state["vol"], state["muted"]
                    # Apply at once: turning it off should drop any lift now,
                    # not at the next speed change.
                    set_output_volume(vol, muted)
                elif k == "speed_volume_strength":
                    if v in SPEED_LIFT_MAX:
                        settings["speed_volume_strength"] = v
                        with lock:
                            state["speedVolumeStrength"] = v
                            vol, muted = state["vol"], state["muted"]
                        set_output_volume(vol, muted)
                elif k == "profile_per_source":
                    settings["profile_per_source"] = bool(v)
                    with lock:
                        state["profilePerSource"] = settings["profile_per_source"]
                        src = state["source"]
                    # Turning it on adopts the current sound as this source's
                    # profile, so nothing jumps; off leaves everything as-is.
                    if settings["profile_per_source"]:
                        remember_profile(src)
            save_settings()
            publish()
            ha_sound_state()

        # Settings > Sound: balance, fader, bass, treble, EQ.
        if isinstance(cmd.get("sound"), dict):
            set_sound(cmd["sound"])
            publish()
        # Settings > Sound > Calibrate.
        if isinstance(cmd.get("calibrate"), dict):
            if cmd["calibrate"].get("start") and cal_proc is None:
                pid = cmd["calibrate"]["start"]
                threading.Thread(target=calibrate, args=(pid if isinstance(pid, str) else "driver",),
                                 daemon=True).start()
            elif cmd["calibrate"].get("next"):
                calibration_next()
            elif cmd["calibrate"].get("stop"):
                threading.Thread(target=stop_calibration, daemon=True).start()
        # {"video": {"brightness": -20, ...}} changes only the keys given;
        # {"video": {"reset": true}} puts all of them back to 0.
        if isinstance(cmd.get("video"), dict):
            set_video(cmd["video"])

        if cmd.get("activate"):
            threading.Thread(target=activate, args=(str(cmd["activate"]),),
                             daemon=True).start()
        with lock:
            mirror = {"vol": state["vol"], "muted": state["muted"]}
        # Every path that changes the volume has to reach the sink, not just
        # the knob's. The page and the API set the same number, and a number
        # that does not move the speakers is a lie told twice.
        set_output_volume(mirror["vol"], mirror["muted"])

        # The page moved something, so the knob has to be told: the point of
        # this bridge is that neither end is ever showing something stale.
        push_knob(mirror)
        publish()
        self._send(200, b"{}", "application/json")


def boot_source() -> None:
    """Put the car on its starting source once the compositor is up.

    Waits for sway rather than assuming: this service starts before it, and a
    workspace switch sent into nothing is silently lost -- which would leave
    the panel on whatever workspace sway happened to start on.
    """
    for _ in range(60):
        if sway_sock():
            break
        time.sleep(1)
    time.sleep(3)

    want = settings.get("boot_source", "menu")
    # CarPlay switched off: starting on it would be a black screen.
    if want == "Carplay" and not settings.get("carplay", True):
        want = "menu"
    if want in SOURCES:
        print(f"boot source: {want}", flush=True)
        activate(want)
    else:
        print("boot source: menu", flush=True)
        show_home()
    booted.set()


# ------------------------------------------------------------------ phone ----
# The phone over Bluetooth, through oFono.
#
# BlueZ pairs and PipeWire carries the audio, but neither can place a call:
# hands-free is a serial link carrying AT commands, and something has to own
# it. oFono does, and presents the connected phone as a modem on the system
# bus -- Dial, Answer, Hangup, and the call state as it changes. Without it we
# could hear a call and not make one, which is not a telephone.
#
# Polled rather than subscribed, like MPD above: a call changes state a handful
# of times in its life, the bus is local, and a signal subscription is one more
# thing to notice has died after oFono restarts.
OFONO = "org.ofono"
_sig_tick = 0

# oFono's own words for where a call is. Mapped to the three the knob draws,
# because "alerting" and "dialing" are the same thing to a driver: it is
# ringing at the other end and there is nothing to do but wait.
CALL_STATE = {
    "incoming": "ringing",      # someone is calling us
    "waiting": "ringing",
    "dialing": "dialing",
    "alerting": "dialing",
    "active": "active",
    "held": "active",
}


def ofono_bus():
    import dbus
    return dbus.SystemBus()


def ofono_modem():
    """The connected phone, or None.

    There is at most one: a car kit holds one hands-free link at a time.
    """
    try:
        import dbus
        bus = ofono_bus()
        mgr = dbus.Interface(bus.get_object(OFONO, "/"), OFONO + ".Manager")
        for path, props in mgr.GetModems():
            if props.get("Online") or props.get("Powered"):
                return str(path), str(props.get("Name") or "phone")
    except Exception:
        pass
    return None, ""


def ofono_call():
    """The call in progress, as the display wants it, or None."""
    path, _ = ofono_modem()
    if not path:
        return None
    try:
        import dbus
        bus = ofono_bus()
        vcm = dbus.Interface(bus.get_object(OFONO, path),
                             OFONO + ".VoiceCallManager")
        for cpath, props in vcm.GetCalls():
            state = CALL_STATE.get(str(props.get("State", "")))
            if not state:
                continue                    # disconnected, or one we do not draw
            # The name if the phone knows it, the number otherwise. A number is
            # not much use at a glance, but it beats a blank card.
            who = str(props.get("Name") or "").strip()
            number = str(props.get("LineIdentification") or "").strip()
            if not who:
                who = number
            # A number the driver has a name for should show the name. The
            # phone does this for its own contacts and the car cannot see
            # those, but it can see the car's list, which is where the numbers
            # dialled from here came from in the first place.
            named = contact_name(number)
            if named:
                who = named
            if who in ("", "withheld"):
                who = "Unknown"
            return {"path": str(cpath), "who": who,
                    "number": number, "status": state}
    except Exception:
        pass
    return None


RECENTS_PATH = "/var/lib/comand/recents.json"
RECENTS_MAX = 20


def load_recents() -> list[dict]:
    try:
        with open(RECENTS_PATH) as fh:
            return json.load(fh).get("recents", [])[:RECENTS_MAX]
    except Exception:
        return []


def add_recent(who: str, number: str, direction: str) -> None:
    """Remember a call.

    Hands-free carries no call history -- the phone has one and will not hand
    it over -- so the car keeps its own. Only what happened in this car, which
    is the useful half anyway: the number you dialled on the way here is the
    one you want on the way back.

    Same number twice running collapses to one entry with a newer time, or a
    call that fails and is retried three times fills the list with itself.
    """
    rows = load_recents()
    if rows and rows[0].get("number") == number:
        rows[0]["at"] = int(time.time())
        rows[0]["dir"] = direction
    else:
        rows.insert(0, {"name": who, "number": number,
                        "dir": direction, "at": int(time.time())})
    rows = rows[:RECENTS_MAX]
    try:
        os.makedirs(os.path.dirname(RECENTS_PATH), exist_ok=True)
        with open(RECENTS_PATH, "w") as fh:
            json.dump({"recents": rows}, fh)
    except Exception as e:
        print(f"could not save recents: {e}", file=sys.stderr)


def ofono_signal() -> tuple[int, str]:
    """Bars and operator, as the phone reports them.

    A car needs this more than a phone does: the phone is in a pocket and its
    own screen is not being looked at, so the only place the driver can learn
    that a call will not connect is here.
    """
    path, _ = ofono_modem()
    if not path:
        return -1, ""
    try:
        import dbus
        net = dbus.Interface(ofono_bus().get_object(OFONO, path),
                             OFONO + ".NetworkRegistration")
        props = net.GetProperties()
        return int(props.get("Strength", -1)), str(props.get("Name", "") or "")
    except Exception:
        return -1, ""


def phone_mute(kind: str, on: bool) -> None:
    """Mute the microphone, or the car's speakers.

    Two different things in two different places. The microphone belongs to
    the call and lives on the phone's side of the link, so it is oFono's
    CallVolume. The speakers are the car's and are already governed by the
    knob, so muting them is the same mute the knob does -- one mute per thing,
    rather than a second one that disagrees with the first.
    """
    if kind == "speaker":
        with lock:
            state["muted"] = on
            vol = state["vol"]
        set_output_volume(vol, on)
        push_knob({"vol": vol, "muted": on})
        publish()
        return

    path, _ = ofono_modem()
    if not path:
        return
    try:
        import dbus
        props = dbus.Interface(ofono_bus().get_object(OFONO, path),
                               OFONO + ".CallVolume")
        props.SetProperty("Muted", dbus.Boolean(on))
        with lock:
            state["micmuted"] = on
        publish()
    except Exception as e:
        print(f"phone mic mute: {e}", file=sys.stderr)


# ------------------------------------------------------- ducking for calls ----
# A call arriving while the radio plays has to take the cabin. Muting the sink
# would be wrong: the call comes out of the same speakers, so that would mute
# the call too. What has to stop is the music, and the music is a stream on
# that sink -- so it is the stream's own volume that moves.
#
# Faded rather than cut. A radio that stops dead sounds like a fault; one that
# ducks away in half a second sounds like the car answering the phone.
# Three seconds. Half a second was long enough to be a fade and short enough
# to still feel like something being switched off; three is the radio getting
# out of the way while the phone rings.
FADE_STEPS = 24
FADE_S = 3.0
# Switching source is deliberate; a call is not. Short enough that the station
# arrives when it was asked for, long enough not to be a click.
SWITCH_FADE_S = 1.2
music_was_playing = False


def find_stream(*needles: str) -> str:
    """PipeWire's index for a stream, found by what pactl says about it.

    Walks the blocks and looks inside each rather than taking the first on the
    sink: during a call or while a phone is playing there is more than one, and
    the first is whichever started first.
    """
    code, out = run(["pactl", "list", "sink-inputs"])
    if code != 0:
        return ""
    index, hit = "", ""
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Sink Input #"):
            if hit:
                return hit
            index = stripped.split("#", 1)[1].strip()
        elif index and any(n.lower() in stripped.lower() for n in needles):
            hit = index
    return hit


def fade_stream(idx: str, down: bool, seconds: float = FADE_S) -> None:
    """Take one stream's volume down or bring it back.

    The length is the caller's, because the two reasons to fade are not the
    same thing. A call interrupts, and three seconds is the radio getting out
    of the way while the phone rings. Choosing another source is deliberate,
    and three seconds of waiting for the station you just asked for is three
    seconds of wondering whether the car heard you.
    """
    if not idx:
        return
    levels = range(FADE_STEPS - 1, -1, -1) if down else range(0, FADE_STEPS + 1)
    for n in levels:
        pct = int(100 * n / FADE_STEPS)
        run(["pactl", "set-sink-input-volume", idx, f"{pct}%"])
        time.sleep(seconds / FADE_STEPS)


def fade_music(down: bool) -> None:
    """Take the radio out of the way, or bring it back."""
    idx = find_stream("COMAND", "mpd")
    if not idx:
        # Said out loud: music that keeps playing through a call is the
        # symptom, and "no stream found" is the cause nobody can see.
        print("fade: no music stream found", file=sys.stderr, flush=True)
        return
    print(f"fade {'down' if down else 'up'} on sink-input {idx}", flush=True)
    fade_stream(idx, down)


def bt_stand_down() -> None:
    """Fade the phone's music out and pause it, because the radio was chosen.

    The mirror of radio_stand_down. Paused on the phone rather than silenced
    here: our end going quiet would leave the phone playing to nobody, and the
    track somewhere else when the driver came back to it.
    """
    if not bt_active():
        return
    with lock:
        playing = (state["btmedia"] or {}).get("status") == "playing"
    if not playing:
        return
    print("radio chosen: standing the phone's music down", flush=True)
    idx = find_stream("bt-music")
    fade_stream(idx, True, SWITCH_FADE_S)
    bt_control("pause")
    # Volume back where it was, or the next track would arrive silent -- but
    # in the background, because the radio is waiting on this and nothing is
    # coming out of that stream to hear.
    threading.Thread(target=fade_stream, args=(idx, False, SWITCH_FADE_S),
                     daemon=True).start()


def duck_for_call(on: bool) -> None:
    """Fade the music down and pause it while a call is up, then restore it.

    Whether to resume is remembered rather than assumed: a driver who paused
    the radio before answering should not have it start again when they hang
    up.
    """
    global music_was_playing
    if on:
        with lock:
            music_was_playing = state["playing"]
        print(f"call: music was {'playing' if music_was_playing else 'stopped'}",
              flush=True)
        if not music_was_playing:
            return
        fade_music(down=True)
        mpd("pause 1")
        with lock:
            state["playing"] = False
        push_knob({"radio": {"playing": False}})
        publish()
        return

    if not music_was_playing:
        return
    music_was_playing = False
    mpd("play")
    # Volume back up after the music is running again, so the first thing heard
    # is the fade rather than silence at full level.
    fade_music(down=False)
    with lock:
        state["playing"] = True
    push_knob({"radio": {"playing": True}})
    publish()


def dial_string(number: str) -> str:
    """A number as oFono will take it.

    Digits, a leading +, and the two characters that mean something to a phone
    system: * and # for menus, and a comma for a pause. Everything else is how
    a human wrote it down.
    """
    out = []
    for i, ch in enumerate(str(number).strip()):
        if ch.isdigit() or ch in "*#,":
            out.append(ch)
        elif ch == "+" and not out:
            out.append(ch)
    return "".join(out)


def bt_control(action: str) -> None:
    """Tell the phone to play, pause or change track.

    AVRCP, through BlueZ's MediaPlayer1. The phone is the one playing, so the
    car asks rather than doing: pausing our end of the audio would leave the
    phone playing to nobody and the track would be somewhere else when it came
    back.
    """
    method = {"play": "Play", "pause": "Pause", "toggle": "Play",
              "next": "Next", "previous": "Previous"}.get(action)
    if not method:
        return
    try:
        import dbus
        bus = dbus.SystemBus()
        objs = dbus.Interface(bus.get_object("org.bluez", "/"),
                              "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
        for path, ifaces in objs.items():
            props = ifaces.get("org.bluez.MediaPlayer1")
            if props is None:
                continue
            if action == "toggle":
                method = "Pause" if str(props.get("Status")) == "playing" else "Play"
            player = dbus.Interface(bus.get_object("org.bluez", path),
                                    "org.bluez.MediaPlayer1")
            getattr(player, method)()
            print(f"bluetooth: {method}", flush=True)
            return
        print("bluetooth: no media player on the phone", file=sys.stderr)
    except Exception as e:
        print(f"bluetooth {action}: {e}", file=sys.stderr)


# ------------------------------------------------------------- mirroring ----
# Controlling the phone while it mirrors.
#
# AVRCP was the obvious channel and it is the wrong one: it belongs to the
# Bluetooth link, and a phone mirroring over the car's LAN has no reason to be
# paired at all. The knob's play/pause did nothing for exactly that reason --
# the phone was paired but not connected, so BlueZ had no MediaPlayer1 to send
# to, and the press went nowhere.
#
# AirPlay carries its own remote-control channel instead. The client hands the
# receiver a DACP-ID and an Active-Remote token when it connects; the ID names
# an mDNS service the phone itself publishes, and the token authorises
# requests to it. uxplay writes both to a file with -dacp, which is the only
# reason this is possible at all.
DACP_PATH = "/var/lib/comand/uxplay.dacp"

# What the phone answers to. These are iTunes' old remote-control endpoints,
# which is what DACP is.
DACP_VERBS = {"toggle": "playpause", "play": "play", "pause": "pause",
              "next": "nextitem", "previous": "previtem"}

_dacp_where: dict[str, tuple[str, int]] = {}     # DACP-ID -> host, port


def dacp_tokens() -> tuple[str, str] | None:
    """The current client's DACP-ID and Active-Remote token, or None.

    uxplay writes the two values bare, one per line and nothing else:

        A74DC78F39D05FD3
        3738931175

    This read them as "name: value" pairs, found neither, and reported no
    tokens -- so every press fell through to the Bluetooth fallback and got
    "no media player on the phone", while the phone was sitting there
    publishing a remote that answered on the first try. Labelled lines are
    still accepted in case a later uxplay starts writing them.
    """
    try:
        with open(DACP_PATH) as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return None

    labelled = {}
    bare = []
    for line in lines:
        if ":" in line:
            k, v = line.split(":", 1)
            labelled[k.strip().lower()] = v.strip()
        else:
            bare.append(line)

    ident = labelled.get("dacp-id") or (bare[0] if bare else None)
    token = labelled.get("active-remote") or (bare[1] if len(bare) > 1 else None)
    return (ident, token) if ident and token else None


def dacp_endpoint(ident: str) -> tuple[str, int] | None:
    """Where the phone is listening, from mDNS.

    The phone publishes this itself, under a name built from the ID it gave
    us. Cached, because resolving takes about a second and a driver pressing
    play does not want to wait for it -- but not cached across IDs, so a
    different phone is looked up afresh.
    """
    if ident in _dacp_where:
        return _dacp_where[ident]
    # Compared as numbers rather than as text. uxplay prints the ID as a plain
    # hex number and the phone pads it to sixteen digits, so a session whose
    # ID happens to start with a zero advertises "iTunes_Ctrl_0B7AED..." for a
    # file that says "B7AED...". Matching the strings worked perfectly until
    # an ID came up with a leading zero, and then reported a phone offering no
    # remote while it was publishing one.
    want = ident.lstrip("0").upper()
    prefix = "iTunes_Ctrl_"
    code, out = run(["avahi-browse", "-rpt", "_dacp._tcp"], timeout=10)
    if code != 0:
        return None
    for line in out.splitlines():
        # =;iface;proto;name;type;domain;host;address;port;txt
        f = line.split(";")
        if len(f) < 9 or f[0] != "=" or not f[3].startswith(prefix):
            continue
        if f[3][len(prefix):].lstrip("0").upper() != want:
            continue
        host, port = f[7], int(f[8])
        if ":" in host:                      # IPv6 needs brackets in a URL
            host = f"[{host}]"
        _dacp_where[ident] = (host, port)
        return host, port
    return None


def dacp(action: str) -> bool:
    """Send one transport command to the mirroring phone. True if it landed."""
    verb = DACP_VERBS.get(action)
    if not verb:
        return False
    tokens = dacp_tokens()
    if not tokens:
        return False
    ident, token = tokens
    where = dacp_endpoint(ident)
    if not where:
        print("mirror: the phone is not offering a remote", file=sys.stderr)
        return False
    host, port = where
    req = urllib.request.Request(f"http://{host}:{port}/ctrl-int/1/{verb}",
                                 headers={"Active-Remote": token})
    try:
        urllib.request.urlopen(req, timeout=3).read()
        print(f"mirror: {verb}", flush=True)
        return True
    except Exception as e:
        # A phone that has gone will not come back at the same place.
        _dacp_where.pop(ident, None)
        print(f"mirror {verb}: {e}", file=sys.stderr)
        return False


def phone_transport(action: str) -> None:
    """Play, pause or skip on the phone, by whichever channel it has.

    AirPlay's own remote first, because a mirroring phone always has that and
    may not be paired over Bluetooth at all; AVRCP after, because a phone that
    is paired answers it even when the DACP tokens are stale from an older
    session.
    """
    if dacp(action):
        return
    bt_control(action)


def source_kind() -> str:
    """What the knob's gestures should act on.

    What is playing wins. Only when nothing is does the last source chosen
    decide, because that is the one the driver was using and the one they mean
    to come back to.

    It used to ask "is the phone connected and does it have a track", which is
    true of a phone that was paused an hour ago -- so the swipe skipped the
    phone's tracks while the radio was the thing playing.
    """
    with lock:
        source = state["source"]
        station = state["radio"]
        radio_playing = state["playing"]
        bt = state["btmedia"] or {}
        video = state["nowvideo"]

    if video and video.get("playing"):
        return "video"

    # The phone's screen on the panel means the phone is what the knob acts
    # on, whatever AVRCP currently claims: a video app that reports nothing is
    # still the thing the driver is watching, and play/pause has to reach it.
    # This goes before the "what is playing wins" rule because during
    # mirroring nothing of ours is playing to win.
    if source == "Mirror":
        return "mirror"

    if bt.get("status") == "playing":
        return "bluetooth"
    if radio_playing:
        return "radio" if station else "library"
    if video:
        return "video"              # paused: the knob has to be able to resume it

    # Nothing is playing: go by what was last chosen.
    if source == "Music":
        return "library"
    if source == "Radio":
        return "radio" if station else "library"
    if bt.get("status"):
        return "bluetooth"          # paused, and nothing else has a claim
    return "radio" if station else "library"


def video_key(word: str) -> None:
    """A transport command for car/video-link.py."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(word.encode(), ("127.0.0.1", 7343))


# The video was paused for the phone's music or a call, and carries on after them.
# A video the driver paused, or one that made way for a source they chose instead,
# stays paused.
video_held = False
video_held_at = 0.0


def video_stand_down(hold: bool = False) -> None:
    """Pause the video because other sound has started. Its picture stays up, and
    the knob moves off it straight away instead of at the video's next report."""
    global video_held, video_held_at
    with lock:
        video = state["nowvideo"]
        if not hold:
            video_held = False
        if not (video and video.get("playing")):
            return
        if hold:
            video_held, video_held_at = True, time.monotonic()
        state["nowvideo"] = {**video, "playing": False}
    print("other sound started: pausing the video", flush=True)
    video_key("pause")


def video_resume_later() -> None:
    """Carry on with a held video once the phone has gone quiet. After a wait, because
    a phone pauses between tracks and around a call without being finished."""
    global video_held
    time.sleep(5)
    with lock:
        video = state["nowvideo"]
        if not (video_held and video and not video.get("playing")):
            return
        if (state["call"] or state["playing"]
                or (state["btmedia"] or {}).get("status") == "playing"):
            return
        video_held = False
        state["nowvideo"] = {**video, "playing": True}
    print("phone quiet: the video carries on", flush=True)
    video_key("play")
    knob_resting()


def source_step(step: int) -> None:
    """The next or previous of whatever is playing."""
    kind = source_kind()
    if kind == "video":
        video_key("forward" if step > 0 else "back30")
    elif kind == "mirror":
        phone_transport("next" if step > 0 else "previous")
    elif kind == "bluetooth":
        bt_control("next" if step > 0 else "previous")
    elif kind == "library":
        mpd("next" if step > 0 else "previous")
        print(f"library: {'next' if step > 0 else 'previous'}", flush=True)
    elif kind == "radio":
        sts = load_stations()
        with lock:
            cur = state["radio"]
        if sts:
            i = next((n for n, x in enumerate(sts) if x["name"] == cur), 0)
            play_station(sts[(i + step) % len(sts)]["name"])


def source_toggle() -> None:
    """Play or pause whatever is playing."""
    kind = source_kind()
    if kind == "video":
        video_key("toggle")
    elif kind == "mirror":
        phone_transport("toggle")
    elif kind == "bluetooth":
        bt_control("toggle")
    elif kind == "library":
        toggle_radio()          # the same MPD, and it already reads the state



def bt_active() -> bool:
    """Is the phone the thing making music right now.

    Paused counts. A driver who paused a track and reaches for the knob wants
    that track back, not the radio.
    """
    with lock:
        m = state["btmedia"]
    return bool(m and m.get("status") in ("playing", "paused"))


def resume_station(name: str) -> None:
    """Choosing Radio when a station is already loaded.

    Stands the phone down first, then makes sure MPD is actually playing, then
    puts the station back on the knob. In that order: the speakers should not
    carry both, and the picture should not change before the sound does.
    """
    bt_stand_down()
    st = (mpd_status().get("state") or "").strip()
    if st != "play":
        mpd("play")
        print(f"radio: resuming {name}", flush=True)
    with lock:
        state["playing"] = True
        state["source"] = "Radio"
    publish()
    push_knob_radio(name, True, True)


def radio_stand_down() -> None:
    """Fade the radio out because something else is playing.

    The same fade a call uses, and then a pause rather than a duck: a call is
    an interruption the radio comes back from, and another music source is not.
    """
    with lock:
        playing = state["playing"]
    if not playing:
        # Ask mpd too: a bridge restart forgets the radio while mpd plays on, and then nothing stood it down
        # (a video, or Music Assistant, played over it).
        try:
            playing = mpd_status().get("state") == "play"
        except OSError:
            playing = False
    if not playing:
        return
    print("radio: standing down for another source", flush=True)
    fade_music(down=True)
    mpd("pause 1")
    with lock:
        state["playing"] = False
    push_knob({"radio": {"playing": False}})
    # The stream's volume goes back up now rather than on the next play, or the
    # radio would come back silent whenever it is chosen again.
    fade_music(down=False)
    publish()


# The number on the panel's keypad, while the keypad is up. The page owns it;
# this is a copy so the knob can offer to dial it, and so its dial button
# calls the number the driver can see rather than one the knob remembers.
dial_typed = ""


def push_knob_dial() -> None:
    """The knob's "ready to call" face: the typed number and a dial button."""
    name = contact_name(dial_typed)
    push_knob({"m": "call", "call": {"who": name or dial_typed,
                                     "num": dial_typed, "status": "ready"}})


def knob_typed(number: str) -> None:
    """Follow the panel's keypad onto the knob."""
    global dial_typed
    was, dial_typed = dial_typed, number.strip()
    with lock:
        in_call = bool(state["call"])
    if in_call or was == dial_typed:
        return                  # the call owns the knob; it reads this at the end
    if dial_typed:
        push_knob_dial()
    else:
        knob_resting()


def phone_command(what: str, number: str = "") -> bool:
    """Dial, answer or hang up. True if the phone took it."""
    path, _ = ofono_modem()
    if not path:
        print("phone: nothing connected", file=sys.stderr)
        return False
    try:
        import dbus
        bus = ofono_bus()
        vcm = dbus.Interface(bus.get_object(OFONO, path),
                             OFONO + ".VoiceCallManager")
        if what == "dial" and number:
            # oFono wants a bare number. Contacts arrive as people write them
            # -- "+1 (555) 010-0123", "(+34)600 00 00 00" -- and every one of
            # those came back InvalidFormat, which looks from the driver's seat
            # like the car refusing to call somebody in its own phone book.
            number = dial_string(number)
            if not number:
                print("phone: nothing dialable in that number", file=sys.stderr)
                return False
            add_recent("", number, "out")
            # hide_callerid empty: withholding the car's number is not a
            # setting anybody has asked for, and the phone's own preference
            # already applies.
            vcm.Dial(number, "")
            print(f"phone: dialling {number}", flush=True)
            return True
        if what == "hangup":
            vcm.HangupAll()
            print("phone: hung up", flush=True)
            return True
        if what == "answer":
            with lock:
                cur = dict(state.get("call") or {})
            cpath = cur.get("path")
            if not cpath:
                return False
            dbus.Interface(bus.get_object(OFONO, cpath),
                           OFONO + ".VoiceCall").Answer()
            print("phone: answered", flush=True)
            return True
    except Exception as e:
        print(f"phone {what}: {e}", file=sys.stderr)
    return False


def dial(number: str) -> None:
    """Place a call, and say so on the knob when it cannot be placed.

    The knob shows "calling" the moment its button is tapped, so a dial that
    goes nowhere has to be undone -- otherwise it sits on a call that does not
    exist, with a hang-up button that has nothing to hang up. The toast says
    why, and ends on knob_resting, which puts the number back up.
    """
    if phone_command("dial", number):
        return
    path, _ = ofono_modem()
    push_knob_toast("No phone connected" if not path else "Call failed")


def phone_watch() -> None:
    """Follow the phone and the call, and keep both screens in step."""
    was_call = None
    was_phone = False
    while True:
        path, name = ofono_modem()
        connected = bool(path)
        call = ofono_call()

        # Signal is read on a slower beat than the call: it moves constantly by
        # a point or two and republishing on every change would push the whole
        # state to both screens several times a second for a bar that has not
        # visibly moved.
        global _sig_tick
        _sig_tick = (_sig_tick + 1) % 5
        sig_changed = False
        if _sig_tick == 0 and connected:
            strength, operator = ofono_signal()
            # Quantised to what is drawn. Five bars means a change of twenty
            # points matters and a change of three does not.
            with lock:
                before = state["signal"] // 20 if state["signal"] >= 0 else -1
                now = strength // 20 if strength >= 0 else -1
                sig_changed = (now != before) or (operator != state["operator"])
                state["signal"] = strength
                state["operator"] = operator

        # Compared on what is drawn, not on the whole object: the call's object
        # path changes nothing on screen and would republish every second.
        drawn = (call or {}).get("who"), (call or {}).get("status")
        if connected and not was_phone:
            # A phone has just connected. Ask it for its contacts, in a thread:
            # the pull takes seconds and this loop owes the screens a state
            # update every one of them.
            threading.Thread(target=sync_phonebook, daemon=True).start()

        if connected != was_phone or drawn != was_call or sig_changed:
            # A call that arrives goes in the list too, so "recents" is calls
            # rather than only the ones this car placed.
            if call and drawn != was_call and call["status"] == "ringing":
                add_recent(call["who"], call["who"], "in")
            with lock:
                state["phone"] = name if connected else ""
                state["call"] = call
            started = bool(call) and not bool(was_call and was_call[0])
            ended = not call and bool(was_call and was_call[0])

            if call:
                push_knob({"m": "call",
                           "call": {"who": call["who"], "num": call["number"],
                                    "status": call["status"]}})
            elif ended and dial_typed:
                # Back on the keypad, which still holds the number: the knob
                # offers it again, the way the panel's green key does.
                push_knob_dial()
            elif ended:
                # The call is over: the knob goes back to what it was showing.
                # This used to hang off the same branch as the audio wiring and
                # stopped running when that changed, which left the knob saying
                # "in call" until something else happened to redraw it.
                with lock:
                    playing_station = state["radio"]
                    video = state["nowvideo"]
                if video:
                    threading.Thread(target=knob_resting, daemon=True).start()
                else:
                    push_knob({"m": "radio" if playing_station else "idle"})

            if started:
                video_stand_down(hold=True)
            elif ended:
                threading.Thread(target=video_resume_later, daemon=True).start()

            if started or ended:
                # The music gets out of the way first, so the fade has begun
                # before the phone's audio arrives.
                threading.Thread(target=duck_for_call, args=(started,),
                                 daemon=True).start()
                if started:
                    threading.Thread(target=follow_call_audio, daemon=True).start()

            publish()
            was_call, was_phone = drawn, connected
        time.sleep(1)


def radio_watch() -> None:
    """Follow the stream's title while the radio is playing.

    Internet radio has no tags -- only the ICY title the station pushes -- so
    this is the only way to show what is on. Polled rather than subscribed
    because MPD's idle would mean holding a connection open across every tunnel
    the car drives through.
    """
    while True:
        with lock:
            playing = bool(state["radio"])
        if playing:
            info = mpd_status()
            title = (info.get("title") or "").strip()
            # MPD falls back to the URL's last path element when a stream sends
            # no ICY title, so "fip-midfi.mp3" turns up as the song. A filename
            # is not a track, and the station name is the more honest answer.
            if not title or "/" in title or title.lower().endswith(
                    (".mp3", ".aac", ".m3u8", ".ogg")):
                with lock:
                    title = state["radio"]
            playing = (info.get("state") or "").strip() == "play"
            with lock:
                moved = playing != state["playing"]
                changed = (title != state["song"]) or moved
                state["song"] = title
                state["playing"] = playing
            if moved:
                # A stream that drops stops the player without anyone pressing
                # anything, and a knob still showing "playing" would be the
                # only thing in the car that had not noticed.
                push_knob({"radio": {"playing": playing}})
            if changed:
                publish()
        time.sleep(3)


def android_autostart() -> None:
    """Put Sygic in front as soon as Android is ready.

    The Android launcher is not part of this product and should never be on
    screen. Waiting for boot_completed and then bringing Sygic forward means
    the first thing the panel shows for that source is the map.
    """
    pkg = SOURCES.get("Navigation", {}).get("pkg")
    if not pkg:
        return
    for _ in range(60):
        code, out = run(["waydroid", "shell", "--", "getprop",
                         "sys.boot_completed"], timeout=10)
        if out.strip() == "1":
            bring_forward(pkg, tries=10)
            return
        time.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    global HTTP_PORT
    ap.add_argument("--device", default=None, help="serial port; default is by USB vid")
    ap.add_argument("--no-serial", action="store_true")
    args = ap.parse_args()
    HTTP_PORT = args.port
    load_settings()
    apply_settings_to_state()
    if not settings.get("phone_bt", True):
        threading.Thread(target=apply_phone_bt, args=(False,), daemon=True).start()
    state["bootSource"] = settings.get("boot_source", "menu")

    if not args.no_serial:
        threading.Thread(target=scan_thread, daemon=True).start()
        threading.Thread(target=gps_watch, daemon=True).start()
    threading.Thread(target=android_autostart, daemon=True).start()
    threading.Thread(target=boot_source, daemon=True).start()
    threading.Thread(target=radio_watch, daemon=True).start()
    threading.Thread(target=knob_clock, daemon=True).start()
    threading.Thread(target=knob_volts, daemon=True).start()
    threading.Thread(target=phone_watch, daemon=True).start()
    threading.Thread(target=umik_watch, daemon=True).start()
    threading.Thread(target=output_watch, daemon=True).start()
    threading.Thread(target=sound_pusher, daemon=True).start()
    threading.Thread(target=speed_vol_watch, daemon=True).start()
    threading.Thread(target=ha_link, daemon=True).start()
    threading.Thread(target=screen_ticker, daemon=True).start()
    load_stations()
    load_favourites()
    with lock:
        state["favourites"] = list(favourites)
    set_output_volume(state["vol"], state["muted"])

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"http://{args.host}:{args.port}/  (page, /events, /state, POST /cmd)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
