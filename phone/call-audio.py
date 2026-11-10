#!/usr/bin/env python3
"""Wire the car's microphone and speakers into a Bluetooth call.

PipeWire brings up two nodes when a hands-free call starts: a sink that is the
phone's uplink -- what the far end hears -- and a source that is its downlink.
Nothing connects them to anything. A media player picks its own sink and a
recorder picks its own source, but a call is neither: it needs the car's
microphone sent to the phone and the phone's audio sent to the car, and no
application is asking for either.

So the links are made here, when a call starts, and dropped when it ends.

    USB microphone  ->  bluez sink    (the far end hears the car)
    bluez source    ->  car speakers  (the car hears the far end)

Made with pw-loopback rather than pw-link. A bare link between two nodes is
not a stream, and a node with no stream on it is a node PipeWire is entitled
to suspend: the far end could hear the car at the start of a call and not a
few seconds later, which is exactly what a suspended microphone sounds like.
A loopback is a real client at each end, and a running client is not idle.

Made and unmade rather than left standing, because the bluez nodes only exist
while a call does, and a loopback pointed at a node that is not there is a
loopback that never comes back when it is.

Answering on the handset takes the call's audio off Bluetooth, and its nodes go
with it. A loopback left without one is moved by the session manager to the default
device: the microphone into the car's speakers, at call gain, which howls. So the
loopbacks are not allowed to move, and --sync, run throughout the call, takes them
down when the nodes go and makes them again if the audio comes back to the car.

Usage:
    call-audio.py --start
    call-audio.py --sync
    call-audio.py --stop
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time


def run(cmd: list[str]) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return r.returncode, (r.stdout + r.stderr)
    except Exception as e:
        return 1, str(e)


def nodes(direction: str) -> list[str]:
    """Node names in one direction. `-o` is outputs (sources), `-i` inputs."""
    code, out = run(["pw-link", direction])
    if code != 0:
        return []
    # pw-link prints "node:port"; the node is what we link by.
    seen, order = set(), []
    for line in out.splitlines():
        name = line.strip().split(":")[0]
        if name and name not in seen:
            seen.add(name)
            order.append(name)
    return order


def pick(names: list[str], *want: str) -> str:
    for n in names:
        if all(w.lower() in n.lower() for w in want):
            return n
    return ""


def car_mic(names: list[str]) -> str:
    """The car's own microphone: a USB input that isn't the UMIK-1, which is a
    measurement mic for Settings > Sound and sits on the floor, not at the driver."""
    for n in names:
        if "alsa_input" in n and "usb" in n.lower() and "umik" not in n.lower():
            return n
    return ""


def default_sink() -> str:
    code, out = run(["pactl", "get-default-sink"])
    return out.strip() if code == 0 else ""


def default_source() -> str:
    code, out = run(["pactl", "get-default-source"])
    return out.strip() if code == 0 else ""


PIDS = "/run/user/@UID@/comand-call-audio.pids"
PINNED = "node.dont-fallback=true node.dont-reconnect=true"


def loopback(capture: str, playback: str) -> int:
    """One direction of the call, as a process that stays running."""
    if not capture or not playback:
        return 0
    try:
        p = subprocess.Popen(
            ["pw-loopback",
             "-i", f"target.object={capture} {PINNED}",
             "-o", f"target.object={playback} {PINNED}",
             # Named so the graph says what it is when something goes wrong at
             # the roadside rather than at a desk.
             "-n", f"call-{'up' if 'bluez' in playback else 'down'}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"loopback {capture} -> {playback} (pid {p.pid})", flush=True)
        return p.pid
    except Exception as e:
        print(f"loopback {capture} -> {playback}: {e}", file=sys.stderr)
        return 0


# Some lift for a mic 20 cm away. WebRTC's own gain control stays off: with it on,
# the far end described the voice as robotic. The extended filter alone is fine.
CALL_MIC_PERCENT = 130
CLEAN_ID = "/run/user/@UID@/comand-call-clean.id"


def clean_audio(mic: str, speakers: str) -> tuple[str, str]:
    """Road noise and the far end's own voice out of what the mic sends.

    WebRTC's noise suppression, gain control and echo cancellation, as PipeWire
    ships them. Returns the ends to use; the plain ones if this can't be loaded,
    because a noisy call is better than none.
    """
    code, out = run(["pactl", "load-module", "module-echo-cancel", "aec_method=webrtc",
                     f"source_master={mic}", f"sink_master={speakers}",
                     "source_name=call_clean_mic", "sink_name=call_clean_speakers",
                     'aec_args="webrtc.noise_suppression=true webrtc.gain_control=false '
                     'webrtc.extended_filter=true webrtc.high_pass_filter=true"'])
    if code != 0 or not out.strip().isdigit():
        print(f"no noise suppression: {out.strip()}", file=sys.stderr)
        return mic, speakers
    with open(CLEAN_ID, "w") as fh:
        fh.write(out.strip())
    print("noise suppression on", flush=True)
    return "call_clean_mic", "call_clean_speakers"


def drop_direct_links(bt_source: str) -> None:
    """The session manager also links the call straight to the speakers.

    That second path never reaches the echo canceller, so the mic sent the far
    end's own voice back to them. Done again after a moment, as the link can
    appear after the loopbacks do.
    """
    if not bt_source:
        return
    for _ in range(3):
        time.sleep(1)
        _, out = run(["pw-link", "-l"])
        port = None
        for line in out.splitlines():
            if not line.startswith(" "):
                port = line.strip()
            elif line.strip().startswith("|<- " + bt_source + ":") and port and port.startswith(("alsa_output", "comand_eq:")):
                run(["pw-link", "-d", line.strip()[4:], port])
                print(f"dropped direct link to {port}", flush=True)


def stop_all() -> None:
    """Take down whatever the last call left running."""
    try:
        with open(PIDS) as fh:
            pids = [int(x) for x in fh.read().split() if x.strip().isdigit()]
    except Exception:
        pids = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"stopped {pid}", flush=True)
        except ProcessLookupError:
            pass
        except Exception as e:
            print(f"stopping {pid}: {e}", file=sys.stderr)
    try:
        os.unlink(PIDS)
    except FileNotFoundError:
        pass
    try:
        with open(CLEAN_ID) as fh:
            run(["pactl", "unload-module", fh.read().strip()])
        os.unlink(CLEAN_ID)
    except FileNotFoundError:
        pass


def wait_for_bt(seconds: float) -> tuple[list[str], list[str]]:
    """Wait for the call's audio nodes to appear.

    They do not exist when the call does. The phone answers, then the audio
    channel is negotiated, and only then does PipeWire have anything to link.
    Looking once at the moment of dialling found nothing and gave up, which is
    why there was no ringing tone in the car and nothing for the far end to
    hear.
    """
    deadline = time.monotonic() + seconds
    while True:
        outs, ins = nodes("-o"), nodes("-i")
        if pick(ins, "bluez_output") or pick(outs, "bluez_input"):
            return outs, ins
        if time.monotonic() >= deadline:
            return outs, ins
        time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stop", action="store_true", help="take the links down")
    ap.add_argument("--sync", action="store_true",
                    help="match the links to where the call's audio is now")
    ap.add_argument("--wait", type=float, default=12.0,
                    help="how long to wait for the call's audio to appear")
    args = ap.parse_args()
    remove = args.stop

    if remove:
        stop_all()
        mic = car_mic(nodes("-o"))
        if mic:
            run(["pactl", "set-source-volume", mic, "100%"])
        return 0

    outs, ins = (nodes("-o"), nodes("-i")) if args.sync else wait_for_bt(args.wait)
    # Only a call has a Bluetooth sink. A Bluetooth source alone can be the phone's music.
    on_bluetooth = bool(pick(ins, "bluez_output"))
    if args.sync:
        wired = os.path.exists(PIDS)
        if wired and not on_bluetooth:
            print("the call left Bluetooth: car audio off", flush=True)
            stop_all()
            mic = car_mic(outs)
            if mic:
                run(["pactl", "set-source-volume", mic, "100%"])
        if wired or not on_bluetooth:
            return 0
        print("the call is on Bluetooth: car audio on", flush=True)

    # The phone's ends of the call, by their exact prefixes. Matching "bluez"
    # alone found bluez_midi.server -- BlueZ's MIDI node, which is always
    # there and is not a telephone -- and cheerfully tried to send the far end
    # to it.
    bt_sink = pick(ins, "bluez_output")     # uplink: we write to it
    bt_source = pick(outs, "bluez_input")   # downlink: we read from it

    # The car's ends. The defaults are used rather than a name in a config
    # file: they are what every other sound in this car already goes through,
    # and a call that came out of a different speaker would be a surprise.
    # The USB audio device first, and the defaults only if it is absent. In
    # this car USB is the path to the amplifier and to the microphone; HDMI is
    # a bench monitor that happens to accept audio, and asking pactl from a
    # service can return it because the default is a property of a session
    # this does not run in.
    mic = car_mic(outs)
    # The output chosen in Settings > Sound is the default sink, set by the
    # bridge; the first USB output only if that can't be read.
    speakers = default_sink()
    if speakers not in ins or not (speakers.startswith("alsa_output") or speakers == "comand_eq"):
        speakers = pick(ins, "alsa_output", "usb") or pick(ins, "alsa_output")
    if not mic:
        mic = default_source()
        if mic not in outs or "monitor" in mic or "umik" in mic.lower():
            mic = pick(outs, "alsa_input")
    # A monitor is not a microphone. Sending one to the phone would send the
    # car's own output back to the far end as an echo.
    if "monitor" in mic or "umik" in mic.lower():
        mic = car_mic(outs) or ""

    # The far end heard the car quietly. For calls only: voice commands are
    # happy with the mic as it is, and too much gain would make them guess.
    if mic:
        run(["pactl", "set-source-volume", mic, f"{CALL_MIC_PERCENT}%"])
    print(f"mic={mic or '-'}  speakers={speakers or '-'}", flush=True)
    print(f"bt sink={bt_sink or '-'}  bt source={bt_source or '-'}", flush=True)

    if not bt_sink and not bt_source:
        print("no call audio nodes; nothing to do", file=sys.stderr)
        return 1
    if not mic:
        # Worth saying plainly: the far end hearing nothing is the symptom, and
        # "no microphone" is not something the driver can deduce from it.
        print("no microphone found; the far end will hear nothing",
              file=sys.stderr)

    # Anything left from a previous call goes first: two loopbacks into the
    # same sink is the far end hearing the car twice, slightly apart.
    stop_all()
    if mic and speakers:
        mic, speakers = clean_audio(mic, speakers)
    pids = [loopback(mic, bt_sink), loopback(bt_source, speakers)]
    with open(PIDS, "w") as fh:
        fh.write(" ".join(str(p) for p in pids if p))
    drop_direct_links(bt_source)
    return 0


if __name__ == "__main__":
    sys.exit(main())
