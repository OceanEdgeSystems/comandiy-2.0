#!/usr/bin/env python3
"""Album art for what the phone is playing.

AVRCP knows the artist, the album and the title, and carries the artwork on a
separate OBEX channel that BlueZ does not implement -- so the art cannot come
from the phone. It is looked up by name instead.

The source is Apple's public search endpoint, which is free, needs no key, and
knows about the music people actually have. Nothing is invented: a track it
does not recognise gets no art, and the screen shows the words alone, which is
better than a wrong cover with somebody else's record on it.

Cached on the car, because the same album plays on the same commute.

Usage:
    cover-art.py "Artist" "Album" ["Title"]
    cover-art.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.parse
import urllib.request

OUT = "/var/lib/comand/covers"
SEARCH = "https://itunes.apple.com/search"
UA = {"User-Agent": "Mozilla/5.0 (comand head unit cover art)"}
TIMEOUT = 12
# 600px is far more than a 320x230 panel needs, but the same file has to look
# right on the 800x480 bench screen, and the difference is 40kB on a disk with
# 800GB free.
SIZE = "600x600"


def key(artist: str, album: str) -> str:
    basis = f"{artist} {album}".strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", basis).strip("-")[:80]


def cached(k: str) -> str:
    for ext in (".jpg", ".png", ".webp"):
        p = os.path.join(OUT, k + ext)
        if os.path.exists(p):
            return p
    return ""


def words(text: str) -> list[str]:
    """Lower case, no accents, no punctuation: "Machín" and "Machin" are one word."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", plain.lower())


# What a release adds to its name and a shop may or may not repeat.
EDITION = {"deluxe", "edition", "remaster", "remastered", "version", "bonus", "expanded",
           "anniversary", "single", "ep", "the", "of", "a", "and", "feat", "ft"}


def named(text: str) -> set[str]:
    return set(words(text)) - EDITION


def same_artist(asked: str, row: str) -> bool:
    """All of one name inside the other: "Moby" is on "Moby, Amaral", Sade is not."""
    a, b = set(words(asked)), set(words(row))
    return bool(a and b) and (a <= b or b <= a)


def same_name(asked: str, row: str) -> bool:
    """Most of the words asked for: shops title the same release differently."""
    a, b = named(asked), named(row)
    return bool(a) and len(a & b) / len(a) >= 0.6


def lookup(entity: str, term: str) -> list[dict]:
    q = urllib.parse.urlencode({"term": term, "entity": entity, "limit": 25})
    try:
        req = urllib.request.Request(f"{SEARCH}?{q}", headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("results", [])
    except Exception as e:
        print(f"search failed: {e}", file=sys.stderr)
        return []


def search(artist: str, album: str, title: str = "") -> str:
    """The artwork URL for this record, or "".

    The shop answers with its nearest guess however far that is, so every answer is
    checked against the artist and the name. The album first, for the album's cover,
    then the track, which also covers a track whose album the shop calls something else.
    """
    plain = re.sub(r"\(.*?\)|\[.*?\]", " ", album)
    rows = [r for r in lookup("album", f"{artist} {plain}")
            if same_artist(artist, r.get("artistName", ""))
            and same_name(album, r.get("collectionName", ""))]
    if not rows and title:
        short = re.sub(r"\(.*?\)|\[.*?\]", " ", title)
        rows = [r for r in lookup("musicTrack", f"{artist} {short}")
                if same_artist(artist, r.get("artistName", ""))
                and same_name(short, r.get("trackName", ""))]
        rows.sort(key=lambda r: not same_name(album, r.get("collectionName", "")))
    for row in rows:
        url = row.get("artworkUrl100") or row.get("artworkUrl60") or ""
        if url:
            # The API returns a 100px thumbnail; the same path serves any size.
            return re.sub(r"/\d+x\d+bb", f"/{SIZE}bb", url)
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("artist", nargs="?", default="")
    ap.add_argument("album", nargs="?", default="")
    ap.add_argument("title", nargs="?", default="")
    args = ap.parse_args()

    if args.list:
        for f in sorted(os.listdir(OUT)) if os.path.isdir(OUT) else []:
            print(" ", f)
        return 0

    if not (args.artist or args.album):
        print("nothing to look up", file=sys.stderr)
        return 1

    k = key(args.artist, args.album)
    have = cached(k)
    if have:
        print(f"cached {os.path.basename(have)}")
        return 0

    url = search(args.artist, args.album, args.title)
    if not url:
        print(f"no art for {args.artist} - {args.album}")
        return 1

    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read(4 * 1024 * 1024)
    except Exception as e:
        print(f"download failed: {e}", file=sys.stderr)
        return 1
    if len(data) < 500:
        print("art too small to be art", file=sys.stderr)
        return 1

    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext not in (".jpg", ".png", ".webp"):
        ext = ".jpg"
    os.makedirs(OUT, exist_ok=True)
    dest = os.path.join(OUT, k + ext)
    with open(dest, "wb") as fh:
        fh.write(data)
    print(f"{os.path.basename(dest)} ({len(data)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
