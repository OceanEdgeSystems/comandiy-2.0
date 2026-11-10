#!/usr/bin/env python3
"""Pull the phone's contacts over Bluetooth.

Hands-free carries calls, not contacts: there is nothing in HFP that will
hand over a phonebook, which is why the car has had a hand-written list. The
profile that does carry one is PBAP, and BlueZ implements its client side in
obexd -- a separate daemon from bluetoothd, on the session bus, spoken to
through org.bluez.obex.

The phone decides whether to answer. On iOS this is the "Sync Contacts"
switch under the car in Bluetooth settings, and it is off until somebody
turns it on; a refusal here is that switch, not a fault.

What is pulled goes to its own file rather than into contacts.json. The
driver's list is theirs -- a handful of numbers they chose to have one press
away -- and overwriting it with three hundred entries from a phone would
lose it. The bridge shows both.

Usage:
    pbap-sync.py                 # sync the connected phone
    pbap-sync.py --list          # show what was pulled last time
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

OUT = "/var/lib/comand/phonebook.json"
OBEX = "org.bluez.obex"
# Numbers shorter than this are service codes rather than people, and a
# phonebook full of them is a phonebook nobody scrolls.
MIN_DIGITS = 5


def bluez_phone() -> str:
    """The connected phone's address, or "".

    The one that is connected, not the one that is paired: a car keeps old
    pairings for phones that are not in it.
    """
    import dbus
    bus = dbus.SystemBus()
    objs = dbus.Interface(bus.get_object("org.bluez", "/"),
                          "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    for path, ifaces in objs.items():
        dev = ifaces.get("org.bluez.Device1")
        if dev and dev.get("Connected"):
            return str(dev.get("Address", ""))
    return ""


def parse_vcards(text: str) -> list[dict]:
    """vCards to {name, number}, one entry per number.

    Properties are split on the first colon and their parameters discarded:
    a vCard line is NAME;PARAM=VALUE:value, and matching on "FN:" alone finds
    nothing at all on a phone that sends "FN;CHARSET=UTF-8:". That is what
    three hundred nameless contacts looked like.

    A contact with a mobile and a landline becomes two rows, because a row is
    the thing you press to dial and it has to mean one number. The type goes
    in the name so the two are telling you which is which rather than looking
    like a duplicate.
    """
    out: list[dict] = []
    name = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            if line.upper().startswith("BEGIN:VCARD"):
                name = ""
            continue
        head, _, value = line.partition(":")
        parts = head.split(";")
        prop = parts[0].upper()
        params = [p.upper() for p in parts[1:]]
        value = value.strip()

        if prop == "BEGIN" and value.upper() == "VCARD":
            name = ""
        elif prop == "FN":
            name = value
        elif prop == "N" and not name:
            # family;given;middle;prefix;suffix -- assembled as it is read.
            f = value.split(";")
            given = f[1].strip() if len(f) > 1 else ""
            family = f[0].strip()
            name = " ".join(x for x in (given, family) if x)
        elif prop == "TEL":
            digits = "".join(c for c in value if c.isdigit())
            if len(digits) < MIN_DIGITS:
                continue
            kind = ""
            for t, label in (("CELL", "Mobile"), ("IPHONE", "Mobile"),
                             ("HOME", "Home"), ("WORK", "Work"),
                             ("MAIN", "Main")):
                if any(t in p for p in params):
                    kind = label
                    break
            label = f"{name} ({kind})" if name and kind else (name or value)
            out.append({"name": label, "number": value})
    return out


def sync(timeout: float = 60.0) -> int:
    import dbus
    import dbus.mainloop.glib
    from gi.repository import GLib

    addr = bluez_phone()
    if not addr:
        print("no phone connected", file=sys.stderr)
        return 1
    print(f"pulling the phonebook from {addr}", flush=True)

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SessionBus()
    client = dbus.Interface(bus.get_object(OBEX, "/org/bluez/obex"),
                            OBEX + ".Client1")
    try:
        session = client.CreateSession(addr, {"Target": "PBAP"})
    except Exception as e:
        # The usual answer when the phone has not been told to share.
        print(f"no PBAP session: {e}", file=sys.stderr)
        print("on iPhone: Settings > Bluetooth > the car > Sync Contacts",
              file=sys.stderr)
        return 2

    pba = dbus.Interface(bus.get_object(OBEX, session),
                         OBEX + ".PhonebookAccess1")
    # "int" is the phone's own memory; "sim1" would be the SIM, which on a
    # modern phone is empty.
    pba.Select("int", "pb")

    path, props = pba.PullAll("", dbus.Dictionary({}, signature="sv"))
    done = {"ok": False, "file": str(props.get("Filename", ""))}
    loop = GLib.MainLoop()

    def on_props(iface, changed, invalidated, p=None):
        status = str(changed.get("Status", ""))
        if status in ("complete", "error"):
            done["ok"] = status == "complete"
            loop.quit()

    bus.add_signal_receiver(on_props, path=path,
                            dbus_interface="org.freedesktop.DBus.Properties",
                            signal_name="PropertiesChanged")
    GLib.timeout_add_seconds(int(timeout), lambda: (loop.quit(), False)[1])
    loop.run()

    try:
        client.RemoveSession(session)
    except Exception:
        pass

    if not done["ok"] or not done["file"] or not os.path.exists(done["file"]):
        print("transfer did not complete", file=sys.stderr)
        return 3

    with open(done["file"], "r", errors="replace") as fh:
        rows = parse_vcards(fh.read())
    os.unlink(done["file"])

    # Sorted by name, because a phonebook is scrolled rather than searched on
    # a panel with no keyboard. Entries the phone has no name for sort last:
    # they are unreadable as a list and would otherwise sit at the top, since
    # a number starting with "(" sorts before every letter.
    def key(c):
        named = c["name"] != c["number"]
        return (0 if named else 1, c["name"].lower())
    rows.sort(key=key)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as fh:
        json.dump({"synced": int(time.time()), "contacts": rows}, fh)
    print(f"{len(rows)} numbers -> {OUT}", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        try:
            with open(OUT) as fh:
                d = json.load(fh)
        except Exception as e:
            print(f"nothing synced: {e}", file=sys.stderr)
            return 1
        for c in d.get("contacts", []):
            print(f"  {c['name']:<32} {c['number']}")
        return 0

    return sync()


if __name__ == "__main__":
    sys.exit(main())
