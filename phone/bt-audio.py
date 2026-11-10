#!/usr/bin/env python3
"""The car as a Bluetooth speaker: play the phone's audio, show what it is.

Two jobs that belong together because they are the same phone.

AUDIO
Nothing to do here. When a phone plays to us over A2DP, WirePlumber links its
stream to the default output by itself. This used to add a loopback as well, which
played everything twice, slightly apart.

METADATA
BlueZ exposes what the phone is playing as org.bluez.MediaPlayer1 -- title,
artist, album, position, and whether it is playing. That is AVRCP, and it
arrives without asking. Cover art does not: AVRCP 1.6 carries images over a
separate OBEX channel that BlueZ does not implement, so the art is fetched by
name instead. See phone/cover-art.py.

Usage:
    bt-audio.py                 # watch, report
    bt-audio.py --once          # one pass, for looking at what it sees
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import urllib.request

BRIDGE = "http://127.0.0.1:8080/cmd"
POLL_S = 1.0
BATTERY_S = 30.0        # sent again this often, for a bridge that has restarted
POSITION_S = 5.0        # the page counts between these, and they catch a seek


def tell(payload: dict) -> None:
    try:
        req = urllib.request.Request(
            BRIDGE, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def bluez_objects() -> dict:
    """Everything BlueZ is exposing, or {} if it cannot be asked.

    Fetched once per pass and handed to whoever needs it: the connected device
    and the media player are two facts about the same phone, and asking twice
    a second for each was two round trips for one answer.
    """
    try:
        import dbus
        bus = dbus.SystemBus()
        return dbus.Interface(bus.get_object("org.bluez", "/"),
                              "org.freedesktop.DBus.ObjectManager"
                              ).GetManagedObjects()
    except Exception:
        return {}


# What makes a connected device a phone rather than something else on the
# radio. Hands-free, headset, and the two ends of A2DP: a phone offers at
# least one of these, and a battery monitor offers none of them.
PHONE_UUIDS = {
    "0000111e-0000-1000-8000-00805f9b34fb",   # handsfree
    "0000111f-0000-1000-8000-00805f9b34fb",   # handsfree audio gateway
    "00001108-0000-1000-8000-00805f9b34fb",   # headset
    "0000110a-0000-1000-8000-00805f9b34fb",   # audio source
    "0000110b-0000-1000-8000-00805f9b34fb",   # audio sink
}


def connected_phone(objs: dict) -> str:
    """The name of the connected phone, or "" if none is.

    Its name rather than a flag, because the announcement says which phone --
    a car that pairs with more than one has to say which one it just picked
    up, and "phone connected" would be the same sentence either way.

    Only things that offer a phone's services count. This used to be "any
    connected device", which was true of exactly one kind of device until the
    car started talking to a battery monitor -- and then every BMS poll
    connected, announced itself across both screens, disconnected, and did it
    again thirty seconds later.
    """
    for _path, ifaces in objs.items():
        dev = ifaces.get("org.bluez.Device1")
        if not dev or not bool(dev.get("Connected")):
            continue
        uuids = {str(u).lower() for u in (dev.get("UUIDs") or [])}
        if not uuids & PHONE_UUIDS:
            continue
        return str(dev.get("Alias") or dev.get("Name") or "Phone")
    return ""


def phone_battery(objs: dict) -> int:
    """The connected phone's battery in percent, or -1. An iPhone offers it over BLE."""
    for _path, ifaces in objs.items():
        dev = ifaces.get("org.bluez.Device1")
        if (dev and bool(dev.get("Connected")) and {str(u).lower() for u in (dev.get("UUIDs") or [])} & PHONE_UUIDS
                and "org.bluez.Battery1" in ifaces):
            return int(ifaces["org.bluez.Battery1"].get("Percentage", -1))
    return -1


HFP_GATEWAY = "0000111f-0000-1000-8000-00805f9b34fb"
HANDSFREE_RETRY_S = 30.0


def handsfree_online() -> bool:
    try:
        import dbus
        mgr = dbus.Interface(dbus.SystemBus().get_object("org.ofono", "/"), "org.ofono.Manager")
        return any(bool(props.get("Online")) for _path, props in mgr.GetModems())
    except Exception:
        return True                 # can't tell: leave the link alone


def phone_without_handsfree(objs: dict) -> str:
    """The address of a connected phone whose hands-free link has dropped, or "".

    A phone that goes away and comes back reconnects its music and not always its calls,
    and nothing asks again: the car then has no phone until it is paired afresh.
    """
    for _path, ifaces in objs.items():
        dev = ifaces.get("org.bluez.Device1")
        if (dev and bool(dev.get("Connected"))
                and HFP_GATEWAY in {str(u).lower() for u in (dev.get("UUIDs") or [])}):
            return "" if handsfree_online() else str(dev.get("Address"))
    return ""


def connect_handsfree(address: str) -> None:
    print(f"hands-free dropped: connecting {address}", flush=True)
    subprocess.run(["bluetoothctl", "--timeout", "12", "connect", address, HFP_GATEWAY],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def media_player(objs: dict) -> dict:
    """What the phone says it is playing, or {}.

    BlueZ puts a MediaPlayer1 object under the device while something is
    playing and takes it away afterwards, so its absence is the answer rather
    than an error.
    """
    for path, ifaces in objs.items():
        props = ifaces.get("org.bluez.MediaPlayer1")
        if not props:
            continue
        track = props.get("Track", {}) or {}
        return {
            "title": str(track.get("Title", "") or ""),
            "artist": str(track.get("Artist", "") or ""),
            "album": str(track.get("Album", "") or ""),
            # Milliseconds from AVRCP; seconds is what a screen shows.
            "duration": int(track.get("Duration", 0) or 0) // 1000,
            "position": int(props.get("Position", 0) or 0) // 1000,
            "status": str(props.get("Status", "") or ""),
        }
    return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    linked = ""
    last: dict = {}
    position_sent = 0.0
    battery, battery_sent = None, 0.0
    handsfree_tried = 0.0
    while True:
        objs = bluez_objects()

        # The phone arriving is worth saying out loud once. Announced on the
        # transition rather than on the state, or every pass would announce a
        # phone that has been sitting there connected all along.
        phone = connected_phone(objs)
        if phone and phone != linked:
            tell({"toast": {"icon": "bluetooth",
                            "text": f"{phone} connected"}})
            print(f"{phone} connected", flush=True)
        linked = phone

        if time.monotonic() - handsfree_tried >= HANDSFREE_RETRY_S:
            address = phone_without_handsfree(objs)
            if address:
                handsfree_tried = time.monotonic()
                threading.Thread(target=connect_handsfree, args=(address,), daemon=True).start()

        level = phone_battery(objs)
        if level != battery or time.monotonic() - battery_sent >= BATTERY_S:
            battery, battery_sent = level, time.monotonic()
            tell({"phonebat": level})

        media = media_player(objs)
        # During CarPlay the phone says "playing" over Bluetooth with no track,
        # and the track comes from carplay.py instead.
        if not media.get("title"):
            media = {}
        # Compared on what is drawn: AVRCP reports position every second, and
        # republishing the whole state for a clock nobody is watching would
        # push it to both screens sixty times a minute.
        drawn = {k: media.get(k) for k in ("title", "artist", "album", "status")}
        if drawn != last:
            last = drawn
            position_sent = time.monotonic()
            tell({"btmedia": media or None})
            if media:
                print(f"{media['status']}: {media['artist']} - {media['title']}",
                      flush=True)
            else:
                print("nothing playing", flush=True)

        elif media.get("status") == "playing" and time.monotonic() - position_sent >= POSITION_S:
            position_sent = time.monotonic()
            tell({"btpos": media["position"]})

        if args.once:
            print(f"phone={phone or '-'}", flush=True)
            return 0
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
