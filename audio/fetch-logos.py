#!/usr/bin/env python3
"""Fetch each station's own logo for the radio tiles.

The tiles were two or three letters on a colour, which is legible but not
recognisable -- and recognition is the whole reason a grid beats a list on a
screen used while driving. This fetches the real artwork.

WHERE THE ARTWORK COMES FROM
A logo bundled in audio/logos/<slug>.<ext> wins outright: it is in the repo,
so it needs no network at all and cannot change under the car. Failing that,
an explicit `logo_url` in stations.json: a hand-picked mark
beats anything guessed, and guessing finds a station's favicon as often as its
logo. Failing that, the station's own site, in the order a browser would
prefer:

    apple-touch-icon    usually 180x180 PNG, square, made to sit on a grid --
                        which is exactly what a tile is
    og:image            large, but often a banner rather than a mark
    favicon             last resort; 32x32 looks poor at tile size

Nothing is invented. A station with no usable icon keeps its letters, which is
honest: a wrong logo is worse than clear lettering.

Logos are cached on the car and only re-fetched with --force, because a head
unit should not depend on a station's website being up to draw its own menu.

Usage:
    fetch-logos.py                 # fetch what is missing
    fetch-logos.py --force         # re-fetch everything
    fetch-logos.py --list          # what is cached, what is not
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

STATIONS = "/var/lib/comand/stations.json"
LOGO_DIR = "/var/lib/comand/logos"
# the IPTV server's IPTV head-end resolves a channel/station name to an already
# transparent logo from the tv-logos set -- the same artwork the Live TV guide
# uses. Optional: IPTV_URL in /etc/default/comand.
IPTV_BASE = os.environ.get("IPTV_URL", "")
# Logos shipped with the source, beside this script. Hand-picked artwork that
# should not depend on a website still being up, or on the car having a network
# the first time it draws its own menu.
BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logos")
UA = {"User-Agent": "Mozilla/5.0 (comand head unit logo fetch)"}
TIMEOUT = 15

ICON_RE = re.compile(
    r'<link[^>]+rel=["\']?(?:apple-touch-icon[^"\']*|shortcut icon|icon)["\']?[^>]*>',
    re.I)
HREF_RE = re.compile(r'href=["\']([^"\']+)["\']', re.I)
SIZES_RE = re.compile(r'sizes=["\'](\d+)x\d+["\']', re.I)
OG_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.I)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


EXTS = (".png", ".jpg", ".ico", ".svg", ".webp")


def clear_cached(dest_base: str) -> None:
    """Drop every cached form of one station's logo.

    Artwork does not always come back in the format it went out in -- a
    re-fetch that finds a JPEG where a PNG was cached would otherwise leave
    both, and everything that reads the cache takes the first extension it
    finds rather than the newest file. One logo per station, always.
    """
    for ext in EXTS:
        try:
            os.unlink(dest_base + ext)
        except FileNotFoundError:
            pass


def ext_for(url: str) -> str:
    """File extension to save under, from the URL's path.

    The query string is ignored on purpose -- Wikipedia's thumbnails carry
    tracking parameters after a perfectly good `.png`.
    """
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    if ext == ".jpeg":
        return ".jpg"
    return ext if ext in (".png", ".jpg", ".ico", ".svg", ".webp") else ".png"


def get(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read(4 * 1024 * 1024)
    except Exception:
        return None


def iptv_logo(name: str) -> str | None:
    """A transparent logo URL from the IPTV server's resolver, or None if it has none.

    The resolver matches a name against the tv-logos set and hands back a raw
    GitHub URL to a transparent PNG; strong for TV channels, thinner for radio.
    """
    try:
        url = IPTV_BASE + "/api/guest/iptv/logo-resolve?channels=" + urllib.parse.quote(name)
        data = get(url)
        if not data:
            return None
        return (json.loads(data).get("logos") or {}).get(name) or None
    except Exception:
        return None


def strip_bg(data: bytes):
    """Make a solid logo background transparent, leaving gradients untouched.

    Returns PNG bytes, or None to say "use the original": a logo that already
    has transparency needs no help, and a gradient background cannot be removed
    by matching one colour -- a half-stripped block looks worse than the whole
    image, so those are left alone. Needs Pillow; without it nothing is stripped.
    """
    try:
        import io
        from collections import deque
        from PIL import Image
        import numpy as np
    except Exception:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None
    a = np.asarray(im).astype(np.int16)
    h, w = a.shape[:2]
    if h < 8 or w < 8:
        return None
    # Already transparent? Leave it -- it is its own answer.
    if (a[:, :, 3] < 250).mean() > 0.04:
        return None
    rgb = a[:, :, :3]
    corners = np.stack([rgb[0, 0], rgb[0, w - 1], rgb[h - 1, 0], rgb[h - 1, w - 1]]).astype(np.float64)
    # A gradient shows up as corners that disagree; don't try to strip those.
    spread = max(np.linalg.norm(corners[i] - corners[j])
                 for i in range(4) for j in range(i + 1, 4))
    if spread > 42:
        return None
    bg = np.median(corners, axis=0)
    dist = np.sqrt(((rgb - bg) ** 2).sum(axis=2))
    tol = 36.0
    near = dist < tol
    # Flood from the border only, so a patch of the same colour inside the mark
    # (the hole of an 'O', say) is kept rather than punched out.
    seen = np.zeros((h, w), bool)
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if near[y, x] and not seen[y, x]:
                seen[y, x] = True; dq.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if near[y, x] and not seen[y, x]:
                seen[y, x] = True; dq.append((y, x))
    while dq:
        y, x = dq.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and not seen[ny, nx] and near[ny, nx]:
                seen[ny, nx] = True; dq.append((ny, nx))
    if not seen.any():
        return None
    out = a.copy()
    out[:, :, 3] = np.where(seen, 0, a[:, :, 3])
    # Feather the one ring where background meets mark, so the edge is not jagged.
    edge = (dist >= tol) & (dist < tol + 22) & ~seen
    soft = np.clip((dist - tol) / 22.0, 0.0, 1.0)
    out[:, :, 3] = np.where(edge, out[:, :, 3] * soft, out[:, :, 3]).astype(np.int16)
    res = Image.fromarray(out.astype(np.uint8), "RGBA")
    bbox = res.getbbox()
    if bbox:
        res = res.crop(bbox)
    buf = io.BytesIO()
    res.save(buf, "PNG")
    return buf.getvalue()


def save_logo(dest_base: str, data: bytes, ext: str, transparent: bool) -> str:
    """Write one station's logo, stripping a solid background unless it is
    already transparent artwork (IPTV/bundled PNGs)."""
    if not transparent:
        stripped = strip_bg(data)
        if stripped is not None:
            data, ext = stripped, ".png"
    os.makedirs(LOGO_DIR, exist_ok=True)
    clear_cached(dest_base)
    dest = dest_base + ext
    with open(dest, "wb") as fh:
        fh.write(data)
    return dest


def site_for(st: dict) -> str:
    """Where to look for artwork.

    An explicit `site` wins. Otherwise the stream's own host is the best guess
    -- streams usually live on a subdomain of the station, and asking the
    stream host for a homepage is cheap and often right.
    """
    if st.get("site"):
        return st["site"]
    u = urllib.parse.urlparse(st["url"])
    return f"{u.scheme}://{u.netloc}/"


def candidates(page_url: str, html: str) -> list[str]:
    """Icon URLs, best first."""
    found: list[tuple[int, str]] = []
    for tag in ICON_RE.findall(html):
        m = HREF_RE.search(tag)
        if not m:
            continue
        size = int(SIZES_RE.search(tag).group(1)) if SIZES_RE.search(tag) else 0
        # apple-touch-icon is square by convention and sized for a grid, so it
        # outranks a bigger favicon.
        if "apple-touch-icon" in tag.lower():
            size = max(size, 180)
        found.append((size, urllib.parse.urljoin(page_url, m.group(1))))

    for m in OG_RE.finditer(html):
        # Ranked below icons: og:image is often a wide banner, which has to be
        # cropped to a square and usually loses the mark doing it.
        found.append((60, urllib.parse.urljoin(page_url, m.group(1))))

    found.sort(key=lambda x: -x[0])
    out = [u for _, u in found]
    out.append(urllib.parse.urljoin(page_url, "/favicon.ico"))
    return out


def fetch_logo(st: dict, force: bool) -> str | None:
    name = st["name"]
    dest_base = os.path.join(LOGO_DIR, slug(name))
    for ext in EXTS:
        if os.path.exists(dest_base + ext) and not force:
            return dest_base + ext

    # A bundled logo is the whole answer, and needs no network. Curated
    # artwork, left exactly as shipped.
    for ext in (".png", ".jpg", ".svg", ".webp"):
        src = os.path.join(BUNDLED, slug(name) + ext)
        if os.path.exists(src):
            with open(src, "rb") as fh:
                data = fh.read()
            dest = save_logo(dest_base, data, ext, transparent=True)
            print(f"  {name}: bundled -> {os.path.basename(dest)} ({len(data)} bytes)")
            return dest

    # A hand-picked logo is the whole answer -- no page to parse, no ranking to
    # get wrong. Its background is stripped if it is a solid one.
    explicit = st.get("logo_url")
    if explicit:
        data = get(explicit)
        if data and len(data) >= 200:
            dest = save_logo(dest_base, data, ext_for(explicit), transparent=False)
            print(f"  {name}: {explicit} -> {os.path.basename(dest)}")
            return dest
        print(f"  {name}: logo_url unusable, trying the IPTV server", file=sys.stderr)

    # the IPTV server's transparent logo for this name, before guessing from the site:
    # it is the same clean artwork the Live TV guide shows, already cut out.
    resolved = iptv_logo(name)
    if resolved:
        data = get(resolved)
        if data and len(data) >= 200:
            dest = save_logo(dest_base, data, ext_for(resolved), transparent=True)
            print(f"  {name}: the IPTV server {os.path.basename(resolved)} -> {os.path.basename(dest)}")
            return dest

    page_url = site_for(st)
    raw = get(page_url)
    if raw is None:
        print(f"  {name}: no site at {page_url}", file=sys.stderr)
        return None

    html = raw.decode("utf-8", "replace")
    for url in candidates(page_url, html):
        data = get(url)
        if not data or len(data) < 200:
            continue
        dest = save_logo(dest_base, data, ext_for(url), transparent=False)
        print(f"  {name}: {url} -> {os.path.basename(dest)}")
        return dest

    print(f"  {name}: nothing usable, keeping letters", file=sys.stderr)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--stations", default=STATIONS)
    args = ap.parse_args()

    with open(args.stations) as fh:
        stations = json.load(fh).get("stations", [])

    if args.list:
        for st in stations:
            base = os.path.join(LOGO_DIR, slug(st["name"]))
            have = next((base + e for e in EXTS
                         if os.path.exists(base + e)), None)
            print(f"  {st['name']:<26} {os.path.basename(have) if have else '-- letters'}")
        return 0

    os.makedirs(LOGO_DIR, exist_ok=True)
    for st in stations:
        fetch_logo(st, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
