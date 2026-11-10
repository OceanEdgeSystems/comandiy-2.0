#!/usr/bin/env python3
"""Feed the car's u-blox fix into Android running under Waydroid.

Waydroid has no location HAL: Android in the container sees no GNSS receiver at
all, so a navigation app gets nothing. This closes that gap from the host side.

The mechanism is Android's own test-provider interface, reached over
`waydroid shell`. No app is installed inside Android and nothing is patched:

    cmd appops set --uid 0 MOCK_LOCATION allow
    cmd location providers add-test-provider gps
    cmd location providers set-test-provider-enabled gps true
    cmd location providers set-test-provider-location gps --location LAT,LON

The app-op grant is required -- the shell runs as uid 0, and without it every
call fails with `SecurityException: not allowed to perform MOCK_LOCATION`.

What makes this worth doing rather than writing an Android mock-location app:
the injected fix lands on the **fused** provider as well as `gps`, and fused is
what Play Services apps -- Sygic included -- actually read.

    last location=Location[gps   52.229700,21.012200 hAcc=5.0 mock]
    last location=Location[fused 52.229700,21.012200 hAcc=5.0 mock]

PORT OWNERSHIP
The receiver is single-owner. While LIVI is running it holds /dev/ttyACM0 and
this script cannot read it; that is expected and is why LIVI is being replaced
rather than run alongside. Once our own daemon owns the receiver, this stops
reading the tty directly and instead takes the fix from the daemon, which fans
it out to the UI, to CarPlay and to here.

Usage:
    gps-bridge.py                     # read the u-blox, push at 1 Hz
    gps-bridge.py --self-test         # synthetic track, no receiver needed
    gps-bridge.py --device /dev/ttyX  # a different receiver
    gps-bridge.py --once              # inject one fix and exit
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
import time

# The by-id path, not /dev/ttyACM0: the bare name renumbers if another CDC
# device appears, and on this box the STM32 is also a CDC device.
DEFAULT_DEVICE = (
    "/dev/serial/by-id/"
    "usb-u-blox_AG_-_www.u-blox.com_u-blox_7_-_GPS_GNSS_Receiver-if00"
)
DEFAULT_BAUD = 9600
PROVIDER = "gps"


def shell(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run one command inside the Waydroid Android container."""
    return subprocess.run(
        ["sudo", "waydroid", "shell", "--", *args],
        capture_output=True, text=True, check=check,
    )


def setup_provider() -> None:
    """Grant the app-op and register the test provider. Idempotent."""
    shell("cmd", "appops", "set", "--uid", "0", "MOCK_LOCATION", "allow")
    shell("cmd", "location", "providers", "add-test-provider", PROVIDER)
    shell("cmd", "location", "providers", "set-test-provider-enabled", PROVIDER, "true")


def push(lat: float, lon: float, accuracy: float = 5.0) -> bool:
    r = shell(
        "cmd", "location", "providers", "set-test-provider-location", PROVIDER,
        "--location", f"{lat:.6f},{lon:.6f}", "--accuracy", f"{accuracy:.1f}",
    )
    err = (r.stdout + r.stderr)
    if "Exception" in err:
        # The provider is dropped when the Android session restarts, so a
        # failure usually means "re-register", not "give up".
        print("push failed, re-registering provider", file=sys.stderr)
        setup_provider()
        return False
    return True


# ------------------------------------------------------------------ NMEA ----

def _deg(raw: str, hemi: str) -> float | None:
    """NMEA ddmm.mmmm -> signed decimal degrees."""
    if not raw or not hemi:
        return None
    dot = raw.find(".")
    if dot < 3:
        return None
    deg = float(raw[: dot - 2])
    minutes = float(raw[dot - 2 :])
    val = deg + minutes / 60.0
    return -val if hemi in ("S", "W") else val


def parse(line: str) -> tuple[float, float, float] | None:
    """Return (lat, lon, accuracy) from a fixed GGA or RMC sentence."""
    if not line.startswith("$"):
        return None
    f = line.strip().split(",")
    talker = f[0][3:]

    if talker == "GGA" and len(f) > 8:
        if f[6] in ("", "0"):          # fix quality 0 = no fix
            return None
        lat, lon = _deg(f[2], f[3]), _deg(f[4], f[5])
        try:
            hdop = float(f[8]) if f[8] else 1.0
        except ValueError:
            hdop = 1.0
        # HDOP is not metres. 5 m per unit is the usual rough conversion and is
        # honest enough for a mock accuracy field.
        return (lat, lon, max(3.0, hdop * 5.0)) if lat is not None and lon is not None else None

    if talker == "RMC" and len(f) > 6:
        if f[2] != "A":                # A = valid, V = warning
            return None
        lat, lon = _deg(f[3], f[4]), _deg(f[5], f[6])
        return (lat, lon, 10.0) if lat is not None and lon is not None else None

    return None


# ------------------------------------------------------------------ modes ----

def run_gpsd(host: str, port: int, interval: float) -> int:
    """Take the fix from gpsd rather than from the receiver directly.

    The receiver is single-owner, and it now belongs to gpsd -- which is what
    lets Navit, this, and anything else added later all see the same fix.
    Reading the tty here as well would mean whichever started first got the
    receiver and the other silently got nothing, which is exactly how Sygic
    ended up with no GPS at all: gpsd held the port, this could not open it,
    and Android was never told anything.

    It also stops this having to know how the receiver is configured. Ours
    speaks UBX binary at 115200, not the NMEA at 9600 this file was written
    against; gpsd works that out and hands over JSON either way.
    """
    import json as _json
    import socket

    print(f"reading gpsd at {host}:{port}, pushing every {interval:.1f}s")
    last = 0.0
    while True:
        try:
            sk = socket.create_connection((host, port), timeout=10)
            sk.sendall(b'?WATCH={"enable":true,"json":true};\n')
            buf = b""
            with sk:
                while True:
                    chunk = sk.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            d = _json.loads(line)
                        except ValueError:
                            continue
                        if d.get("class") != "TPV" or d.get("mode", 0) < 2:
                            continue        # no fix yet; nothing to say
                        lat, lon = d.get("lat"), d.get("lon")
                        if lat is None or lon is None:
                            continue
                        now = time.monotonic()
                        if now - last < interval:
                            continue
                        last = now
                        # eph is the horizontal error estimate in metres when
                        # gpsd has one; a floor because Android treats a very
                        # small accuracy as a claim, not a hope.
                        acc = float(d.get("eph") or 0) or 10.0
                        ok = push(lat, lon, max(3.0, acc))
                        print(f"{'ok ' if ok else 'ERR'} {lat:.6f},{lon:.6f}  ±{acc:.0f}m",
                              flush=True)
        except Exception as e:
            # A car loses and regains everything; reconnecting quietly is the
            # whole job of a daemon like this.
            print(f"gpsd: {e}; retrying", file=sys.stderr, flush=True)
            time.sleep(5)

def run_serial(device: str, baud: int, interval: float) -> int:
    try:
        import serial  # python3-serial
    except ImportError:
        print("pyserial missing: apt install python3-serial", file=sys.stderr)
        return 2

    try:
        port = serial.Serial(device, baud, timeout=2)
    except Exception as e:
        print(f"cannot open {device}: {e}", file=sys.stderr)
        print("if LIVI is running it owns this port; only one reader is possible",
              file=sys.stderr)
        return 2

    print(f"reading {device} at {baud}, pushing every {interval:.1f}s")
    last = 0.0
    with port:
        while True:
            raw = port.readline().decode("ascii", "replace")
            fix = parse(raw)
            if not fix:
                continue
            now = time.monotonic()
            if now - last < interval:
                continue
            last = now
            lat, lon, acc = fix
            ok = push(lat, lon, acc)
            print(f"{'ok ' if ok else 'ERR'} {lat:.6f},{lon:.6f}  ±{acc:.0f}m")


def run_self_test(interval: float) -> int:
    """Drive a slow circle so a map visibly moves, with no receiver attached."""
    lat0, lon0, r = 52.2297, 21.0122, 0.004
    print("self-test: circling central Warsaw; ^C to stop")
    i = 0
    while True:
        a = (i % 120) / 120.0 * 2 * math.pi
        lat = lat0 + r * math.sin(a)
        lon = lon0 + r * math.cos(a) * 1.6      # rough cos(lat) correction
        t0 = time.monotonic()
        ok = push(lat, lon, 5.0)
        dt = (time.monotonic() - t0) * 1000
        print(f"{'ok ' if ok else 'ERR'} {lat:.6f},{lon:.6f}  push {dt:.0f}ms")
        i += 1
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    # gpsd by default: the receiver has one owner and it is not this.
    ap.add_argument("--gpsd", default="127.0.0.1:2947",
                    help="gpsd to read from, or 'no' to open the receiver directly")
    ap.add_argument("--device", default=DEFAULT_DEVICE)
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between pushes (default 1)")
    ap.add_argument("--self-test", action="store_true",
                    help="synthetic moving track, no receiver needed")
    ap.add_argument("--once", action="store_true", help="inject one fix and exit")
    args = ap.parse_args()

    setup_provider()

    if args.once:
        ok = push(52.2297, 21.0122)
        print("injected" if ok else "failed")
        return 0 if ok else 1
    try:
        if args.self_test:
            return run_self_test(args.interval)
        if args.gpsd and args.gpsd != "no":
            host, _, port = args.gpsd.partition(":")
            return run_gpsd(host or "127.0.0.1", int(port or 2947), args.interval)
        return run_serial(args.device, args.baud, args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    sys.exit(main())
