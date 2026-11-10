#!/usr/bin/env python3
"""Make a connected iPod Classic playable.

Watches for the iPod appearing, mounts it, exposes its music to MPD and tells
the bridge. Unplugging is handled too: the symlink goes, the database is
updated, and playback of a device that is no longer there stops rather than
stalling.

WHY NOT libgpod
An iPod Classic is USB mass storage, and iTunes writes real ID3 tags into the
files it copies. So MPD can index them directly and show proper artist, album
and title -- no iTunesDB parsing, no libgpod, nothing to keep in step with a
database format Apple stopped documenting.

The filenames are scrambled (`iPod_Control/Music/F07/ABCD.mp3`) and MPD does
not care, because it reads tags rather than paths.

What is lost by skipping libgpod: on-device playlists, play counts and ratings,
which live only in the iTunesDB. If those turn out to matter, libgpod can be
added later without changing anything here -- the files are still the files.

FORMATS
A Mac-formatted iPod is HFS+ and a Windows-formatted one is FAT32. Both are
handled; HFS+ needs its kernel module, which is present but not loaded until
something asks for it. HFS+ is mounted read-only on purpose: the kernel's
write support for it is not something to trust a music library to, and nothing
here needs to write.

Usage:
    ipod-watch.py                 # watch, mount, index, report
    ipod-watch.py --once          # do one pass and exit
    ipod-watch.py --eject         # unmount and clean up
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

BRIDGE = "http://127.0.0.1:8080/cmd"
MUSIC_DIR = "/var/lib/comand/music"
LINK = os.path.join(MUSIC_DIR, "ipod")
MOUNT = "/media/ipod"
APPLE_VID = "05ac"
POLL_S = 3.0


def run(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def find_ipod_partition() -> str | None:
    """The iPod's data partition, by USB vendor rather than by size or label.

    An iPod is not the only removable disk that might be plugged into a car, so
    matching Apple's vendor id is what makes this specific. The partition we
    want is the one carrying iPod_Control, which is checked after mounting.
    """
    code, out = run(["lsblk", "-J", "-o", "NAME,PATH,TYPE,FSTYPE,VENDOR,TRAN,MOUNTPOINT"])
    if code != 0:
        return None
    try:
        tree = json.loads(out)
    except ValueError:
        return None

    def walk(nodes, usb_apple=False):
        for n in nodes:
            vendor = (n.get("vendor") or "").strip().lower()
            is_apple = usb_apple or "apple" in vendor
            if n.get("type") == "part" and is_apple and n.get("fstype"):
                yield n
            yield from walk(n.get("children", []), is_apple)

    for part in walk(tree.get("blockdevices", [])):
        return part["path"]

    # lsblk does not always carry the vendor string through to the partition,
    # so fall back to asking udev directly about each removable disk.
    code, out = run(["lsblk", "-J", "-o", "NAME,PATH,TYPE,FSTYPE,RM"])
    try:
        tree = json.loads(out)
    except ValueError:
        return None
    for disk in tree.get("blockdevices", []):
        if not disk.get("rm"):
            continue
        code, info = run(["udevadm", "info", "--query=property", f"--name={disk['path']}"])
        if f"ID_VENDOR_ID={APPLE_VID}" not in info:
            continue
        for part in disk.get("children", []):
            if part.get("fstype"):
                return part["path"]
    return None


def mount(dev: str) -> str | None:
    if os.path.ismount(MOUNT):
        return MOUNT
    os.makedirs(MOUNT, exist_ok=True)

    code, out = run(["lsblk", "-no", "FSTYPE", dev])
    fstype = out.strip().splitlines()[0] if out.strip() else ""

    if fstype == "hfsplus":
        run(["modprobe", "hfsplus"])
        # Read-only deliberately: the kernel's HFS+ write support is not worth
        # risking a music library on, and nothing here writes.
        opts = ["-t", "hfsplus", "-o", "ro,uid=1000,gid=1000"]
    elif fstype in ("vfat", "exfat"):
        opts = ["-o", "ro,uid=1000,gid=1000,iocharset=utf8"]
    else:
        print(f"unexpected filesystem {fstype!r} on {dev}", file=sys.stderr)
        return None

    code, out = run(["mount", *opts, dev, MOUNT])
    if code != 0:
        print(f"mount failed: {out}", file=sys.stderr)
        return None
    return MOUNT


def music_root(mountpoint: str) -> str | None:
    """iPod_Control/Music, whatever case the filesystem presents it in."""
    for control in ("iPod_Control", "ipod_control", "IPOD_CONTROL"):
        path = os.path.join(mountpoint, control, "Music")
        if os.path.isdir(path):
            return path
    return None


def mpc(*args: str) -> str:
    return run(["mpc", "-h", "127.0.0.1", *args])[1]


def tell_bridge(payload: dict) -> None:
    try:
        req = urllib.request.Request(
            BRIDGE, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def attach() -> bool:
    dev = find_ipod_partition()
    if not dev:
        return False

    mp = mount(dev)
    if not mp:
        return False

    root = music_root(mp)
    if not root:
        print(f"{dev} mounted but has no iPod_Control/Music", file=sys.stderr)
        return False

    os.makedirs(MUSIC_DIR, exist_ok=True)
    if os.path.islink(LINK):
        os.unlink(LINK)
    os.symlink(root, LINK)

    print(f"iPod on {dev}, indexing {root}", flush=True)
    mpc("update")
    # Wait for the scan rather than reporting a library that is still filling
    # up: a menu that grows while being read is worse than one that appears a
    # few seconds late.
    for _ in range(120):
        if "updating_db" not in mpc("status"):
            break
        time.sleep(1)

    count = len([l for l in mpc("listall").splitlines() if l.strip()])
    print(f"iPod indexed: {count} tracks", flush=True)
    tell_bridge({"ipod": {"connected": True, "tracks": count}})
    return True


def detach() -> None:
    if os.path.islink(LINK):
        os.unlink(LINK)
    if os.path.ismount(MOUNT):
        run(["umount", MOUNT])
    mpc("update")
    print("iPod gone", flush=True)
    tell_bridge({"ipod": {"connected": False, "tracks": 0}})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--eject", action="store_true")
    args = ap.parse_args()

    if args.eject:
        detach()
        return 0

    attached = os.path.ismount(MOUNT)
    if args.once:
        return 0 if attach() else 1

    while True:
        present = find_ipod_partition() is not None
        if present and not attached:
            attached = attach()
        elif not present and attached:
            detach()
            attached = False
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
