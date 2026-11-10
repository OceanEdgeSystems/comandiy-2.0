#!/usr/bin/env python3
"""Plex and YouTube on COMAND: the menu's browser, an mpv player, and the moving-car rule.

A local web service for main-menu.html on 127.0.0.1:8092, and UDP 7343 for COMAND's
keys. The Plex token is in /etc/default/comand-plex. Phones send YouTube links to
port 8093 on this laptop's Tailscale address, reachable only from the tailnet.

The picture stops while the car moves, because COMAND's screen is in the driver's
view; the sound carries on. Speed comes from the car's CAN and from GPS, and either
one saying "moving" is enough. It comes back after a few seconds at a standstill.
"""
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("PLEX_TOKEN", "")
URLS = os.environ.get("PLEX_URLS", "").split()
BRIDGE = "http://127.0.0.1:8080"
IPC = "/tmp/comand-mpv.sock"
CLIENT = "comand-head-unit"
YT_DLP = "/opt/comand-voice/venv/bin/yt-dlp"
YT_COOKIES = "/var/lib/comand/youtube-cookies.txt"
YT_FORMAT = "best[height<=480]/bv*[height<=480]+ba/best"
YT_FEEDS = {"subs": "https://www.youtube.com/feed/subscriptions", "later": "https://www.youtube.com/playlist?list=WL",
            "playlists": "https://www.youtube.com/feed/playlists"}
# Live TV: an optional IPTV server (line-up, logos, guide, restreamer) at IPTV_URL in /etc/default/comand.
# Without it the Live TV menu says it is not reachable. COMAND watches as its own viewer, zone:comand.
IPTV = os.environ.get("IPTV_URL", "")
IPTV_ZONE = "comand"
CAN_MOVING_KMH = 3.0
GPS_MOVING_MS = 2.5        # GPS wanders when still, so it needs more than CAN does
STILL_S = 3.0
VAR_DIR = os.path.dirname(YT_COOKIES)         # /var/lib/comand
SETTINGS_FILE = os.path.join(VAR_DIR, "video-settings.json")

playing: dict = {}
proc = None
lock = threading.Lock()
gps_speed = 0.0
test_until, test_speed = 0.0, 0.0
moving = False
sent_away = False           # the picture was taken away by the moving rule
allow_in_motion = False     # user setting: keep the picture while driving (default off = safe)


def server() -> str:
    return URLS[0]


def plex(path: str, method: str = "GET"):
    req = urllib.request.Request(server() + path, method=method, headers={
        "X-Plex-Token": TOKEN, "Accept": "application/json", "X-Plex-Client-Identifier": CLIENT,
        "X-Plex-Product": "COMAND", "X-Plex-Platform": "Linux", "X-Plex-Device-Name": "COMAND"})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
    return json.loads(body).get("MediaContainer", {}) if body[:1] == b"{" else {}


def row(m: dict) -> dict:
    kind = m.get("type")
    if kind == "episode":
        name = f"{m.get('grandparentTitle', '')} S{m.get('parentIndex', 0)}E{m.get('index', 0)} {m.get('title', '')}"
    elif kind == "season":
        name = m.get("title", "")
    else:
        name = m.get("title", "") + (f" ({m['year']})" if m.get("year") and kind == "movie" else "")
    # Cover: the poster (a show's for its episodes, so a list of episodes reads as one series).
    thumb = (m.get("grandparentThumb") if kind == "episode" else None) or m.get("thumb") or m.get("parentThumb") or ""
    return {"name": name, "key": m.get("ratingKey"), "type": kind,
            "offset": m.get("viewOffset", 0), "duration": m.get("duration", 0), "thumb": thumb}


def search(q: str) -> list:
    """Plex's own search over every library: movies, shows, seasons and episodes, best first."""
    out = []
    for hub in plex("/hubs/search?" + urllib.parse.urlencode({"query": q, "limit": 20})).get("Hub", []):
        out += [row(m) for m in hub.get("Metadata", []) or [] if m.get("type") in ("movie", "show", "season", "episode")]
    return out[:60]


def image(path: str, w: int) -> bytes:
    """A Plex poster, resized by Plex, for pages that must not see the token."""
    if not re.fullmatch(r"/library/[\w/:.-]+", path):
        raise ValueError("not a Plex image")
    w = max(60, min(600, w))
    q = urllib.parse.urlencode({"width": w, "height": w * 3 // 2, "minSize": 1, "upscale": 1, "url": path})
    req = urllib.request.Request(f"{server()}/photo/:/transcode?{q}", headers={"X-Plex-Token": TOKEN, "Accept": "image/jpeg"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def ytimg(vid: str) -> bytes:
    """A YouTube thumbnail, fetched here so the page needs nothing but this box."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
        raise ValueError("not a video id")
    with urllib.request.urlopen(f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg", timeout=10) as r:
        return r.read()


def browse(what: str, key: str, offset: int) -> list:
    page = f"X-Plex-Container-Start={offset}&X-Plex-Container-Size=200"
    if what == "continue":
        path = "/library/onDeck?" + page
    elif what == "recent":
        path = "/library/recentlyAdded?" + page
    elif what in ("movie", "show"):
        section = next(d["key"] for d in plex("/library/sections").get("Directory", []) if d["type"] == what)
        path = f"/library/sections/{section}/all?sort=titleSort&{page}"
    else:
        path = f"/library/metadata/{urllib.parse.quote(key)}/children?{page}"
    return [row(m) for m in plex(path).get("Metadata", []) if m.get("type") in ("movie", "show", "season", "episode")]


def yt_signed_in(path: str = "") -> bool:
    """The jar still holds a Google session: both of the cookies YouTube's login sets."""
    try:
        with open(path or YT_COOKIES) as f:
            names = {line.split("\t")[5] for line in f if line.count("\t") >= 6 and not line.startswith("# ")}
    except OSError:
        return False
    return "LOGIN_INFO" in names and bool(names & {"SID", "__Secure-1PSID", "__Secure-3PSID"})


def yt_dlp_signed_in(cmd: list) -> subprocess.CompletedProcess:
    """Run yt-dlp on a copy of the jar. It writes back what YouTube sends, and once YouTube
    ends the session that is the removal of the sign-in, so keep only a jar still signed in."""
    fd, work = tempfile.mkstemp(dir=os.path.dirname(YT_COOKIES), prefix=".yt-cookies-")
    try:
        with os.fdopen(fd, "w") as out, open(YT_COOKIES) as jar:
            out.write(jar.read())
        r = subprocess.run(cmd + ["--cookies", work], capture_output=True, text=True, timeout=60)
        if yt_signed_in(work):
            os.replace(work, YT_COOKIES)
        return r
    finally:
        if os.path.exists(work):
            os.unlink(work)


def youtube(what: str, url: str, q: str = "") -> list:
    """A feed or playlist, as rows. Subscriptions and the like need the sign-in. what="search" with q searches
    all of YouTube (signed in when the jar is, so the results are the account's)."""
    target = YT_FEEDS.get(what) or url
    signed_in = yt_signed_in()
    if what in YT_FEEDS and not signed_in:
        raise RuntimeError("sign in needed")
    if what == "search":
        q = " ".join(q.split())[:100]
        if not q:
            raise ValueError("nothing to search for")
        target = f"ytsearch30:{q}"
    elif not target.startswith("https://www.youtube.com/"):
        raise ValueError("not a YouTube address")
    cmd = [YT_DLP, "--flat-playlist", "-J", "--playlist-end", "100", "--no-warnings", target]
    r = yt_dlp_signed_in(cmd) if signed_in else subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    # A session YouTube has ended fails like this, however intact the jar looks.
    if what in YT_FEEDS and r.returncode and re.search(r"HTTP Error 401|playlist does not exist", r.stderr):
        raise RuntimeError("sign in needed")
    if r.returncode:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "yt-dlp failed")
    rows = []
    for e in json.loads(r.stdout).get("entries") or []:
        link = e.get("url") or e.get("webpage_url") or ""
        is_list = "list=" in link and "watch?" not in link
        by = e.get("channel") or e.get("uploader") or ""
        if by.startswith("View full"):         # YouTube's button, not a channel
            by = ""
        if what == "search" and link and not link.startswith("http"):
            link = f"https://www.youtube.com/watch?v={link}"
        vid = e.get("id") if not is_list and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(e.get("id") or "")) else ""
        thumbs = e.get("thumbnails") or []
        rows.append({"name": (e.get("title") or "") + (f" \u2014 {by}" if by else ""), "url": link,
                     "type": "ytlist" if is_list else "youtube", "vid": vid,
                     # a playlist's own cover (a youtube.com/i.ytimg.com address) when there is no video id
                     "thumb": "" if vid else next((t.get("url", "") for t in reversed(thumbs) if "ytimg.com/" in t.get("url", "")), "")})
    return rows


def mpv(*command):
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(IPC)
            s.sendall(json.dumps({"command": list(command), "request_id": 1}).encode() + b"\n")
            for line in s.makefile():
                reply = json.loads(line)
                if reply.get("request_id") == 1:
                    return reply.get("data")
    except (OSError, ValueError):
        return None
    return None


def tell(payload: dict) -> None:
    req = urllib.request.Request(BRIDGE + "/cmd", json.dumps(payload).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=3).read()
    except OSError as e:
        print(f"bridge: {e}", flush=True)


def iptv(path: str, body: dict | None = None, timeout: float = 15):
    if not IPTV:
        raise RuntimeError("Live TV is not set up (IPTV_URL in /etc/default/comand)")
    req = urllib.request.Request(IPTV + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def tv_lineup() -> dict:
    """the IPTV server's guest line-up: {groups: [{name, channels: [{id, name, logo, health}]}], hidden, playing}."""
    return json.loads(iptv("/api/guest/iptv/lineup"))


def tv_logo(cid: str) -> bytes:
    if not re.fullmatch(r"[\w-]{1,64}", cid):
        raise ValueError("not a channel id")
    return iptv(f"/api/guest/iptv/logo/{cid}")


def tv_epg() -> dict:
    """Now and next for every channel the IPTV server has a guide for.

    One call fetches the lot: {programs: {<channelId>: {current: {title, start,
    stop, desc, category}, next: {...}}}}, start/stop in ms since the epoch. The
    channel key is the same id used by the line-up, logos and activate, so the
    guest page can hang now/next straight onto the channel it already drew.
    """
    return json.loads(iptv("/api/guest/iptv/epg?action=now"))


def start_tv(cid: str, name: str) -> None:
    """Ask the IPTV server's hub for the channel (one provider connection, shared by every zone on it) and play its HLS."""
    if not re.fullmatch(r"[\w-]{1,64}", cid):
        raise ValueError("not a channel id")
    stop(next_follows=True, keep_tv=True)     # activate on the same viewer swaps the channel; no release race
    r = json.loads(iptv("/api/guest/iptv/activate", {"action": "activate", "zoneId": IPTV_ZONE, "channelId": cid}, 30))
    if r.get("busy"):
        who = ", ".join(s.get("channelName", "") for s in r.get("sessions") or [] if s.get("channelName"))
        raise RuntimeError("all TV connections are in use" + (f" ({who})" if who else ""))
    if not r.get("streamUrl"):
        raise RuntimeError(r.get("error") or "channel not available")
    launch(IPTV + r["streamUrl"], {"name": name, "tv": cid}, ["--cache=yes", "--demuxer-max-bytes=50MiB"])


def tv_release() -> None:
    try:
        iptv("/api/guest/iptv/activate", {"action": "deactivate", "zoneId": IPTV_ZONE}, 10)
    except OSError as e:
        print(f"tv release: {e}", flush=True)


def stop(next_follows: bool = False, keep_tv: bool = False) -> None:
    global proc, playing, sent_away
    with lock:
        p, proc = proc, None
        item, playing = playing, {}
    if p and p.poll() is None:
        report(item, "stopped")
        p.terminate()
    if item.get("tv") and not keep_tv:   # free the provider connection for the other zones (not for a channel change)
        threading.Thread(target=tv_release, daemon=True).start()
    sent_away = False
    # With another video on its way the knob goes straight to it, not via the music.
    if item.get("youtube") and not next_follows:
        tell({"video_playing": None})


def announce(item: dict) -> None:
    """Tell the bridge which YouTube video is on, for the knob. Until mpv has loaded it,
    its title is only the address, so the name from the menu stands in."""
    title, paused = mpv("get_property", "media-title"), mpv("get_property", "pause")
    if not title or "watch?v=" in title:
        title = item.get("name") or "YouTube"
    if playing is item:
        tell({"video_playing": {"title": title, "playing": not paused}})


def report(item: dict, state: str) -> None:
    """Tell Plex where playback is, so it carries on from there on any device."""
    if not item or item.get("youtube") or item.get("tv"):
        return
    pos = mpv("get_property", "time-pos")
    if pos is None:
        return
    q = urllib.parse.urlencode({"ratingKey": item["key"], "key": f"/library/metadata/{item['key']}",
                                "state": state, "time": int(pos * 1000), "duration": item.get("duration", 0)})
    try:
        plex("/:/timeline?" + q)
    except OSError as e:
        print(f"timeline: {e}", flush=True)


def launch(url: str, item: dict, extra: list) -> None:
    global proc, playing, sent_away
    cmd = ["mpv", "--no-config", "--really-quiet", "--fs", "--force-window=yes", "--keep-open=no",
           "--no-osc", "--no-input-default-bindings", "--monitorpixelaspect=0.5", "--ao=pulse",
           f"--input-ipc-server={IPC}", "--wayland-app-id=comand-video", "--hwdec=vaapi,no", *extra, url]
    with lock:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        playing = item
    sent_away = False
    tell({"ma_music": True, "from_video": True})
    if moving and not allow_in_motion:
        picture(False)
    else:
        tell({"activate": "Video"})
    print(f"playing {item.get('name')}", flush=True)


def youtube_link(text: str) -> str | None:
    """The video in whatever a phone shares: a bare link, or text with one in it."""
    m = re.search(r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?\S*?v=|shorts/|live/)"
                  r"|youtu\.be/)([\w-]{6,20})", text or "")
    return f"https://www.youtube.com/watch?v={m[1]}" if m else None


def start_youtube(url: str, name: str) -> None:
    url = youtube_link(url)
    if not url:
        raise ValueError("not a YouTube video")
    stop(next_follows=True)
    # Not signed in: the sign-in is for the lists. With it, YouTube asks for a challenge
    # this Debian's Node is too old to solve, and offers no streams at all.
    launch(url, {"name": name, "youtube": True},
           [f"--script-opts=ytdl_hook-ytdl_path={YT_DLP}", f"--ytdl-format={YT_FORMAT}"])


def start(key: str, offset_ms: int, duration: int, name: str) -> None:
    stop()
    q = urllib.parse.urlencode({
        "path": f"/library/metadata/{key}", "mediaIndex": 0, "partIndex": 0, "protocol": "hls",
        "fastSeek": 1, "directPlay": 0, "directStream": 0, "videoResolution": "640x360",
        "maxVideoBitrate": 1500, "videoQuality": 60, "subtitleSize": 100, "audioBoost": 100,
        "session": uuid.uuid4().hex, "offset": int(offset_ms / 1000),
        "X-Plex-Client-Identifier": CLIENT, "X-Plex-Product": "COMAND", "X-Plex-Platform": "Chrome"})
    url = f"{server()}/video/:/transcode/universal/start.m3u8?{q}"
    launch(url, {"key": key, "duration": duration, "name": name},
           [f"--http-header-fields=X-Plex-Token: {TOKEN},X-Plex-Client-Identifier: {CLIENT}"])


def load_settings() -> None:
    global allow_in_motion
    try:
        with open(SETTINGS_FILE) as f:
            allow_in_motion = bool(json.load(f).get("allow_in_motion", False))
    except (OSError, ValueError):
        allow_in_motion = False
    print(f"video in motion: {'allowed' if allow_in_motion else 'blanked while driving'}", flush=True)


def save_settings() -> None:
    try:
        os.makedirs(VAR_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"allow_in_motion": allow_in_motion}, f)
    except OSError as e:
        print(f"save_settings: {e}", flush=True)


def set_in_motion(on: bool) -> None:
    """Flip the 'keep the picture while driving' setting and apply it right now."""
    global allow_in_motion
    allow_in_motion = bool(on)
    save_settings()
    if playing and moving:
        if allow_in_motion and sent_away:
            picture(True)           # car is moving and we just allowed it: bring the picture back
        elif not allow_in_motion and not sent_away:
            picture(False)          # car is moving and we just forbade it: take the picture away


def picture(on: bool) -> None:
    """The moving rule: no picture, sound still playing, the menu on the screen."""
    global sent_away
    mpv("set_property", "vid", "auto" if on else "no")
    if on:
        tell({"activate": "Video"})
        tell({"toast": {"icon": "bluetooth", "text": "Video back", "ms": 1500}})
    else:
        tell({"activate": "Home"})
        tell({"toast": {"icon": "bluetooth", "text": "Sound only while driving", "ms": 3000}})
    sent_away = not on


def watch_gps() -> None:
    global gps_speed
    while True:
        try:
            with socket.create_connection(("127.0.0.1", 2947), 5) as s:
                s.sendall(b'?WATCH={"enable":true,"json":true}\n')
                for line in s.makefile():
                    if '"TPV"' in line:
                        d = json.loads(line)
                        gps_speed = float(d.get("speed") or 0) if d.get("mode", 0) >= 2 else 0.0
        except (OSError, ValueError):
            gps_speed = 0.0
        time.sleep(5)


def car_speed() -> float:
    try:
        with urllib.request.urlopen(BRIDGE + "/state", timeout=2) as r:
            v = json.load(r).get("vehicle") or {}
        return float(v.get("speed") or 0) if v.get("can") else 0.0
    except (OSError, ValueError, TypeError):
        return 0.0


def is_moving() -> bool:
    if time.time() < test_until and test_speed > 0:     # can only ever say "moving"
        return True
    return car_speed() >= CAN_MOVING_KMH or gps_speed >= GPS_MOVING_MS


def watch_motion() -> None:
    global moving
    still_since, ticks = None, 0
    while True:
        time.sleep(0.5)
        now_moving = is_moving()
        if now_moving:
            still_since = None
            if not moving:
                moving = True
                if playing and not allow_in_motion:
                    picture(False)
        elif moving:
            still_since = still_since or time.monotonic()
            if time.monotonic() - still_since >= STILL_S:
                moving = False
                if playing and sent_away:
                    picture(True)
        ticks += 1
        if playing.get("youtube") and ticks % 2 == 0:
            announce(playing)
        if playing and ticks % 20 == 0:
            with lock:
                ended = proc is not None and proc.poll() is not None
            if ended:
                stop()
                tell({"activate": "Home"})
            else:
                paused = mpv("get_property", "pause")
                report(playing, "paused" if paused else "playing")


def keys() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 7343))
    while True:
        control(sock.recv(64).decode(errors="replace").strip())


def control(word: str) -> None:
    if word == "toggle":
        mpv("cycle", "pause")
        if mpv("get_property", "pause") is False:   # back on: one music source at a time
            tell({"ma_music": True, "from_video": True})
    elif word in ("pause", "play"):
        mpv("set_property", "pause", word == "pause")
    elif word in ("forward", "back30"):
        mpv("seek", 30 if word == "forward" else -30)
    elif word == "stop":
        stop()
        tell({"activate": "Home"})


class SendHandler(BaseHTTPRequestHandler):
    """A link from a phone: the body is the shared text, or {"url": ...}."""

    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 8192)).decode(errors="replace")
        try:
            text = json.loads(raw).get("url", "")
        except (ValueError, AttributeError):
            text = raw
        link = youtube_link(text)
        self.send_response(200 if link else 400)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Playing on COMAND\n" if link else b"No YouTube video in that\n")
        if link:
            print(f"from a phone: {link}", flush=True)
            threading.Thread(target=start_youtube, args=(link, "YouTube"), daemon=True).start()


def serve_phones() -> None:
    """On the Tailscale address only, so only the tailnet can reach it."""
    while True:
        try:
            ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10).stdout.split()[0]
            print(f"phones: {ip}:8093", flush=True)
            ThreadingHTTPServer((ip, 8093), SendHandler).serve_forever()
        except (OSError, IndexError, subprocess.SubprocessError) as e:
            print(f"phones: {e}", flush=True)
            time.sleep(30)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.reply(204, {})

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path == "/list":
                self.reply(200, browse(q.get("what", ""), q.get("key", ""), int(q.get("offset", 0))))
            elif url.path == "/youtube":
                self.reply(200, youtube(q.get("what", ""), q.get("url", ""), q.get("q", "")))
            elif url.path == "/search":
                self.reply(200, search(q.get("q", "")))
            elif url.path == "/tv":
                self.reply(200, tv_lineup())
            elif url.path == "/epg":
                self.reply(200, tv_epg())
            elif url.path in ("/image", "/ytimg", "/tvlogo"):
                body = (image(q.get("path", ""), int(q.get("w", 240))) if url.path == "/image"
                        else ytimg(q.get("id", "")) if url.path == "/ytimg" else tv_logo(q.get("id", "")))
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/now":
                self.reply(200, {"playing": playing.get("name", ""), "moving": moving, "tv": bool(playing.get("tv")),
                                 "channel": playing.get("tv") or "",   # channel id, so now/next can be looked up
                                 "in_motion": allow_in_motion,
                                 "paused": bool(mpv("get_property", "pause")) if playing else False,
                                 "pos": mpv("get_property", "time-pos") if playing else None})
            elif url.path == "/settings":
                self.reply(200, {"allow_in_motion": allow_in_motion})
            else:
                self.reply(404, {})
        except Exception as e:  # noqa: BLE001 - the page says so, and the car goes on
            print(f"{self.path}: {e}", flush=True)
            self.reply(502, {"error": str(e)[:120]})

    def do_POST(self):
        global test_until, test_speed
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        try:
            if self.path == "/play" and body.get("url") and "list=" in body["url"] and "v=" not in body["url"]:
                raise ValueError("a playlist, not a video")
            if self.path == "/play" and body.get("tv"):
                start_tv(str(body["tv"]), body.get("name", ""))
            elif self.path == "/play" and body.get("url"):
                start_youtube(body["url"], body.get("name", ""))
            elif self.path == "/play":
                start(str(body["key"]), int(body.get("offset") or 0), int(body.get("duration") or 0), body.get("name", ""))
            elif self.path == "/ctl":
                control(body.get("action", ""))
            elif self.path == "/test-speed":      # for testing the moving rule on a parked car
                test_until, test_speed = time.time() + float(body.get("for", 20)), float(body.get("speed", 0))
            elif self.path == "/settings":
                set_in_motion(bool(body.get("allow_in_motion")))
            self.reply(200, {"ok": True})
        except Exception as e:  # noqa: BLE001
            print(f"{self.path}: {e}", flush=True)
            self.reply(502, {"error": str(e)[:120]})


if __name__ == "__main__":
    load_settings()
    for target in (watch_gps, watch_motion, keys, serve_phones):
        threading.Thread(target=target, daemon=True).start()
    print("listening on 127.0.0.1:8092" + ("" if TOKEN else ", no token yet"), flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8092), Handler).serve_forever()
