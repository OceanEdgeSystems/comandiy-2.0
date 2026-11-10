#!/usr/bin/env python3
"""Tell the bridge when the phone starts and stops mirroring.

uxplay has no hook -- no --on-connect, no D-Bus, nothing to subscribe to. What
it does have is a log, so that is the signal: follow the service's journal and
turn two lines of it into a source change.

This matters because a phone that starts mirroring is the driver choosing a
source without having touched the panel. Until this existed, the radio carried
on underneath and the two played over each other.

Reading the journal rather than a file so there is nothing to rotate, and
`--since now` so a restart of this watcher does not replay an old session and
switch the car to a mirror that ended an hour ago.
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request

BRIDGE = "http://127.0.0.1:8080/cmd"
UNIT = "comand-mirror.service"

# uxplay prints this once the client's video stream is actually being decoded,
# which is later and more reliable than the connection request: a phone that
# merely browses receivers gets as far as connecting.
BEGIN = "Begin streaming to GStreamer video pipeline"
# And these when it goes. "Stopping..." covers the client ending the session;
# the mirror thread line covers it dropping off the network.
END = ("Stopping...", "raop_rtp_mirror->running is no longer true")


def tell(active: bool) -> None:
    body = json.dumps({"mirror": {"active": active}}).encode()
    req = urllib.request.Request(BRIDGE, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=5).read()
        print(f"mirror {'started' if active else 'ended'}", flush=True)
    except Exception as e:
        print(f"could not reach the bridge: {e}", file=sys.stderr, flush=True)


def main() -> int:
    p = subprocess.Popen(
        ["journalctl", "-f", "-n", "0", "--since", "now", "-u", UNIT,
         "-o", "cat"],
        stdout=subprocess.PIPE, text=True)
    active = False
    assert p.stdout is not None
    for line in p.stdout:
        line = line.strip()
        if BEGIN in line:
            if not active:
                active = True
                tell(True)
        elif any(e in line for e in END):
            # Only when we said it started. uxplay prints "Stopping..." on its
            # own shutdown too, and a restart with no client should not send
            # the car back to the menu.
            if active:
                active = False
                tell(False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
