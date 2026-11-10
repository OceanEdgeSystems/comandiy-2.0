#!/usr/bin/env python3
"""COMAND's menu to Music Assistant: what is playing, the library, and playback.

A small local web service for main-menu.html, on 127.0.0.1:8091. Music Assistant
is on the car's network, or over Tailscale from anywhere else. It needs a token,
made in Music Assistant, in /etc/default/comand-ma as MA_TOKEN=...
"""
import json
import os
import socket
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOSTS = os.environ.get("HA_HOSTS", "").split()
TOKEN = os.environ.get("MA_TOKEN", "")
QUEUE = "02:00:00:c0:4d:01"       # COMAND's player, car/squeezelite-comand.sh
LIMIT = 200
KINDS = {"albums": "music/albums/library_items", "artists": "music/artists/library_items",
         "playlists": "music/playlists/library_items", "radios": "music/radios/library_items",
         "tracks": "music/tracks/library_items"}


def host() -> str:
    for h in HOSTS:
        try:
            with socket.create_connection((h, 8095), timeout=2):
                return h
        except OSError:
            continue
    raise OSError("Music Assistant not reachable")


def api(command: str, **args):
    req = urllib.request.Request(f"http://{host()}:8095/api",
                                 json.dumps({"command": command, "args": args}).encode(),
                                 {"Content-Type": "application/json",
                                  "Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def art(item: dict | None) -> str:
    item = item or {}
    image = item.get("image") or (item.get("media_item") or {}).get("image") or {}
    if not image.get("proxy_id"):
        # Library items keep their pictures in metadata.images (a thumb first), not in image.
        pics = ((item.get("metadata") or (item.get("media_item") or {}).get("metadata") or {}).get("images") or [])
        image = next((p for p in pics if p.get("type") == "thumb" and p.get("proxy_id")), None) or \
            next((p for p in pics if p.get("proxy_id")), {})
    return "/image?" + urllib.parse.urlencode({"id": image["proxy_id"]}) if image.get("proxy_id") else ""


def row(item: dict) -> dict:
    artists = item.get("artists") or []
    return {"name": item.get("name", ""), "uri": item.get("uri", ""), "id": item.get("item_id"),
            "provider": item.get("provider"), "type": item.get("media_type"),
            "by": ", ".join(a.get("name", "") for a in artists), "art": art(item)}


ORDERS = ("sort_name", "last_played_desc", "timestamp_added_desc")


def search(q: str) -> list:
    """Music Assistant's search over the library and its providers: artists, albums, playlists, then tracks."""
    q = " ".join(q.split())[:100]
    if not q:
        return []
    r = api("music/search", search_query=q, media_types=["artist", "album", "playlist", "track"], limit=12) or {}
    return [row(x) for kind in ("artists", "albums", "playlists", "tracks") for x in r.get(kind) or []][:LIMIT]


def now() -> dict:
    q = next((x for x in api("player_queues/all") if x.get("queue_id") == QUEUE), None)
    if not q:
        return {"state": "off"}
    cur = q.get("current_item") or {}
    media = cur.get("media_item") or {}
    artists = media.get("artists") or []
    return {"state": q.get("state"), "title": media.get("name") or cur.get("name") or "",
            "artist": ", ".join(a.get("name", "") for a in artists),
            "album": (media.get("album") or {}).get("name", ""), "art": art(cur),
            "elapsed": q.get("elapsed_time"), "duration": cur.get("duration")}


def kids(kind: str, item_id: str, provider: str) -> list:
    if kind == "album":
        items = api("music/albums/album_tracks", item_id=item_id, provider_instance_id_or_domain=provider)
    elif kind == "artist":
        items = api("music/artists/artist_albums", item_id=item_id, provider_instance_id_or_domain=provider)
    else:
        items = api("music/playlists/playlist_tracks", item_id=item_id, provider_instance_id_or_domain=provider)
    return [row(x) for x in items][:LIMIT]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, code: int, body: bytes, kind: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.reply(204, b"")

    def answer(self, fn) -> None:
        if not TOKEN:
            self.reply(503, b'{"error":"no token"}')
            return
        try:
            self.reply(200, json.dumps(fn()).encode())
        except Exception as e:  # noqa: BLE001 - the page shows it, and the car goes on
            print(f"{self.path}: {e}", flush=True)
            self.reply(502, json.dumps({"error": str(e)[:120]}).encode())

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/now":
            self.answer(now)
        elif url.path == "/list" and q.get("kind") in KINDS:
            order = q.get("order") if q.get("order") in ORDERS else "sort_name"
            self.answer(lambda: [row(x) for x in api(KINDS[q["kind"]], limit=LIMIT,
                                                    offset=int(q.get("offset", 0)), order_by=order)])
        elif url.path == "/search":
            self.answer(lambda: search(q.get("q", "")))
        elif url.path == "/kids":
            self.answer(lambda: kids(q.get("kind", ""), q.get("id", ""), q.get("provider", "library")))
        elif url.path == "/image":
            try:
                pic = urllib.parse.quote(q.get("id", ""), safe="")
                with urllib.request.urlopen(f"http://{host()}:8095/imageproxy/{pic}?size=256", timeout=10) as r:
                    self.reply(200, r.read(), r.headers.get("Content-Type", "image/jpeg"))
            except OSError:
                self.reply(502, b"")
        else:
            self.reply(404, b"{}")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/play":
            def play():
                # Music Assistant keeps COMAND's player switched off when idle, and play_media then only
                # loads the queue: switch it on first.
                try:
                    api("players/cmd/power", player_id=QUEUE, powered=True)
                except Exception as e:  # noqa: BLE001 - play anyway; it may already be on
                    print(f"power on: {e}", flush=True)
                return api("player_queues/play_media", queue_id=QUEUE, media=body["uri"], option="replace") or {"ok": True}
            self.answer(play)
        elif self.path == "/ctl" and body.get("action") in ("play_pause", "next", "previous"):
            self.answer(lambda: api(f"player_queues/{body['action']}", queue_id=QUEUE) or {"ok": True})
        else:
            self.reply(404, b"{}")


if __name__ == "__main__":
    print("listening on 127.0.0.1:8091" + ("" if TOKEN else ", no token yet"), flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8091), Handler).serve_forever()
