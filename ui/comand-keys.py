#!/usr/bin/env python3
"""COMAND's own rotary knob and buttons drive our menu.

In TV mode COMAND sends its key presses to the TV tuner over the media bus; the rbcan box (rbcan/, a CANable with
Open-TV-COMAND) is that tuner and types them as a USB keyboard. This takes that keyboard for itself (so the keys
do not also land in Android or CarPlay) and hands them to the page as the remote knob, bridge /cmd {"ui": {...}}.

  rotary + / -       keypad + / -       -> down / up
  rotary press, 5    Enter              -> enter
  RET                Backspace          -> back
  DEL                Delete             -> del  (last digit on the phone keypad)
  0-9, *, #          keypad digits      -> d0 ... d9, d*, d#  (typed on the phone keypad)
  0 held            keypad 0 held      -> d+  (a phone's hold-zero-for-plus)
  2 / 8              arrow up / down    -> up / down     (only in the firmware's arrow mode)
  4 / 6              arrow left / right -> back / enter
  << / >> (and the steering wheel's)  B / N   -> track- / track+  (the page moves the cursor if nothing is playing)

While CarPlay is the source the same keys go to the phone instead (carplay/carplay.py's knob port): turn = left /
right, press = select, RET = back. DEL, or holding RET, leaves CarPlay for our menu.

A remote (a phone app, through the bridge) sends key names to 127.0.0.1:7344; they go through the same rules.

  comand-keys.py [--dry-run]       --dry-run: print the keys, take nothing, send nothing
"""
import argparse, glob, json, socket, sys, threading, time, urllib.request

import evdev
from evdev import ecodes as e

BRIDGE = "http://127.0.0.1:8080/cmd"
STATE = "http://127.0.0.1:8080/state"
CARPLAY = ("127.0.0.1", 7340)
VOICE = ("127.0.0.1", 7342)      # car/voice.py
VIDEO = ("127.0.0.1", 7343)      # car/video-link.py
REMOTE = ("127.0.0.1", 7344)     # keys from a remote, see remote()
TO_VIDEO = {"enter": "toggle", "down": "forward", "up": "back30", "back": "stop",
            "track+": "forward", "track-": "back30"}
TO_CARPLAY = {"down": "right", "up": "left", "enter": "select", "back": "back",
              "track+": "right", "track-": "left"}
HOLD = 0.8                       # seconds of RET that mean "out of CarPlay"
# The box holds a key down for as long as COMAND does: a 0 held this long is a +,
# sent while still held, and a shorter one is a 0, sent on release.
ZERO_PLUS = 0.6
source = {"now": ""}
DEVICE = "/dev/input/by-id/usb-STMicroelectronics_STM32_COMPOSITE_DEVICE_*-event-kbd"
KEYS = {
    e.KEY_KPPLUS: "down", e.KEY_KPMINUS: "up",
    e.KEY_ENTER: "enter", e.KEY_KPENTER: "enter", e.KEY_BACKSPACE: "back", e.KEY_DELETE: "del",
    e.KEY_UP: "up", e.KEY_DOWN: "down", e.KEY_LEFT: "back", e.KEY_RIGHT: "enter",
    e.KEY_B: "track-", e.KEY_N: "track+",
    # COMAND's number keys (the firmware's digit mode): typed on our phone keypad.
    **{getattr(e, f"KEY_KP{n}"): f"d{n}" for n in range(10)},
    e.KEY_KPASTERISK: "d*", e.KEY_KPDOT: "d#",
}


def send(key: str) -> None:
    req = urllib.request.Request(BRIDGE, data=json.dumps({"ui": {"key": key}}).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError as err:
        print(f"bridge: {err}", file=sys.stderr)


def follow_source() -> None:
    """Keep up with which source is on screen, without blocking a key on a request."""
    while True:
        try:
            with urllib.request.urlopen(STATE, timeout=2) as r:
                st = json.load(r)
            # Our menu raised over CarPlay takes the knob back.
            source["now"] = "" if st.get("menuOpen") else str(st.get("source") or "")
            source["raw"] = str(st.get("source") or "")
            source["cp_live"] = bool(st.get("carplayConn"))
            w = st.get("warning") or {}
            source["warn_until"] = float(w.get("at") or 0) + float(w.get("ms") or 0) / 1000
        except (OSError, ValueError):
            pass
        time.sleep(0.4)


def ooono_knob(held: int) -> None:
    """The button again for the knob, when the knob is the box the app talks to."""
    req = urllib.request.Request(BRIDGE, data=json.dumps({"ooono": {"btn": max(50, min(4000, held))}}).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError as err:
        print(f"bridge: {err}", file=sys.stderr)


def activate(label: str) -> None:
    req = urllib.request.Request(BRIDGE, data=json.dumps({"activate": label}).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except OSError as err:
        print(f"bridge: {err}", file=sys.stderr)


class Zero:
    """Keypad 0: a tap is 0, a hold is +."""

    def __init__(self, emit):
        self.emit, self.timer, self.plus = emit, None, False

    def down(self) -> None:
        self.plus = False
        self.timer = threading.Timer(ZERO_PLUS, self.held); self.timer.daemon = True; self.timer.start()

    def held(self) -> None:
        self.plus = True
        self.emit("d+")

    def up(self) -> None:
        if self.timer: self.timer.cancel(); self.timer = None
        if not self.plus:
            self.emit("d0")


class Keys:
    """What a key does, wherever it came from: COMAND's keyboard, or a remote (a phone app) through REMOTE."""

    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ret_down = None
        self.ooono_key, self.ooono_at = None, 0.0
        self.zero = Zero(lambda k: (print(f"KEY_KP0 -> {k}", flush=True), dry_run or send(k)))
        self.lock = threading.Lock()        # keyboard and remote threads take turns

    def handle(self, code: int, value: int) -> None:
        with self.lock:
            self._handle(code, value)

    def _handle(self, code: int, value: int) -> None:
        a, udp = self, self.udp
        key = KEYS.get(code)
        carplay = source["now"] == "Carplay"
        # The Ooono's button: the knob while a warning is up, as on the
        # real box and the knob, and 9 any time outside the phone keypad.
        # Sent on release with how long it was held: the app tells a
        # press from a hold.
        if source.get("raw") == "Video" and key in TO_VIDEO:
            if value == 1 and not a.dry_run:
                udp.sendto(TO_VIDEO[key].encode(), VIDEO)
            return
        if key == "d7" and source.get("raw") != "Telephone":
            if value == 1 and not a.dry_run:     # 7: voice command
                print("KEY_KP7 -> voice", flush=True)
                udp.sendto(b"listen", VOICE)
            return
        if code == self.ooono_key and value == 0:
            held = int((time.monotonic() - self.ooono_at) * 1000)
            self.ooono_key = None
            print(f"{e.KEY.get(code, code)} -> ooono button ({held} ms)", flush=True)
            if not a.dry_run:
                ooono_knob(held)
            return
        if value == 1 and ((key == "enter" and time.time() < source.get("warn_until", 0))
                           or (key == "d9" and source.get("raw") != "Telephone")):
            self.ooono_key, self.ooono_at = code, time.monotonic()
            return
        if carplay and key == "back":
            # RET acts on release, so a hold can mean something else.
            if value == 1:
                self.ret_down = time.monotonic(); return
            held = self.ret_down is not None and time.monotonic() - self.ret_down >= HOLD
            self.ret_down = None
            # No phone on CarPlay: nothing would answer "back", and the screen is black. Any RET leaves.
            held = held or not source.get("cp_live", True)
            print(f"RET {'held' if held else ''} -> {'menu' if held else 'carplay back'}", flush=True)
            if not a.dry_run:
                if held: activate("Home")
                else: udp.sendto(b"back", CARPLAY)
            return
        if carplay and key == "del":
            if value == 1:
                print("DEL -> menu", flush=True)
                if not a.dry_run: activate("Home")
            return
        if key == "d0" and not carplay:
            if value == 1: self.zero.down()
            else: self.zero.up()
            return
        if value != 1:
            return
        print(f"{e.KEY.get(code, code)} -> {key}{' (carplay)' if carplay else ''}", flush=True)
        if key and not a.dry_run:
            if carplay:
                if key in TO_CARPLAY: udp.sendto(TO_CARPLAY[key].encode(), CARPLAY)
                elif key in ("d*", "d#"): send(key)      # volume
            else: send(key)


# A remote's key names -> the keyboard code COMAND's box would type for it.
REMOTE_CODES = {"up": e.KEY_KPMINUS, "down": e.KEY_KPPLUS, "enter": e.KEY_ENTER, "back": e.KEY_BACKSPACE,
                "del": e.KEY_DELETE, "track-": e.KEY_B, "track+": e.KEY_N,
                **{f"d{n}": getattr(e, f"KEY_KP{n}") for n in range(10)}, "d*": e.KEY_KPASTERISK, "d#": e.KEY_KPDOT}


def remote(keys: Keys) -> None:
    """Keys from a remote (bridge: MQTT comand/remote/key) as UDP datagrams on REMOTE: a key name, or
    "back-hold" (RET held). Each becomes a press and release of the matching key, so it does exactly
    what that key on COMAND does in every source."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(REMOTE)
    while True:
        name = sock.recv(64).decode(errors="replace").strip()
        hold = name == "back-hold"
        code = REMOTE_CODES.get("back" if hold else name)
        if code is None:
            print(f"remote: unknown key {name!r}", file=sys.stderr); continue
        print(f"remote: {name}", flush=True)
        if hold and source["now"] != "Carplay":
            # Held RET only means something to CarPlay; elsewhere the remote's hold goes to the main menu.
            keys.dry_run or activate("Home"); continue
        keys.handle(code, 1)
        time.sleep(HOLD + 0.1 if hold else 0.03)
        keys.handle(code, 0)


def main() -> None:
    ap = argparse.ArgumentParser(); ap.add_argument("--dry-run", action="store_true"); a = ap.parse_args()
    threading.Thread(target=follow_source, daemon=True).start()
    keys = Keys(a.dry_run)
    threading.Thread(target=remote, args=(keys,), daemon=True).start()
    while True:
        paths = glob.glob(DEVICE)
        if not paths:
            time.sleep(3); continue                  # box unplugged or flashing: wait for it
        try:
            dev = evdev.InputDevice(paths[0])
            if not a.dry_run:
                dev.grab()
            print(f"reading {dev.path} ({dev.name})", flush=True)
            for ev in dev.read_loop():
                if ev.type != e.EV_KEY or ev.value == 2:       # no auto-repeat
                    continue
                keys.handle(ev.code, ev.value)
        except OSError as err:                       # unplugged mid-read
            print(f"device: {err}", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    main()
