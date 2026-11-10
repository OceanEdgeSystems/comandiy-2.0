#!/usr/bin/env python3
"""CarPlay / Android Auto on the panel, via the Carlinkit dongle.

probe.py established that the dongle enumerates, claims and completes a
handshake. This turns that into something you can watch: it holds the session
open, keeps the link alive, and feeds the H.264 the dongle sends into GStreamer,
which decodes it and puts it on screen as a Wayland window.

    dongle --USB bulk--> this --stdin--> gst-launch-1.0 --> Wayland surface

WHY IT HAS TO HOLD THE SESSION
The dongle resets itself when the host goes away, and it goes away every time a
probe exits. The USB log during testing was a device connecting and
disconnecting on a nine-second cycle, which reads like failing hardware and is
not -- it is the dongle waiting for a host that stays. So this reconnects
rather than exits, and sends a heartbeat.

WHY GSTREAMER AND NOT OUR OWN DECODER
The CM5 has no hardware H.264 decoder (see docs/carplay-headunit.md), so this is
software decode either way. avdec_h264 into waylandsink is a few lines and gets
frames on screen today; the daemon in the design note can replace it once there
is something to compare against.

CARPLAY OR ANDROID AUTO
Both, and the same code. The dongle projects whichever phone connects and the
message stream is identical -- only the pixels differ. The paired list is
printed at startup so it is clear which one is about to appear.

Usage:
    carplay.py                 # session + window, reconnecting
    carplay.py --no-video      # protocol only, log message types
    carplay.py --record FILE   # also write the raw H.264 stream
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import struct
import subprocess
import sys
import urllib.request
import threading
import time

VENDOR = 0x1314
PRODUCTS = (0x1520, 0x1521)
MAGIC = 0x55AA55AA
HDR = struct.Struct("<IIII")

T_OPEN, T_VIDEO, T_AUDIO, T_SENDFILE, T_HEARTBEAT = 0x01, 0x06, 0x07, 0x99, 0xAA
T_TOUCH, T_COMMAND = 0x05, 0x08
# BoxSettings is how the head unit describes itself to the dongle, and the only
# way to ask for turn-by-turn; MetaData is what carries it back.
T_BOXSETTINGS, T_METADATA = 0x19, 0x2A

# Touch: action, x, y, flags. Coordinates are normalised to 0..10000 across the
# projected display rather than pixels, so the phone never needs our geometry.
# The actions are these values, not 0/1/2.
TOUCH_DOWN, TOUCH_MOVE, TOUCH_UP = 14, 15, 16
TOUCH_SCALE = 10000

# Command values (msgtype 8). The button set is why the knob can drive CarPlay
# natively later: rotation is BtnLeft/BtnRight and a press is BtnSelectDown.
CMD_AUDIO_TO_HOST = 23         # phone audio comes to us, not out of the dongle
CMD_SUPPORT_WIFI = 1000        # asks the dongle to offer a wireless session
CMD_SUPPORT_WIFI_NEED_KO = 1012
# The d-pad's commands: the rotary ones (111, 112) did not move between icons.
CMD_BTN_LEFT, CMD_BTN_RIGHT = 100, 101
CMD_BTN_SELECT_DOWN, CMD_BTN_SELECT_UP = 104, 105
CMD_BTN_BACK = 106

TYPES = {
    0x01: "Open", 0x06: "VideoData", 0x07: "AudioData", 0x08: "Command",
    0x0d: "BluetoothDeviceName", 0x0e: "WifiDeviceName",
    0x10: "DashboardData", 0x12: "BluetoothPairedList", 0x19: "BoxSettings",
    0x29: "GnssData", 0x2A: "MetaData", 0x2C: "ClusterVideoData",
    0x26: "Unknown26", 0x99: "SendFile", 0xAA: "HeartBeat",
    0xCC: "SoftwareVersion",
}

# The first 20 bytes of a VideoData payload are a header (width, height,
# flags, length, unused) and not part of the stream. Feeding them to the
# decoder produces a picture that never appears and no error.
VIDEO_HDR = 20

HEARTBEAT_S = 2.0

# Read whole USB packets into a buffer and parse messages out of it, rather
# than reading exactly a header and then exactly a payload. Asking for 16 bytes
# while the device has a 512-byte packet queued fails with [Errno 75] Overflow,
# which is what reconnecting onto a stream already in flight looks like.
READ_CHUNK = 16384              # a multiple of the 512-byte endpoint size
MAGIC_BYTES = struct.pack("<I", MAGIC)


def adopt_compositor(quiet: bool = False) -> bool:
    """Point GStreamer and swaymsg at the running compositor.

    Started as its own unit rather than from sway's config, because a process
    sway execs escapes the unit's cgroup: restarting the session left the old
    client running, and two clients claiming the same dongle reset it between
    them and produced a black screen with everything apparently healthy.

    The cost of being a separate unit is that WAYLAND_DISPLAY and SWAYSOCK are
    not inherited, and sway's socket names carry its pid, so they are found here
    instead of guessed.
    """
    import glob
    run_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"

    # Looked up per session, not once. This service starts before the
    # compositor, so the first look finds nothing -- and looking only once left
    # WAYLAND_DISPLAY unset for the life of the process, so GStreamer failed
    # with "Failed to set pipeline to PAUSED" while the dongle streamed
    # perfectly. Sway's socket name also carries its pid, so a value found once
    # goes stale across a compositor restart.
    socks = [x for x in sorted(glob.glob(os.path.join(run_dir, "wayland-[0-9]*")),
                               key=os.path.getmtime, reverse=True)
             if not x.endswith(".lock")]
    if socks:
        os.environ["WAYLAND_DISPLAY"] = os.path.basename(socks[0])
    elif "WAYLAND_DISPLAY" in os.environ:
        del os.environ["WAYLAND_DISPLAY"]

    ipc = sorted(glob.glob(os.path.join(run_dir, "sway-ipc.*.sock")),
                 key=os.path.getmtime, reverse=True)
    if ipc:
        os.environ["SWAYSOCK"] = ipc[0]

    if not quiet:
        print(f"compositor: WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')}",
              flush=True)
    return bool(socks)


def run(cmd: list[str], timeout: float = 5.0) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except Exception as e:
        return 1, str(e)


def i32(v: int) -> bytes:
    return struct.pack("<I", v)


def frame(mtype: int, payload: bytes = b"") -> bytes:
    return HDR.pack(MAGIC, len(payload), mtype, mtype ^ 0xFFFFFFFF) + payload


def sendfile(path: str, content: bytes) -> bytes:
    p = path.encode() + b"\0"
    return frame(T_SENDFILE,
                 struct.pack("<I", len(p)) + p +
                 struct.pack("<I", len(content)) + content)


def touch_packet(action: int, x: float, y: float) -> bytes:
    """x and y are 0.0-1.0 across the projected display."""
    return frame(T_TOUCH, struct.pack(
        "<IIII", action,
        max(0, min(TOUCH_SCALE, int(x * TOUCH_SCALE))),
        max(0, min(TOUCH_SCALE, int(y * TOUCH_SCALE))), 0))


# ---------------------------------------------------------------- view area --
# What the phone is told it may draw into, and where. Found by unpacking LIVI's
# AppImage after the phone kept rendering a 320x230 picture into the corner of
# an 800x480 frame no matter what the Open message asked for: LIVI sends two
# files we did not, and the dongle keeps them in its own /etc.
#
#     /etc/RiddleBoxData/HU_VIEWAREA_INFO   24 bytes, six LE uint32
#         width, height, view width, view height, left inset, top inset
#     /etc/RiddleBoxData/HU_SAFEAREA_INFO   20 bytes, five LE uint32
#         safe width, safe height, left, top, draw-outside flag
#
# That is why the size was stuck: these persist on the dongle across sessions
# and across whichever host is driving it. LIVI ran here first, configured for
# the 320x230 COMAND panel, and wrote 320x230 into the dongle. Every session
# since has inherited it -- which is also why changing settings in the dongle's
# own menu did nothing, and why asking for 1280x720 in Open changed nothing.
#
# No insets: the panel has no rounded corners or overlapping furniture to keep
# clear of, so the view area is the whole screen and the safe area is the whole
# view area.


def view_area(width: int, height: int) -> bytes:
    return sendfile("/etc/RiddleBoxData/HU_VIEWAREA_INFO",
                    struct.pack("<6I", width, height, width, height, 0, 0))


def safe_area(width: int, height: int) -> bytes:
    return sendfile("/etc/RiddleBoxData/HU_SAFEAREA_INFO",
                    struct.pack("<5I", width, height, 0, 0, 0))


# ----------------------------------------------------------------- car icon --
# The icon CarPlay shows for the car itself -- the "OEM" app in CarPlay's dock,
# whose tap means "take me back to the car's own screens". Found the same way as
# the view area, in LIVI's AppImage: three PNGs the dongle keeps in its /etc,
# plus /etc/oem_icon.png and an airplay.conf that makes the icon visible and
# names it. LIVI's airplay.conf is exactly these five keys; name and model are
# its values, kept as they are so the file says nothing new about the dongle.
#
# Tapping the icon comes back as Command 3 (requestHostUI) -- see run(), which
# hands it to the bridge as "go to the menu".
#
# The phone reads the icon when a session starts, so a new one shows after the
# phone next connects, not mid-session. Icons: tools/make-carplay-icon.py.
CMD_REQUEST_HOST_UI = 3
ICON_DIRS = ("/usr/local/share/comand-ui/carplay-icons",
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons"))


def oem_icon(label: str) -> list[tuple[str, bytes]]:
    """The car icon and its label, as (log label, packet); empty without icons."""
    def read(size: int) -> bytes | None:
        for d in ICON_DIRS:
            try:
                with open(os.path.join(d, f"oem-{size}.png"), "rb") as fh:
                    return fh.read()
            except OSError:
                continue
        return None

    icons = {size: read(size) for size in (120, 180, 256)}
    if not all(icons.values()):
        print("car icon: no icons found, leaving the dongle's own", flush=True)
        return []
    conf = (f"oemIconVisible = 1\nname = AutoBox\nmodel = Magic-Car-Link-1.00\n"
            f"oemIconPath = /etc/oem_icon.png\noemIconLabel = {label}")
    return [("icon_120", sendfile("/etc/icon_120x120.png", icons[120])),
            ("icon_180", sendfile("/etc/icon_180x180.png", icons[180])),
            ("icon_256", sendfile("/etc/icon_256x256.png", icons[256])),
            ("oem_icon", sendfile("/etc/oem_icon.png", icons[256])),
            ("airplay_conf", sendfile("/etc/airplay.conf", conf.encode("ascii", "replace")))]


def host_ui() -> None:
    """The car icon was tapped in CarPlay: show the COMAND menu."""
    try:
        req = urllib.request.Request(
            BRIDGE, data=json.dumps({"activate": "Home"}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
        print("car icon tapped -> menu", flush=True)
    except Exception as e:
        print(f"car icon tapped, bridge did not answer: {e}", flush=True)


PHONE_GONE_S = 3.0
# CarPlay's clock redraws the screen every minute, so a real session is never silent
# for this long. A phone that goes out of range can leave without the dongle saying so.
PHONE_SILENT_S = 180.0
last_video = 0.0


def _phone_lost() -> None:
    global _phone
    _phone = False


def phone_gone() -> None:
    """The phone left: CarPlay's last picture stays up, so go to the menu.

    Only if CarPlay is what's on screen, and it hasn't come straight back.
    """
    if _phone:
        return
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/state", timeout=2) as r:
            if json.load(r).get("source") != "Carplay":
                return
        req = urllib.request.Request(BRIDGE, data=json.dumps({"activate": "Home"}).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
        print("phone gone -> menu", flush=True)
    except (OSError, ValueError) as e:
        print(f"phone gone, bridge did not answer: {e}", flush=True)


# --------------------------------------------------------------- dashboard --
# Turn-by-turn, from the phone, as data rather than as pixels.
#
# The dongle will send it, but only if it is told the head unit wants it, and
# that is what the BoxSettings JSON is for. Found in LIVI's bundle; we had
# never sent this message at all, which is why nothing navigational ever
# arrived.
#
# What comes back, as MetaData (0x2A) with a leading uint32 sub-type:
#
#     200  DashboardInfo   JSON: NaviManeuverType, NaviTurnSide,
#                          NaviTurnAngle, NaviRoundaboutExitNumber,
#                          NaviRemainDistance, NaviDisplayDistanceE3,
#                          NaviDisplayDistanceUnit
#     201  DashboardImage  the rendered manoeuvre arrow
#
# This is what esp32-knob/nav-ui wants and what android/nav-watch.py has been
# approximating by scraping `dumpsys notification` out of Google Maps and
# guessing at icon ids. It is the phone's own guidance, whichever app is
# driving it.
META_MEDIA_DATA = 1
META_DASHBOARD_INFO = 200
META_DASHBOARD_IMAGE = 201


# ------------------------------------------------------------------- audio --
# AudioData: decode type (u32), volume (f32), audio type (u32), then PCM s16le.
# 13 bytes is a one-byte audio command and 16 a volume ramp: neither carries sound.
AUDIO_FORMATS = {1: (44100, 2), 2: (44100, 2), 3: (8000, 1), 4: (48000, 2),
                 5: (16000, 1), 6: (24000, 1), 7: (16000, 2)}


MUSIC_LEVEL, MUSIC_GAP_S = 300, 2.0


def tell_bridge(payload: dict) -> None:
    req = urllib.request.Request(BRIDGE, json.dumps(payload).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except OSError as e:
        print(f"bridge: {e}", file=sys.stderr, flush=True)


class AudioOut:
    """One pw-cat per format, so a navigation prompt mixes over music in PipeWire.

    Fed through a queue: the USB read loop also carries video and must never wait
    on the sound server.
    """

    def __init__(self):
        self.q = queue.Queue(maxsize=400)
        self.players = {}
        self.last_sound = 0.0
        threading.Thread(target=self.run, daemon=True).start()

    def music_check(self, fmt: int, pcm: bytes) -> None:
        """Music starting after a silence: the radio has to stop.

        Stereo only, as prompts and Siri come as mono and mix over the radio.
        Silence is checked in the samples, because the phone goes on sending
        audio for a while after it pauses.
        """
        if AUDIO_FORMATS.get(fmt, (0, 2))[1] != 2:
            return
        samples = struct.unpack(f"<{len(pcm) // 2}h", pcm[:len(pcm) // 2 * 2])
        if not samples or max(abs(x) for x in samples[::32]) < MUSIC_LEVEL:
            return
        now = time.monotonic()
        if now - self.last_sound > MUSIC_GAP_S:
            print("carplay music started", flush=True)
            threading.Thread(target=tell_bridge, args=({"carplay_music": True},), daemon=True).start()
        self.last_sound = now

    def feed(self, payload: bytes) -> None:
        if len(payload) <= 16:
            return
        try:
            self.q.put_nowait((struct.unpack("<I", payload[:4])[0], payload[12:]))
        except queue.Full:
            pass

    def player(self, fmt: int):
        p = self.players.get(fmt)
        if p and p.poll() is None:
            return p
        rate, ch = AUDIO_FORMATS.get(fmt, (44100, 2))
        print(f"audio: format {fmt}, {rate} Hz, {ch} ch", flush=True)
        p = subprocess.Popen(["pw-cat", "-p", "--raw", "--format=s16", f"--rate={rate}",
                              f"--channels={ch}", "--media-role=Music",
                              "--latency=100ms", "-"],
                             stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.players[fmt] = p
        return p

    def run(self) -> None:
        while True:
            fmt, pcm = self.q.get()
            self.music_check(fmt, pcm)
            try:
                p = self.player(fmt)
                p.stdin.write(pcm)
                p.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                print(f"audio: {e}", flush=True)
                self.players.pop(fmt, None)


AUDIO = None


def box_settings(name: str = "") -> bytes:
    """Ask the dongle for turn-by-turn, and change nothing else.

    LIVI sends a full configuration here -- sizes, names, autoConn, UseBTPhone,
    wifiChannel -- because it owns all of those. We do not: the dongle is
    already paired, associated and working, and this message is the only way to
    ask for navigation data.

    The first version of this copied LIVI's set wholesale, including
    wifiChannel 36. The dongle moved its access point, the phone was associated
    on the old channel, and the session never came back; it took a power cycle
    of the hub port to recover. So this sends the navigation fields and nothing
    else. It is JSON, and the dongle takes the keys it is given.

    Deliberately absent, and why:

        wifiChannel, wifiName, btName   the link that is already up
        autoConn, UseBTPhone            pairing behaviour already settled
        boxName, OemName                naming, which has its own config page
        androidAutoSizeW/H              the view area files already say this

    DashboardInfo is the flag that turns turn-by-turn on. GNSSCapability and
    gps say the car will take the phone's fix, which is what makes GnssData
    (0x29) arrive -- worth having on a car whose own receiver is one USB
    connector away from being unplugged.
    """
    cfg = {
        "DashboardInfo": 7,
        "GNSSCapability": 15,
        "gps": 1,
        "syncTime": int(time.time()),
    }
    # The Bluetooth name only: wifiName is the network the phone already knows.
    if name:
        cfg.update(btName=name, boxName=name)
    return frame(T_BOXSETTINGS, json.dumps(cfg).encode("ascii", "replace"))


BRIDGE = "http://127.0.0.1:8080/cmd"

# What the phone actually sends, observed with Sygic guiding to Berlin. The
# LIVI bundle names more fields than these -- NaviManeuverType, NaviTurnSide,
# NaviTurnAngle, NaviRoundaboutExitNumber, NaviRemainDistance -- and they are
# handled below, but they have not been seen yet: the car was stationary, and
# the phone sends the trip once and then only speaks when something changes.
#
#     {"NaviAPPName": "Sygic", "NaviDistanceToDestination": 273000,
#      "NaviTimeToDestination": 10662}
#     {"NaviDestinationName": "Berlin", ...}
#     {"NaviStatus": 0|1|2}
#
# Distances are metres, times seconds.
nav_state: dict = {}
# Two manoeuvres arrive per update: the next one, then the one after it, which
# Sygic labels as prose rather than a road ("Then in 170 m"). They are
# distinguished only by order -- there is no index in the payload -- so the
# first road name after a distance update is taken as the one being driven
# towards and the rest are logged and dropped. Without this the display shows
# the follow-up hint as if it were the next turn.
nav_want_primary = False

# Which way to point the arrow. Established on the road rather than from a
# document: the driver named the next turn as a left while the phone was
# sending NaviTurnSide 1 for it, so 1 is left and 2 is its opposite. Anything
# else points straight ahead, which is the honest answer for a value nobody has
# seen yet -- a roundabout, a motorway exit, or a manoeuvre this table does not
# know about.
#
# NaviTurnAngle and NaviManeuverType come with it and are finer grained: the
# display can already draw slight, sharp and u-turns, and the angle is probably
# what feeds them. Not guessed at here; the values are in the log and one more
# junction of each kind settles each one.
TURN_SIDE = {1: "left", 2: "right"}


def to_nav_display(navi: dict) -> None:
    """Forward the phone's guidance to the round display on the dash.

    Fields arrive a few at a time rather than as a complete picture -- one
    message carries the road, the next only a status -- so they are merged into
    what is known rather than replacing it. A display that blanked every time a
    partial update arrived would flicker its way across Germany.
    """
    global nav_want_primary

    if "NaviRemainDistance" in navi:
        nav_want_primary = True
    if "NaviRoadName" in navi:
        if not nav_want_primary:
            return                      # the follow-up hint, not the next turn
        nav_want_primary = False

    nav_state.update({k: v for k, v in navi.items() if v is not None})

    # Distance to the next turn if the phone has given one, otherwise distance
    # to the destination. Showing the trip total when a turn is 60 m away would
    # be wrong; showing nothing while cruising is worse than showing what is
    # left.
    metres = nav_state.get("NaviRemainDistance")
    if metres is None:
        metres = nav_state.get("NaviDistanceToDestination")
    if metres is None:
        return

    if metres >= 1000:
        dist, unit = int(round(metres / 100.0)), "km"    # tenths, as nav-ui wants
    else:
        dist, unit = int(metres), "m"

    out = {
        "street": nav_state.get("NaviRoadName")
                  or nav_state.get("NaviDestinationName") or "",
        "dist": dist,
        "unit": unit,
        "maneuver": TURN_SIDE.get(nav_state.get("NaviTurnSide"), "straight"),
    }
    try:
        req = urllib.request.Request(
            BRIDGE, data=json.dumps({"nav": out}).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass            # the display is a nicety; never hold up the video path


_media: dict = {}
_media_sent = None


def tell_media(media: dict | None) -> None:
    def post():
        req = urllib.request.Request(BRIDGE, json.dumps({"btmedia": media}).encode(), method="POST")
        try:
            urllib.request.urlopen(req, timeout=3).read()
        except OSError as e:
            print(f"bridge: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=post, daemon=True).start()


def media_data(body: bytes) -> None:
    """What the phone is playing, for the menu's Music page.

    Fields arrive a few at a time, and the play position every second, so they
    are merged and the bridge hears only when something it draws has changed.
    """
    global _media_sent
    try:
        info = json.loads(body.split(b"\x00", 1)[0].decode("utf-8", "replace"))
    except ValueError:
        return
    if not isinstance(info, dict):
        return
    _media.update(info)
    drawn = {"title": str(_media.get("MediaSongName") or ""),
             "artist": str(_media.get("MediaArtistName") or ""),
             "album": str(_media.get("MediaAlbumName") or ""),
             "status": "playing"}
    if drawn["title"] and drawn != _media_sent:
        _media_sent = drawn
        print(f"   playing: {drawn['artist']} - {drawn['title']}", flush=True)
        tell_media(drawn)


def dashboard(payload: bytes) -> None:
    """Print what the phone says about the next turn, and pass on its track."""
    if len(payload) < 4:
        return
    sub = struct.unpack("<I", payload[:4])[0]
    body = payload[4:]
    if sub == META_MEDIA_DATA:
        media_data(body)
        return
    if sub == META_DASHBOARD_IMAGE:
        print(f"   manoeuvre image: {len(body)} bytes", flush=True)
        return
    if sub != META_DASHBOARD_INFO:
        return
    text = body.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    if not text:
        return
    try:
        navi = json.loads(text)
        print(f"   navi: {json.dumps(navi, sort_keys=True)}", flush=True)
        if isinstance(navi, dict):
            to_nav_display(navi)
    except ValueError:
        print(f"   navi (unparsed): {text[:200]}", flush=True)


def gst_pipeline(width: int, height: int, crop: str | None,
                 fit: str | None = None) -> subprocess.Popen:
    """Decode H.264 from stdin onto a Wayland surface.

    fdsrc rather than a socket so back-pressure is the pipe's problem.
    sync=false because the dongle's timing is the source of truth; letting the
    sink re-time frames adds latency for no benefit on a screen you are
    steering by.
    """
    # No videoscale and no fullscreen on the sink. Both were tried to make the
    # picture fill the screen and produced a black screen with the pipeline
    # still running -- and they were aimed at the wrong thing anyway: the
    # decoded frame IS 800x480, confirmed by decoding a recording to a PNG.
    # The content sits in the top-left of it and the rest of the frame is black,
    # so the phone is drawing into a corner of a correctly sized buffer. That is
    # a question about what we tell the phone, not about how we scale it.
    # videoconvert before videocrop: the decoder's output format is not one
    # videocrop necessarily accepts, and a pipeline that cannot negotiate simply
    # produces no frames -- a black screen while the client happily reports
    # 24 fps, which is what happened the first time this was tried.
    cmd = ["gst-launch-1.0", "-q",
           "fdsrc", "fd=0", "do-timestamp=true", "!",
           # Slice threads only: frame threading holds frames back until later ones
           # arrive, and a still CarPlay screen sends almost none, so a knob step
           # showed up seconds late.
           "h264parse", "!", "avdec_h264", "thread-type=slice", "!", "videoconvert", "!"]

    # CarPlay draws a fixed region in the top-left of whatever frame size we
    # ask for -- the same absolute pixels at 800x480 and at 1280x720 -- and
    # leaves the rest black. Why is not understood. Cropping to what it
    # actually draws and scaling that to the output turns a picture in the
    # corner into a full screen, and costs nothing when the region is right.
    #
    # This is a workaround for behaviour we have not explained, not a fix. If
    # the drawn region ever changes size, this crops the wrong thing.
    if crop:
        cw, ch = (int(v) for v in crop.lower().split("x"))
        cmd += ["videocrop",
                f"right={max(0, width - cw)}", f"bottom={max(0, height - ch)}",
                "!"]
    # The compositor keeps a window's buffer shape, which is wrong when the output's
    # pixels are not square. DP-2 is 640x230 for COMAND's 320x230 panel: each pixel
    # is half as wide as it is tall, so 5:3 fitted by sway came out half as wide as
    # it should. --fit sizes the buffer for the shape it must have on the glass.
    # add-borders=false: by default videoscale keeps the shape and pads with black,
    # which is the same mistake one layer down.
    if fit:
        fw, fh = (int(v) for v in fit.lower().split("x"))
        cmd += ["videoscale", "add-borders=false", "!",
                f"video/x-raw,width={fw},height={fh},pixel-aspect-ratio=1/1", "!"]
    else:
        cmd += ["videoscale", "!"]
    cmd += ["videoconvert", "!", "waylandsink", "sync=false"]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE)


class TouchForwarder:
    """Panel touches to the phone.

    Read straight from evdev rather than from the Wayland surface, because the
    surface belongs to gst-launch and gst-launch has nowhere to send an event.
    evdev delivers to every reader, so sway still gets its copy and the menu
    keeps working.

    Gated on the CarPlay workspace being the one on screen: the same finger on
    the same glass means Sygic when Android is up, and forwarding regardless
    would have the phone reacting to taps meant for the map.
    """

    def __init__(self, args, send):
        self.args = args
        self.send = send            # callable(bytes) -> None
        self.active = False
        self.dev = None
        self.x = self.y = 0.0
        self.down = False

    def find_device(self):
        import evdev
        if self.args.touch not in ("auto", ""):
            return evdev.InputDevice(self.args.touch)
        for path in evdev.list_devices():
            d = evdev.InputDevice(path)
            caps = d.capabilities()
            axes = {c[0] for c in caps.get(evdev.ecodes.EV_ABS, [])}
            # A touchscreen reports absolute position. A mouse does not, and
            # the same USB device also exposes a pointer node that would look
            # plausible and never produce a coordinate.
            if evdev.ecodes.ABS_X in axes and evdev.ecodes.ABS_Y in axes:
                return d
        return None

    def watch_workspace(self):
        """Follow which source is on screen."""
        while True:
            code, out = run(["swaymsg", "-t", "get_workspaces"], timeout=4)
            if code == 0:
                try:
                    ws = json.loads(out)
                    self.active = any(w.get("focused") and
                                      str(w.get("name")) == str(self.args.workspace)
                                      for w in ws)
                except Exception:
                    pass
            time.sleep(0.5)

    def run(self):
        import evdev
        from evdev import ecodes

        dev = self.find_device()
        if dev is None:
            print("no touchscreen found; touch disabled", flush=True)
            return
        self.dev = dev
        ax = dev.absinfo(ecodes.ABS_X)
        ay = dev.absinfo(ecodes.ABS_Y)
        print(f"touch: {dev.name} ({dev.path}) "
              f"x {ax.min}-{ax.max} y {ay.min}-{ay.max}", flush=True)

        spanx = (ax.max - ax.min) or 1
        spany = (ay.max - ay.min) or 1

        for ev in dev.read_loop():
            if ev.type == ecodes.EV_ABS:
                if ev.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X):
                    self.x = (ev.value - ax.min) / spanx
                elif ev.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y):
                    self.y = (ev.value - ay.min) / spany
            elif ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
                self.down = bool(ev.value)
                if self.active:
                    self.send(touch_packet(
                        TOUCH_DOWN if self.down else TOUCH_UP, self.x, self.y))
            elif ev.type == ecodes.EV_SYN and self.down and self.active:
                self.send(touch_packet(TOUCH_MOVE, self.x, self.y))


# COMAND's knob while CarPlay is on screen: ui/comand-keys.py sends one word per
# datagram here, and it goes to the phone as the head unit's own buttons, the way
# a rotary controller drives CarPlay in a car that has one.
KNOB_PORT = 7340
KNOB = {
    "left": (CMD_BTN_LEFT,), "right": (CMD_BTN_RIGHT,),
    "select": (CMD_BTN_SELECT_DOWN, CMD_BTN_SELECT_UP), "back": (CMD_BTN_BACK,),
    # LIVI lists this under Android Auto. Asked for in case CarPlay honours it.
    "maps": (508,),
    "pause": (202,),
    "playpause": (203,), "next": (204,), "previous": (205,),
}
_knob_send = None
# Whether the dongle says a phone is attached (Plugged 0x02 / Unplugged 0x04). Without
# one "back" reaches nothing, which left RET dead on the empty CarPlay screen, so it
# goes to our menu instead.
_phone = False


def knob_port(send) -> None:
    """Point the knob at this session's dongle; the listener itself starts once and outlives sessions."""
    global _knob_send
    first = _knob_send is None
    _knob_send = send
    if not first:
        return

    def serve():
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", KNOB_PORT))
        while True:
            word = sock.recv(64).decode(errors="replace").strip()
            if word == "back" and not _phone:
                threading.Thread(target=host_ui, daemon=True).start()
                continue
            for i, cmd in enumerate(KNOB.get(word, ())):
                if i:
                    time.sleep(0.08)      # a press, not two commands at once
                _knob_send(frame(T_COMMAND, i32(cmd)))
    threading.Thread(target=serve, daemon=True).start()


def run_session(args) -> str:
    """One dongle session. Returns why it ended, so the caller can log it."""
    import usb.core
    import usb.util

    dev = None
    for pid in PRODUCTS:
        dev = usb.core.find(idVendor=VENDOR, idProduct=pid)
        if dev:
            break
    if not dev:
        return "no dongle"

    for intf in dev.get_active_configuration():
        n = intf.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(n):
                dev.detach_kernel_driver(n)
        except Exception:
            pass

    cfg = dev.get_active_configuration()
    intf = cfg[(0, 0)]
    out = usb.util.find_descriptor(
        intf, custom_match=lambda e:
        usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
        and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
    inp = usb.util.find_descriptor(
        intf, custom_match=lambda e:
        usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN
        and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
    if not (out and inp):
        return "no bulk endpoints"

    usb.util.claim_interface(dev, intf.bInterfaceNumber)
    print(f"claimed dongle {VENDOR:04x}:{dev.idProduct:04x}", flush=True)

    for label, pkt in (
        ("dpi",         sendfile("/tmp/screen_dpi", i32(args.dpi))),
        ("night_mode",  sendfile("/tmp/night_mode", i32(0))),
        ("hand_drive",  sendfile("/tmp/hand_drive_mode", i32(0))),
        ("charge_mode", sendfile("/tmp/charge_mode", i32(0))),
        # What the dongle calls itself: the name it advertises over Bluetooth
        # for wireless pairing, so it is what appears in the phone's device
        # list. The dongle appends " (D)".
        #
        # Sent only when --name is given. The dongle also has its own config
        # page, reachable from a phone on its WiFi AP, and that is where the
        # name can actually be changed -- box_name is currently ignored. Left
        # unsent by default so that if it ever does start taking effect it
        # cannot silently revert a name set there. Nothing here should quietly
        # undo a setting made somewhere else.
        *((("box_name", sendfile("/etc/box_name", args.name.encode())),)
          if args.name else ()),
        ("open",        frame(T_OPEN,
                              i32(args.width) + i32(args.height) + i32(args.fps) +
                              i32(5) + i32(49152) + i32(2) + i32(2))),
    ):
        out.write(pkt, timeout=3000)

    # After Open, which is the order LIVI uses. Sent before it, the dongle took
    # them and then showed nothing at all -- so it does act on them, it simply
    # will not have them at that point in the handshake.
    vw, vh = (int(v) for v in args.view.lower().split("x")) if args.view else (args.width, args.height)
    for label, pkt in (("view_area", view_area(vw, vh)),
                       ("safe_area", safe_area(vw, vh)),
                       *((("box_settings", box_settings(args.name)),)
                         if args.dashboard else ()),
                       *oem_icon(args.oem_label)):
        out.write(pkt, timeout=3000)

    # LIVI's dongle driver always sends this after the settings.
    out.write(frame(T_COMMAND, i32(CMD_AUDIO_TO_HOST)), timeout=3000)

    # Enable wireless. The dongle offers a wireless session only if the host
    # says it can take one; without this it stays wired-only however the phone
    # is configured.
    if not args.no_wireless:
        out.write(frame(T_COMMAND, i32(CMD_SUPPORT_WIFI)), timeout=3000)
        out.write(frame(T_COMMAND, i32(CMD_SUPPORT_WIFI_NEED_KO)), timeout=3000)
        print("wireless offered to the dongle", flush=True)

    # Start the decoder now rather than on the first frame. Starting it later
    # loses the frames that arrive while GStreamer comes up, and since the
    # stream opens with the only keyframe for a while, losing those leaves the
    # picture half-drawn until the next one -- which looked like a display bug
    # and was a race.
    gst = None if args.no_video else gst_pipeline(args.width, args.height,
                                                  args.crop, args.fit)
    rec = open(args.record, "wb") if args.record else None

    # Writing to the dongle from two threads needs a lock: a torn frame header
    # desynchronises the stream and the dongle stops responding.
    import threading
    wlock = threading.Lock()

    def send(pkt: bytes) -> None:
        with wlock:
            try:
                out.write(pkt, timeout=1000)
            except Exception:
                pass

    knob_port(send)

    if args.touch != "off":
        fwd = TouchForwarder(args, send)
        threading.Thread(target=fwd.watch_workspace, daemon=True).start()
        threading.Thread(target=fwd.run, daemon=True).start()

    last_beat = 0.0
    frames = 0
    started = time.time()

    # An H.264 decoder cannot start mid-stream: with no sequence parameter set
    # it has no geometry and no reference frame, so it never prerolls, never
    # negotiates and never creates a window -- while accepting everything
    # written to it. That is exactly what "no CarPlay displayed" looked like: a
    # healthy client counting frames into a pipeline still waiting to begin.
    # The stream opens with an SPS after a dongle reset, so this only bites on
    # reconnects.
    seen_sps = False

    # Anything queued before we claimed the interface belongs to a session that
    # no longer exists, and parsing it is what desynchronises the reads.
    drained = 0
    while True:
        try:
            junk = inp.read(READ_CHUNK, timeout=300)
        except Exception:
            break
        if not junk:
            break
        drained += len(junk)
    if drained:
        print(f"drained {drained} stale bytes", flush=True)

    buf = bytearray()

    def feed_video(payload: bytes) -> str | None:
        """Returns a reason to end the session, or None."""
        nonlocal frames, seen_sps
        h264 = payload[VIDEO_HDR:]
        if not h264:
            return None
        frames += 1
        if frames == 1:
            w, h = struct.unpack("<II", payload[:8])
            print(f"first video: {w}x{h}, {len(h264)} bytes", flush=True)

        if not seen_sps:
            # Annex B start codes are three bytes or four, and a mid-stream
            # parameter set commonly uses the short form -- looking only for
            # the long one finds nothing in a stream full of them.
            idx = -1
            for i in range(len(h264) - 3):
                if h264[i:i + 3] == b"\x00\x00\x01" and (h264[i + 3] & 0x1F) == 7:
                    idx = i - 1 if i and h264[i - 1] == 0 else i
                    break
            if idx < 0:
                return None
            h264 = h264[idx:]
            seen_sps = True
            print(f"stream starts at an SPS, {frames} frames in", flush=True)

        if rec:
            rec.write(h264)
        if gst and gst.stdin:
            try:
                gst.stdin.write(h264)
                gst.stdin.flush()
            except BrokenPipeError:
                return "decoder exited"
        if frames % 300 == 0:
            secs = max(0.001, time.time() - started)
            print(f"{frames} frames, {frames / secs:.1f} fps", flush=True)
        return None

    try:
        while True:
            now = time.time()
            if now - last_beat > HEARTBEAT_S:
                last_beat = now
                if not carplay_on():
                    return "switched off in Settings"
                with wlock:
                    try:
                        out.write(frame(T_HEARTBEAT), timeout=2000)
                    except Exception as e:
                        return f"heartbeat failed: {e}"

            try:
                chunk = inp.read(READ_CHUNK, timeout=2000)
            except Exception as e:
                if "timed out" in str(e).lower():
                    continue          # quiet link; the heartbeat keeps it up
                return f"read failed: {e}"
            if chunk:
                buf += bytes(chunk)

            while len(buf) >= HDR.size:
                magic, length, mtype, check = HDR.unpack(bytes(buf[:HDR.size]))
                if magic != MAGIC:
                    # Resynchronise on the next magic. One bad byte should cost
                    # one message, not the session.
                    nxt = buf.find(MAGIC_BYTES, 1)
                    if nxt < 0:
                        del buf[:max(0, len(buf) - 3)]
                        break
                    del buf[:nxt]
                    continue
                if len(buf) < HDR.size + length:
                    break
                payload = bytes(buf[HDR.size:HDR.size + length])
                del buf[:HDR.size + length]

                global _phone, last_video
                if mtype in (0x02, 0x04):
                    _phone = mtype == 0x02
                    last_video = time.monotonic()
                    threading.Thread(target=tell_bridge, args=({"carplay_conn": _phone},),
                                     daemon=True).start()
                    if not _phone:
                        threading.Timer(PHONE_GONE_S, phone_gone).start()

                if mtype == T_VIDEO:
                    last_video = time.monotonic()
                    if not _phone:
                        _phone = True
                        threading.Thread(target=tell_bridge, args=({"carplay_conn": True},),
                                         daemon=True).start()
                    why = feed_video(payload)
                    if why:
                        return why
                elif mtype == T_AUDIO:
                    global AUDIO
                    if AUDIO is None:
                        AUDIO = AudioOut()
                    AUDIO.feed(payload)
                elif mtype == T_COMMAND:
                    value = struct.unpack("<I", payload[:4])[0] if len(payload) >= 4 else -1
                    if value == CMD_REQUEST_HOST_UI:
                        threading.Thread(target=host_ui, daemon=True).start()
                elif mtype == T_METADATA:
                    # Turn-by-turn arrives here, behind a sub-type. Media
                    # metadata shares the message, so the sub-type is checked
                    # rather than the message being assumed navigational.
                    dashboard(payload)
                else:
                    nm = TYPES.get(mtype, f"0x{mtype:02x}")
                    if nm in ("BluetoothDeviceName", "WifiDeviceName"):
                        print(f"<- {nm}: "
                              f"{payload.rstrip(bytes(1)).decode('utf-8', 'replace')}",
                              flush=True)
                    elif nm == "BoxSettings" and args.dump_settings:
                        # To a file, not the log: the payload contains newlines,
                        # so a log line shows only the first fragment and looks
                        # like a short, complete object.
                        with open(args.dump_settings, "ab") as fh:
                            fh.write(payload + b"\n--\n")
                        print(f"BoxSettings: {len(payload)} bytes -> "
                              f"{args.dump_settings}", flush=True)
                    elif nm == "BluetoothPairedList":
                        print(f"paired: {payload.decode('ascii', 'replace')}", flush=True)
                    else:
                        print(f"<- {nm} ({len(payload)} bytes)", flush=True)
    finally:
        if gst and gst.stdin:
            try:
                gst.stdin.close()
                gst.wait(timeout=3)
            except Exception:
                gst.kill()
        if rec:
            rec.close()
        try:
            usb.util.release_interface(dev, intf.bInterfaceNumber)
            usb.util.dispose_resources(dev)
        except Exception:
            pass


CMD_WIFI_CONNECT = 1002
SETTINGS = os.path.expanduser("~/.config/comand/settings.json")


def carplay_on() -> bool:
    """Settings > CarPlay. Off, no session is opened, so the dongle never calls
    the phone in and it stays on plain Bluetooth. Read from the bridge's file
    rather than asked for: it is there before the bridge is and after it dies."""
    try:
        with open(SETTINGS) as fh:
            return bool(json.load(fh).get("carplay", True))
    except (OSError, ValueError):
        return True


def report_phone() -> None:
    """Keep the bridge told, as it forgets when it restarts, and look after the link.

    With no phone, ask the dongle to connect to the last one, as LIVI does: it
    doesn't always do so by itself. No frame requests to test for a phone that has
    gone: each one redrew CarPlay and threw its highlight back to Now Playing.
    """
    while True:
        time.sleep(15)
        if _phone and time.monotonic() - last_video > PHONE_SILENT_S:
            print("no picture from the phone: treating it as gone", flush=True)
            _phone_lost()
        tell_bridge({"carplay_conn": _phone})
        send = _knob_send
        if _phone or not send or not carplay_on():
            continue
        try:
            send(frame(T_COMMAND, i32(CMD_WIFI_CONNECT)))
        except Exception as e:  # noqa: BLE001 - the session loop deals with a lost dongle
            print(f"report: {e}", flush=True)


def main() -> int:
    global _media_sent
    threading.Thread(target=report_phone, daemon=True).start()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # Off by default. The message is what asks the dongle for turn-by-turn,
    # and it is also where the wireless link is configured -- the first attempt
    # set wifiChannel to 36 and the phone, associated on the old channel, never
    # came back. Worth having, not worth risking a working CarPlay session on
    # by default until the field that did that is pinned down.
    ap.add_argument("--dashboard", action="store_true",
                    help="ask the dongle for turn-by-turn (sends BoxSettings)")
    ap.add_argument("--width", type=int, default=800)
    ap.add_argument("--height", type=int, default=480)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--dpi", type=int, default=160)
    ap.add_argument("--oem-label", default="COMAND",
                    help="the name under the car icon in CarPlay's dock")
    ap.add_argument("--name", default="",
                    help="set the dongle's advertised name; empty leaves "
                         "whatever the dongle is configured with")
    ap.add_argument("--no-video", action="store_true", help="protocol only")
    ap.add_argument("--record", help="also write the raw H.264 here")
    ap.add_argument("--once", action="store_true", help="do not reconnect")
    # Off by default. Cropping to the drawn region and scaling it up is the
    # obvious way to fill the screen, and three attempts at it all produced a
    # black screen while the client reported 24 fps -- so the picture that
    # works is the uncropped one, and this stays available but unused until
    # somebody works out why adding elements to this pipeline kills it.
    ap.add_argument("--crop", default="",
                    help="crop the top-left WxH CarPlay draws, e.g. 320x230; "
                         "known to produce a black screen, see the note above")
    ap.add_argument("--view", default="",
                    help="have the phone lay out inside this top-left WxH of the frame, "
                         "for a panel whose shape no accepted frame size matches; "
                         "use with --crop of the same size")
    ap.add_argument("--fit", default="",
                    help="scale the picture to this buffer size, for an output "
                         "with non-square pixels, e.g. 640x192 on COMAND's DP-2")
    ap.add_argument("--dump-settings", metavar="FILE",
                    help="append the dongle's BoxSettings payloads to FILE")
    ap.add_argument("--no-wireless", action="store_true",
                    help="do not offer a wireless session")
    ap.add_argument("--touch", default="auto",
                    help="touchscreen event device, 'auto', or 'off'")
    ap.add_argument("--workspace", default="2",
                    help="forward touches only while this sway workspace is focused")
    args = ap.parse_args()
    adopt_compositor()

    while True:
        # Wait for a compositor before opening a session: without one the
        # decoder cannot start, and opening a dongle session whose pipeline
        # then dies churns the dongle for nothing.
        if not args.no_video and not adopt_compositor(quiet=True):
            print("waiting for a compositor", flush=True)
            while not adopt_compositor(quiet=True):
                time.sleep(2)
            adopt_compositor()

        if not carplay_on():
            print("CarPlay switched off: no session", flush=True)
            while not carplay_on():
                time.sleep(2)
            print("CarPlay switched on", flush=True)

        try:
            why = run_session(args)
        except KeyboardInterrupt:
            print("\nstopped")
            return 0
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
        print(f"session ended: {why}", flush=True)
        _phone_lost()
        if _media_sent:
            _media.clear()
            _media_sent = None
            tell_media(None)
        if args.once:
            return 1
        # The dongle re-enumerates after a reset; give it time to come back
        # rather than spinning on a device node that is not there yet.
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
