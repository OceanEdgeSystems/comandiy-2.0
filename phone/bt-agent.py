#!/usr/bin/env python3
"""Pairing for a device with no keyboard.

A car kit cannot type a PIN and has nowhere sensible to show one, so BlueZ
needs an agent that answers on its behalf. Without one, pairing simply fails:
bluetoothd has no way to confirm anything and rejects the request.

This registers as DisplayOnly, which is what asks the phone for passkey entry:
the car shows a six-digit code and the driver types it into the phone. The
panel is the display -- the code is pushed to the bridge and drawn full screen
-- so the capability is honest rather than a claim about hardware we do not
have.

WHY NOT JUST WORKS
It was, and it worked, and it was wrong. Just Works pairs anything that asks,
and the adapter is permanently discoverable because a car that stops being
pairable after five minutes is one where adding a phone means finding a menu
first. Those two together mean anyone within ten metres can pair to the car
while it sits on a driveway. A code the driver reads off the panel and types
into their phone costs one step and closes that.

The device is still trusted once paired, so it reconnects on its own the next
morning rather than being confirmed again.

Usage:
    bt-agent.py                 # register and stay resident
    bt-agent.py --discoverable  # also make the adapter visible and pairable
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import urllib.request

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

AGENT_PATH = "/org/comand/btagent"
# DisplayOnly: we can show a code and cannot take one. That is exactly true of
# a car -- the panel draws, the phone types -- and it is what makes the phone
# choose passkey entry over Just Works.
CAPABILITY = "DisplayOnly"
BRIDGE = "http://127.0.0.1:8080/cmd"
# Long enough to read a code off the panel and type it, short enough that a
# code left on screen is not a code somebody else can use later.
CODE_TTL_S = 90


class Rejected(dbus.DBusException):
    _dbus_error_name = "org.bluez.Error.Rejected"


def name_of(device) -> str:
    """The phone's own name, for the card on the panel."""
    try:
        props = dbus.Interface(dbus.SystemBus().get_object("org.bluez", device),
                               "org.freedesktop.DBus.Properties")
        return str(props.Get("org.bluez.Device1", "Alias"))
    except Exception:
        return ""


class Agent(dbus.service.Object):
    _timer = None

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Release(self):
        print("agent released", flush=True)

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def AuthorizeService(self, device, uuid):
        # Which profile the phone wants -- hands-free, audio, phonebook. A car
        # kit says yes to all of them; refusing one leaves a phone that is
        # paired but silent.
        print(f"authorize {uuid} for {device}", flush=True)
        self.trust(device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="s")
    def RequestPinCode(self, device):
        """Legacy pairing, for phones that predate secure simple pairing.

        The code is generated rather than fixed: a car with a printed 0000 is a
        car anybody can pair to. It goes on the panel the same way a passkey
        does.
        """
        code = f"{secrets.randbelow(10 ** 4):04d}"
        print(f"pin {code} for {device}", flush=True)
        self.show(device, code)
        return code

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="u")
    def RequestPasskey(self, device):
        # Asked of a keyboard, which we are not. Answering zero would pair
        # anything, so refuse and let the phone fall back to a model we can do.
        print(f"passkey entry requested by {device}: refused", file=sys.stderr)
        raise Rejected("no keyboard")

    @dbus.service.method("org.bluez.Agent1", in_signature="ouq", out_signature="")
    def DisplayPasskey(self, device, passkey, entered):
        # Called again as each digit is typed on the phone, so the panel can
        # show progress rather than a code that sits there looking inert.
        code = f"{int(passkey):06d}"
        print(f"passkey {code} for {device}, {entered} entered", flush=True)
        self.show(device, code, int(entered))

    @dbus.service.method("org.bluez.Agent1", in_signature="os", out_signature="")
    def DisplayPinCode(self, device, pincode):
        print(f"pin {pincode} for {device}", flush=True)
        self.show(device, str(pincode))

    @dbus.service.method("org.bluez.Agent1", in_signature="ou", out_signature="")
    def RequestConfirmation(self, device, passkey):
        """Both ends can display, so both should agree the code matches.

        Shown and accepted rather than shown and waited on: there is no yes
        button on this panel, and the driver comparing the code on the phone
        with the one on the screen is the check that matters. A mismatch is
        refused on the phone.
        """
        code = f"{int(passkey):06d}"
        print(f"confirm {code} for {device}", flush=True)
        self.show(device, code)
        self.trust(device)

    @dbus.service.method("org.bluez.Agent1", in_signature="o", out_signature="")
    def RequestAuthorization(self, device):
        print(f"authorize {device}: yes", flush=True)
        self.trust(device)

    @dbus.service.method("org.bluez.Agent1", in_signature="", out_signature="")
    def Cancel(self):
        print("pairing cancelled by the phone", flush=True)
        self.clear()

    # ------------------------------------------------------------------ --
    def show(self, device, code: str, entered: int = 0) -> None:
        """Put the code on the panel, and take it down again by itself.

        A code that stays on screen after pairing is over is one somebody can
        use later, so it expires. The timer is replaced each time a new code
        arrives rather than stacking, or typing the last digit would clear a
        code that had only just gone up.
        """
        self.tell({"pairing": {"code": code, "entered": entered,
                               "who": name_of(device)}})
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(CODE_TTL_S, self.clear)
        self._timer.daemon = True
        self._timer.start()

    def clear(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self.tell({"pairing": None})

    def tell(self, payload: dict) -> None:
        try:
            req = urllib.request.Request(
                BRIDGE, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
        except Exception:
            # The panel is where the code is read, but the log has it too, so a
            # bridge that is not up must not stop the pairing itself.
            pass

    def trust(self, device) -> None:
        """Mark the phone trusted so it reconnects without being accepted again.

        A car that asks the driver to confirm the connection every morning is
        worse than one that does not ask at all.
        """
        try:
            bus = dbus.SystemBus()
            props = dbus.Interface(bus.get_object("org.bluez", device),
                                   "org.freedesktop.DBus.Properties")
            props.Set("org.bluez.Device1", "Trusted", True)
        except Exception as e:
            print(f"could not trust {device}: {e}", file=sys.stderr)


def adapter(bus):
    """The first adapter, whatever it is called."""
    objs = dbus.Interface(bus.get_object("org.bluez", "/"),
                          "org.freedesktop.DBus.ObjectManager").GetManagedObjects()
    for path, ifaces in objs.items():
        if "org.bluez.Adapter1" in ifaces:
            return path
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discoverable", action="store_true",
                    help="also make the adapter visible and pairable")
    args = ap.parse_args()

    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()

    agent = Agent(bus, AGENT_PATH)

    # The code comes down the moment the phone is paired. It used to stay up
    # until it expired, on the screen of a car that had already accepted the
    # phone, looking like a pairing still waiting for an answer.
    def paired(iface, changed, invalidated, path=None):
        if iface != "org.bluez.Device1":
            return
        # A phone that is already paired can be asked to confirm a code again:
        # Paired does not change then, and the link coming up is the only sign.
        if changed.get("Paired") or changed.get("Bonded") or (
                agent._timer and (changed.get("Connected") or changed.get("ServicesResolved"))):
            print(f"paired: {path}", flush=True)
            agent.clear()
    bus.add_signal_receiver(paired, signal_name="PropertiesChanged",
                            dbus_interface="org.freedesktop.DBus.Properties",
                            path_keyword="path")
    mgr = dbus.Interface(bus.get_object("org.bluez", "/org/bluez"),
                         "org.bluez.AgentManager1")
    mgr.RegisterAgent(AGENT_PATH, CAPABILITY)
    # Default agent, so BlueZ routes requests here rather than to whatever
    # bluetoothctl session happens to be open.
    mgr.RequestDefaultAgent(AGENT_PATH)
    print(f"agent registered as {CAPABILITY}", flush=True)

    if args.discoverable:
        path = adapter(bus)
        if path:
            props = dbus.Interface(bus.get_object("org.bluez", path),
                                   "org.freedesktop.DBus.Properties")
            props.Set("org.bluez.Adapter1", "Powered", True)
            props.Set("org.bluez.Adapter1", "Pairable", True)
            props.Set("org.bluez.Adapter1", "Discoverable", True)
            name = props.Get("org.bluez.Adapter1", "Alias")
            print(f"{path} is discoverable as {name}", flush=True)

    GLib.MainLoop().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
