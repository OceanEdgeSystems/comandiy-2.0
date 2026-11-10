#!/usr/bin/env python3
"""What the auxiliary battery is doing, from the BMS and the DC-DC charger.

Two devices, two very different ways of asking:

    Daly BMS        a GATT connection carrying its ordinary serial frames.
                    It only answers when asked, so this polls.
    Orion-Tr Smart  nothing. It shouts its state into the air every second or
                    so and we listen. No pairing, no connection, no airtime
                    held open beside the phone's audio.

That asymmetry is the whole design. Scanning is free and runs continuously;
the BMS link is the expensive one, so it is opened, asked its four questions
and closed again rather than held. There is one radio in this car and the
phone's music and calls are on it.

Settings live in /etc/default/comand-aux:

    VICTRON_KEY   32 hex characters, from VictronConnect: Product info ->
                  "Instant readout via Bluetooth" -> encryption key
    VICTRON_MAC   optional; without it, any Victron DC/DC converter whose
                  advertisement the key decrypts is taken to be ours
    DALY_MAC      optional; without it, the first device whose name looks
                  like a Daly module
    POLL_S        how often to ask the BMS, default 30

Usage:
    aux-battery.py            # watch and report
    aux-battery.py --once     # one pass, printed, for looking at what it sees
    aux-battery.py --scan     # list what is advertising, to find addresses
    aux-battery.py --set-soc 100
                              # overwrite the BMS's state of charge; stop
                              # the service first, the BMS takes one client
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import daly                                            # noqa: E402
import victron                                         # noqa: E402

BRIDGE = "http://127.0.0.1:8080/cmd"
STATE = "http://127.0.0.1:8080/state"
CONF = "/etc/default/comand-aux"

# Daly's Bluetooth modules are not consistent about which UART service they
# expose, so both known pairs are tried in turn. The write characteristic is
# the one we ask on; the notify characteristic is where frames come back.
DALY_SERVICES = (
    ("0000fff2-0000-1000-8000-00805f9b34fb", "0000fff1-0000-1000-8000-00805f9b34fb"),
    ("0000ffe2-0000-1000-8000-00805f9b34fb", "0000ffe1-0000-1000-8000-00805f9b34fb"),
)
DALY_NAME_HINTS = ("daly", "dl-", "smart bms")

# A reading older than this is not shown at all. A stale number about a
# battery is worse than an honest gap: it looks like the present.
STALE_S = 180


KEYS = ("VICTRON_KEY", "VICTRON_MAC", "VICTRON_KEYS", "DALY_MAC", "POLL_S")


def settings() -> dict:
    """Settings from the environment, falling back to reading the file.

    The environment first because that is where systemd puts them, having
    read the file as root. The file holds the charger's encryption key, so it
    is owned by root and mode 600 -- and this service runs as the session
    user, which cannot open it. Reading the file directly worked right up
    until the key went in and the file was locked down, at which point every
    setting silently became its default and the charger "had no key".
    """
    conf = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
    if conf.get("VICTRON_KEY") or conf.get("VICTRON_KEYS"):
        return conf
    try:
        with open(CONF) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                conf.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass
    return conf


def enabled() -> bool:
    """Whether the driver has let this service near the radio.

    Settings -> Bluetooth -> Battery monitor, which the host persists. Asked
    every pass rather than once at startup, so turning it on takes effect
    without restarting anything -- and turning it off stops the scanner and
    the polling within one cycle.

    A host that cannot be reached counts as off. This service exists to feed a
    screen; with nothing to feed it, holding the radio open would be all cost
    and no benefit.
    """
    try:
        with urllib.request.urlopen(STATE, timeout=5) as r:
            return bool(json.loads(r.read().decode()).get("ble"))
    except Exception:
        return False


def victron_keys(conf: dict) -> dict[str, bytes]:
    """Every Victron key configured, as address -> key.

    Two forms, because one device was one setting and several are a list:

        VICTRON_KEY / VICTRON_MAC   the original pair, still honoured
        VICTRON_KEYS                MAC=key,MAC=key,...  -- or bare keys,
                                    which match any device whose check byte
                                    agrees

    An address of "" means "whichever device this key decrypts", which is
    right until two Victrons in range share a first key byte.
    """
    keys: dict[str, bytes] = {}

    def add(mac: str, hexkey: str) -> None:
        try:
            key = bytes.fromhex(hexkey.strip())
        except ValueError:
            print(f"victron: {hexkey!r} is not hex", file=sys.stderr, flush=True)
            return
        if len(key) != 16:
            print(f"victron: a key is 32 hex characters, not {len(hexkey)}",
                  file=sys.stderr, flush=True)
            return
        keys[mac.strip().upper()] = key

    if conf.get("VICTRON_KEY"):
        add(conf.get("VICTRON_MAC", ""), conf["VICTRON_KEY"])
    for entry in (conf.get("VICTRON_KEYS", "") or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        mac, _, hexkey = entry.rpartition("=")
        add(mac, hexkey)
    return keys


def tell(payload: dict) -> None:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(BRIDGE, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"could not reach the bridge: {e}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------ daly ----
# The open link to the BMS, kept rather than made fresh each poll.
#
# It used to connect, ask, and disconnect every thirty seconds, on the
# reasoning that holding a link would compete with the phone's audio. That was
# wrong twice over. A held connection at a long interval is a few packets a
# second; repeatedly scanning and connecting is far more airtime. And this
# module stops advertising for a while after a client drops, so the very act
# of being polite made it invisible -- it would answer once, go quiet, and the
# next poll would report "not advertising" until it happened to come back.
_bms = {"client": None, "notify": None}


async def bms_disconnect() -> None:
    client = _bms["client"]
    _bms["client"] = _bms["notify"] = None
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception:
        pass


async def bms_connect(address: str, pause_scan) -> bool:
    """Open the link and subscribe, or say why not. Scanning stands aside.

    BlueZ will not open a connection while a discovery is running -- it
    answers InProgress -- so the scan is stopped for the connect and started
    again after. It stays running for the polling that follows, which is the
    part that happens every thirty seconds.
    """
    from bleak import BleakClient, BleakScanner

    await pause_scan(False)
    try:
        device = await BleakScanner.find_device_by_address(address, timeout=20)
        if device is None:
            print(f"BMS {address} is not advertising", file=sys.stderr, flush=True)
            return False
        client = BleakClient(device, timeout=20,
                             disconnected_callback=lambda _c: _bms.update(
                                 {"client": None, "notify": None}))
        await client.connect()
        for write_uuid, notify_uuid in DALY_SERVICES:
            try:
                await client.start_notify(notify_uuid, _on_notify)
            except Exception:
                continue                  # not this module's service
            _bms["client"] = client
            _bms["notify"] = (write_uuid, notify_uuid)
            print(f"BMS connected on {notify_uuid[4:8]}", flush=True)
            return True
        await client.disconnect()
        print("BMS has no UART service we know", file=sys.stderr, flush=True)
        return False
    except Exception as e:
        print(f"BMS connect: {type(e).__name__} {e}", file=sys.stderr, flush=True)
        await bms_disconnect()
        return False
    finally:
        await pause_scan(True)


_seen: list[bytes] = []
_buf = b""


def _on_notify(_handle, data: bytearray) -> None:
    # Frames are kept rather than decoded here. The cell voltages arrive three
    # per frame across several frames, so a reading is assembled from the pass
    # as a whole; decoding one at a time cannot see that.
    global _buf
    _buf += bytes(data)
    got, _buf = daly.frames(_buf)
    _seen.extend(got)


async def read_bms(address: str, pause_scan) -> dict:
    """Ask the four questions over the link, opening it if it is not up."""
    if _bms["client"] is None or not _bms["client"].is_connected:
        if not await bms_connect(address, pause_scan):
            return {}

    client = _bms["client"]
    write_uuid, _notify_uuid = _bms["notify"]
    _seen.clear()
    global _buf
    _buf = b""
    try:
        for command in daly.COMMANDS:
            await client.write_gatt_char(write_uuid, daly.request(command),
                                         response=False)
            # One at a time: these modules answer in order and drop questions
            # asked while they are still talking.
            await asyncio.sleep(0.4)
        await asyncio.sleep(1.5)          # the last answers are still coming
    except Exception as e:
        print(f"BMS read: {type(e).__name__} {e}", file=sys.stderr, flush=True)
        await bms_disconnect()

    return daly.readings(list(_seen))


async def find_bms(want: str) -> str:
    """The BMS's address, given or discovered by name."""
    if want:
        return want
    from bleak import BleakScanner
    for device in await BleakScanner.discover(timeout=8):
        name = (device.name or "").lower()
        if any(h in name for h in DALY_NAME_HINTS):
            print(f"BMS looks like {device.name} at {device.address}", flush=True)
            return device.address
    return ""


async def set_soc(address: str, percent: float) -> int:
    """Write the state of charge, then read it back to show it took."""
    if not address:
        print("no Daly BMS found", file=sys.stderr)
        return 1

    async def no_scan(_on: bool) -> None:
        return None

    before = (await read_bms(address, no_scan)).get("soc")
    if before is None:
        print("the BMS did not answer", file=sys.stderr)
        await bms_disconnect()
        return 1
    _seen.clear()
    write_uuid, _notify_uuid = _bms["notify"]
    await _bms["client"].write_gatt_char(write_uuid, daly.set_soc(percent),
                                         response=False)
    await asyncio.sleep(1.5)
    taken = daly.readings(list(_seen)).get("soc_set")
    after = (await read_bms(address, no_scan)).get("soc")
    await bms_disconnect()
    print(f"SOC {before} -> {after} (asked {percent}, "
          f"acknowledged: {taken})")
    return 0 if after is not None and abs(after - percent) < 0.5 else 1


# --------------------------------------------------------------- victron ----
class Victrons:
    """Every Victron in the car, from advertisements alone.

    One object rather than one per device, because they all arrive on the same
    scan and are told apart by the key that decrypts them. A key is tried only
    against advertisements whose check byte matches its first byte, so having
    several costs nothing and cannot cross-decrypt one device's record with
    another's key.

    Held rather than returned: advertisements arrive on their own schedule and
    the rest of this asks questions on ours.
    """

    def __init__(self, keys: dict[str, bytes]):
        # address (upper, or "" for any) -> key
        self.keys = keys
        self.readings: dict[str, dict] = {}
        self.at: dict[str, float] = {}
        self.complained: set[str] = set()

    def _candidates(self, address: str, check: int) -> list[bytes]:
        out = []
        for want, key in self.keys.items():
            if want and want != address:
                continue
            if key[0] != check:
                continue
            out.append(key)
        return out

    def advertisement(self, device, data) -> None:
        raw = (data.manufacturer_data or {}).get(victron.VICTRON_COMPANY_ID)
        if not raw or len(raw) < 9:
            # Four bytes and no record is what a Victron sends when Instant
            # Readout is switched off: it is advertising its identity and
            # nothing else, and no key will help.
            return
        raw = bytes(raw)
        address = device.address.upper()
        for key in self._candidates(address, raw[7]):
            try:
                name, got = victron.reading(raw, key)
            except (victron.WrongKey, ValueError):
                continue
            if name and got:
                self.readings[name] = got
                self.at[name] = time.time()
                return
        if address not in self.complained and not self._candidates(address, raw[7]):
            self.complained.add(address)
            print(f"victron {address}: no key here starts {raw[7]:02x}",
                  file=sys.stderr, flush=True)

    def fresh(self) -> dict:
        now = time.time()
        return {name: r for name, r in self.readings.items()
                if now - self.at.get(name, 0) <= STALE_S}


# ------------------------------------------------------------------ main ----
async def run(args) -> int:
    conf = settings()
    poll_s = float(conf.get("POLL_S", 30))

    keys = victron_keys(conf)
    charger = Victrons(keys) if keys else None
    if not keys:
        print(f"no Victron keys in {CONF}; nothing solar or DC-DC will be read",
              file=sys.stderr, flush=True)

    from bleak import BleakScanner

    if args.scan:
        for device in await BleakScanner.discover(timeout=12):
            marks = []
            if device.name:
                marks.append(device.name)
            print(f"{device.address}  {'  '.join(marks) or '(no name)'}")
        return 0

    bms_address = conf.get("DALY_MAC", "")

    if args.set_soc is not None:
        return await set_soc(await find_bms(bms_address), args.set_soc)

    scanner = None
    scanning = False

    async def scan(on: bool) -> None:
        nonlocal scanner, scanning
        if on == scanning or not charger:
            return
        if scanner is None:
            scanner = BleakScanner(detection_callback=charger.advertisement)
        await (scanner.start() if on else scanner.stop())
        scanning = on

    try:
        was_on = None
        while True:
            # Off means off: no scanning, no connecting, nothing on the air.
            # The phone's audio is the other thing on this radio and it is the
            # one that must not stutter.
            on = enabled()
            if on != was_on:
                print("battery monitor: " + ("on" if on else "off"), flush=True)
                was_on = on
            if not on:
                await scan(False)
                await bms_disconnect()
                if args.once:
                    print("the battery monitor is off "
                          "(Settings -> Bluetooth on the panel)")
                    return 0
                await asyncio.sleep(5)
                continue

            await scan(True)
            aux: dict = {}
            if not bms_address:
                # Discovery is radio traffic as well, so it waits until the
                # driver has switched this on rather than happening at boot.
                bms_address = await find_bms("")
                if not bms_address:
                    print("no Daly BMS found; will keep looking",
                          file=sys.stderr, flush=True)
            if bms_address:
                # The scanner stands aside. BlueZ will not open a connection
                # while a discovery is running -- it answers InProgress -- so
                # listening for the charger and asking the BMS cannot overlap.
                # The charger advertises every second or so and nothing is
                # lost by missing a few: what it reports changes on the scale
                # of a drive, not of a poll.
                try:
                    aux.update(await read_bms(bms_address, scan))
                except Exception as e:
                    print(f"BMS: {e}", file=sys.stderr, flush=True)
                    await bms_disconnect()
            # Said explicitly rather than left to the absence of numbers. A
            # battery page with the charger filled in and everything else
            # blank looks like a broken page; "no link" is a fact about the
            # car, and the two are worth telling apart from the driver's seat.
            aux["bms"] = bool(aux)

            if charger:
                aux.update(charger.fresh())

            if len(aux) > 1:
                aux["at"] = time.time()
                tell({"aux": aux})
                if args.once:
                    print(json.dumps(aux, indent=2))
            elif args.once:
                print("nothing to report: neither device answered")

            if args.once:
                return 0
            await asyncio.sleep(poll_s)
    finally:
        await scan(False)
        await bms_disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--set-soc", type=float, metavar="PERCENT")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
