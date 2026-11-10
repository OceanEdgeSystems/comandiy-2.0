#!/usr/bin/env python3
"""Sleep the L560 a while after the car's power goes away.

The laptop battery carries the head unit and the Home Assistant VM through a
stop: on losing external power it stays fully awake for HOLD seconds, then
suspends to RAM. The VM freezes and resumes with the host, so it needs no
shutdown. A critically low battery sleeps early, because a dead battery is the
one hard power cut this is here to avoid.

Woken on battery (lid, power button, something else), it does not grant the
whole hour again: it sleeps after REWAKE_HOLD.
"""
import argparse
import glob
import os
import subprocess
import time


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hold", type=int, default=3600, help="seconds awake after power loss")
    p.add_argument("--rewake-hold", type=int, default=600, help="seconds awake after waking on battery")
    p.add_argument("--critical", type=int, default=8, help="sleep at or below this battery percent")
    p.add_argument("--poll", type=float, default=5)
    p.add_argument("--sysfs", default="/sys/class/power_supply")
    p.add_argument("--dry-run", action="store_true", help="log the suspend, do not do it")
    p.add_argument("--max-sleeps", type=int, default=0, help="exit after this many suspends (testing)")
    a = p.parse_args()

    ac = glob.glob(os.path.join(a.sysfs, "AC*/online"))[0]
    bat = glob.glob(os.path.join(a.sysfs, "BAT*/capacity"))[0]
    lost_at = None    # monotonic-ish wall time when power went away
    hold = a.hold
    sleeps = 0

    while True:
        online = read(ac, "1") == "1"
        now = time.time()
        if online:
            if lost_at is not None:
                print("external power back", flush=True)
            lost_at, hold = None, a.hold
        else:
            pct = int(read(bat, "100") or 100)
            if lost_at is None:
                lost_at = now
                print(f"external power lost, battery {pct}%, sleeping in {hold} s", flush=True)
            if now - lost_at >= hold or pct <= a.critical:
                print(f"suspending: {int(now - lost_at)} s on battery, {pct}%", flush=True)
                if a.dry_run:
                    print("(dry run)", flush=True)
                else:
                    subprocess.run(["systemctl", "suspend"], check=False)
                    # systemctl returns as the suspend starts: wait for it to happen.
                    time.sleep(10)
                sleeps += 1
                if a.max_sleeps and sleeps >= a.max_sleeps:
                    return
                lost_at, hold = time.time(), a.rewake_hold
                print(f"awake again, sleeping in {hold} s unless power returns", flush=True)
        time.sleep(a.poll)


if __name__ == "__main__":
    main()
