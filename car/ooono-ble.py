#!/usr/bin/env python3
"""The laptop poses as an Ooono NO2 co-driver over Bluetooth LE.

The Ooono app on the phone decides what to warn about and writes one command
byte to the box (see esp32-knob/knob-ui/ooono.h, which does this on the knob).
Here BlueZ serves the same GATT layout and each warning goes to the bridge,
which shows it on COMAND.

The app recognises a box by its name, and the phone is paired with this laptop
for calls, so it shows the laptop's own address under the name it remembers and
the app never finds an Ooono. So the advertising here goes out under a separate
random address the phone has never seen, set by raw HCI commands, because
BlueZ's advertising API always uses the adapter's own identity.
"""
import hashlib
import json
import socket
import struct
import sys
import threading
import time
import urllib.request

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib

BRIDGE = "http://127.0.0.1:8080/cmd"
BLUEZ, GATT_MGR = "org.bluez", "org.bluez.GattManager1"
OM = "org.freedesktop.DBus.ObjectManager"
PROPS = "org.freedesktop.DBus.Properties"
SVC_IF, CHR_IF = "org.bluez.GattService1", "org.bluez.GattCharacteristic1"
NAME = "OOONO2"
ADVERTISED = "72e81523-d54b-48ea-a00c-5ea3973cebc7"
WARNINGS = ("10", "11", "12", "13")      # what the bridge can show
HOLD_MS = 120                            # short, so two quick presses stay two


def uuid(x: str) -> str:
    return f"72e8{x}-d54b-48ea-a00c-5ea3973cebc7"


R, RN, RWN = ["read"], ["read", "notify"], ["read", "write", "notify"]
# The real box's values, because the app reads them on connect.
LAYOUT = [
    (uuid("0100"), [("0110", R, "01010004"), ("0120", R, "f2cf4ef5cd34"),
                    ("0130", RWN, "0fe459010101"), ("0140", RN, "00000018")]),
    (uuid("0200"), [("0210", RN, "02000000c8"), ("0211", RN, "02000000c8"),
                    ("0220", RWN, "00" * 20), ("0230", RWN, "00" * 20), ("0240", RWN, "0203")]),
    (uuid("0300"), [("0310", RN, "00000040")]),
    (uuid("0400"), [("0410", RWN, "00000000"), ("0411", RWN, "00000000"), ("0412", RWN, "00000000"),
                    ("0413", RWN, "00000000"), ("0414", RWN, "00000000")]),
]


def tell_bridge(payload: dict) -> None:
    def post():
        req = urllib.request.Request(BRIDGE, json.dumps(payload).encode(), method="POST")
        try:
            urllib.request.urlopen(req, timeout=3).read()
        except OSError as e:
            print(f"bridge: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=post, daemon=True).start()


class Characteristic(dbus.service.Object):
    def __init__(self, bus, path, service, char_uuid, flags, value):
        super().__init__(bus, path)
        self.path, self.service, self.uuid, self.flags = path, service, char_uuid, flags
        self.value = bytes.fromhex(value)
        self.notifying = False
        self.setting = 0x03

    def properties(self):
        return {CHR_IF: {"Service": dbus.ObjectPath(self.service), "UUID": self.uuid,
                         "Flags": dbus.Array(self.flags, signature="s")}}

    @dbus.service.method(CHR_IF, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        off = int(options.get("offset", 0))
        return dbus.Array([dbus.Byte(b) for b in self.value[off:]], signature="y")

    @dbus.service.method(CHR_IF, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        data = bytes(int(b) for b in value)
        self.value = data
        if self.uuid == uuid("0240"):
            self.command(data)

    def command(self, data: bytes) -> None:
        """The command slot: 04 xx is a setting, one byte is a command."""
        print(f"app wrote {data.hex()}", flush=True)
        if len(data) == 2:
            self.setting = data[1]
        elif len(data) == 1:
            self.value = bytes([data[0], self.setting])  # reads back like the real box
            if data.hex() in WARNINGS:
                tell_bridge({"ooono_test": data.hex()})

    @dbus.service.method(CHR_IF)
    def StartNotify(self):
        self.notifying = True
        print(f"app listening to {self.uuid[4:8]}", flush=True)
        if self.uuid == uuid("0210"):
            tell_bridge({"ooono_conn": True})

    @dbus.service.method(CHR_IF)
    def StopNotify(self):
        self.notifying = False
        print(f"app stopped listening to {self.uuid[4:8]}", flush=True)
        if self.uuid == uuid("0210"):
            tell_bridge({"ooono_conn": False})

    @dbus.service.signal(PROPS, signature="sa{sv}as")
    def PropertiesChanged(self, interface, changed, invalidated):
        pass

    def notify(self) -> None:
        if self.notifying:
            self.PropertiesChanged(CHR_IF, {"Value": dbus.Array(
                [dbus.Byte(b) for b in self.value], signature="y")}, [])


class Service(dbus.service.Object):
    def __init__(self, bus, path, svc_uuid):
        super().__init__(bus, path)
        self.path, self.uuid = path, svc_uuid
        self.chars = []

    def properties(self):
        return {SVC_IF: {"UUID": self.uuid, "Primary": True}}


class Application(dbus.service.Object):
    def __init__(self, bus):
        super().__init__(bus, "/comand/ooono")
        self.services, self.beat, self.button = [], None, None
        layout = LAYOUT + [("fe59", [])]
        for i, (svc_uuid, chars) in enumerate(layout):
            svc = Service(bus, f"/comand/ooono/s{i}", svc_uuid)
            for j, (short, flags, value) in enumerate(chars):
                c = Characteristic(bus, f"{svc.path}/c{j}", svc.path, uuid(short), flags, value)
                svc.chars.append(c)
                if short == "0140":
                    self.beat = c
                if short == "0210":
                    self.button = c
            self.services.append(svc)
        # Firmware update: a stub, because the app subscribes to it. Writes do nothing.
        dfu = self.services[-1]
        dfu.chars.append(Characteristic(bus, f"{dfu.path}/c0", dfu.path,
                                        "8ec90003-f315-4f60-9fb8-838830daea50",
                                        ["write", "indicate"], ""))

    @dbus.service.method(OM, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        out = {}
        for s in self.services:
            out[dbus.ObjectPath(s.path)] = s.properties()
            for c in s.chars:
                out[dbus.ObjectPath(c.path)] = c.properties()
        return out


# ------------------------------------------------------------- advertising ----
def hci_command(sock: socket.socket, ocf: int, params: bytes = b"") -> int:
    """One LE controller command; returns its status."""
    opcode = (0x08 << 10) | ocf
    sock.send(struct.pack("<BHB", 1, opcode, len(params)) + params)
    until = time.monotonic() + 2
    while time.monotonic() < until:
        ev = sock.recv(260)
        if len(ev) >= 7 and ev[1] == 0x0E and struct.unpack("<H", ev[4:6])[0] == opcode:
            return ev[6]
    return -1


def open_hci() -> socket.socket:
    sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_RAW, socket.BTPROTO_HCI)
    sock.bind((0,))
    # Events only: type mask for HCI events, all event codes.
    sock.setsockopt(0, 2, struct.pack("<IIIHxx", 1 << 4, 0xFFFFFFFF, 0xFFFFFFFF, 0))
    sock.settimeout(2)
    return sock


def random_address(identity: str) -> bytes:
    """A static random address, the same on every start so the app knows it again."""
    addr = bytearray(hashlib.sha256(b"comand-ooono" + identity.encode()).digest()[:6])
    addr[5] |= 0xC0              # the two top bits mark a static random address
    return bytes(addr)


def start_advertising(sock: socket.socket, addr: bytes) -> None:
    hci_command(sock, 0x000A, b"\x00")
    name = NAME.encode()
    uu = bytes.fromhex(ADVERTISED.replace("-", ""))[::-1]
    data = bytes([2, 1, 6, 17, 7]) + uu + bytes([len(name) + 1, 9]) + name
    scan = bytes([len(name) + 1, 9]) + name
    steps = [
        ("address", 0x0005, addr),
        # 100 ms, connectable, own address random, all channels.
        ("params", 0x0006, struct.pack("<HHBBB6sBB", 0xA0, 0xA0, 0, 1, 0, bytes(6), 7, 0)),
        ("data", 0x0008, bytes([len(data)]) + data.ljust(31, b"\0")),
        ("scan", 0x0009, bytes([len(scan)]) + scan.ljust(31, b"\0")),
        ("enable", 0x000A, b"\x01"),
    ]
    for label, ocf, params in steps:
        status = hci_command(sock, ocf, params)
        if status != 0:
            print(f"advertising {label}: status {status}", file=sys.stderr, flush=True)


def main() -> int:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    objects = dbus.Interface(bus.get_object(BLUEZ, "/"), OM).GetManagedObjects()
    adapter = next(p for p, i in objects.items() if GATT_MGR in i)
    identity = str(objects[adapter]["org.bluez.Adapter1"]["Address"])
    app = Application(bus)
    loop = GLib.MainLoop()

    def registered():
        print("gatt registered", flush=True)

    def failed(err):
        print(f"gatt registration failed: {err}", file=sys.stderr, flush=True)
        loop.quit()

    dbus.Interface(bus.get_object(BLUEZ, adapter), GATT_MGR).RegisterApplication(
        app, {}, reply_handler=registered, error_handler=failed)

    tell_bridge({"ooono_conn": False})    # nobody is connected to a fresh start
    sock = open_hci()
    addr = random_address(identity)
    start_advertising(sock, addr)
    print("advertising as", NAME, ":".join(f"{b:02X}" for b in addr[::-1]), flush=True)

    def keep_advertising():
        # A connection stops advertising, and the phone may come back after
        # dropping: asking again is harmless when it is already on.
        hci_command(sock, 0x000A, b"\x01")
        return True

    def beat():
        if app.beat:
            app.beat.notify()        # the real box's heartbeat
        return True

    # The box's button, from ui/comand-keys.py: 01 00000000 on press, then
    # 02 <ms held, big-endian> on release. The app counts the presses.
    def release():
        app.button.value = bytes([2]) + (HOLD_MS).to_bytes(4, "big")
        app.button.notify()
        return False

    def press(source, _cond):
        source.recv(64)
        print(f"button pressed (app listening: {app.button.notifying})", flush=True)
        app.button.value = bytes([1, 0, 0, 0, 0])
        app.button.notify()
        GLib.timeout_add(HOLD_MS, release)
        return True

    keys = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    keys.bind(("127.0.0.1", 7341))
    GLib.io_add_watch(keys, GLib.IO_IN, press)

    GLib.timeout_add_seconds(5, keep_advertising)
    GLib.timeout_add_seconds(30, beat)
    loop.run()
    return 1


if __name__ == "__main__":
    sys.exit(main())
