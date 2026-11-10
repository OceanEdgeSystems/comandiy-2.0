#!/usr/bin/env python3
"""The car's sound correction: one PipeWire filter-chain in front of the output.

    players -> comand_eq (a sink, the default) -> comand_eq_out -> chosen output

Each channel gets a gain, a delay and BANDS peaking filters. Flat (every band
at 0 dB, gain 1, no delay) when the correction is off or not calibrated, so the
path is the same either way and switching is a live change, not a restart.

The calibration result lives in PROFILE:

    {"channels": {"FL": {"gain_db": -2.0, "delay_ms": 0.0,
                         "bands": [{"f": 63.0, "q": 4.0, "g": -6.0}, ...]},
                  "FR": {...}}}

Volume is not here: it stays on the hardware output, so that if this stage is
ever missing the sound falls back to the output at the same volume, not at 100%.

Usage:
    comand-eq.py --write --target SINK [--off]   write the config, restart if it changed
    comand-eq.py --live [--off]                  push the values to the running stage
"""

from __future__ import annotations

import argparse
import cmath
import json
import math
import os
import subprocess
import sys

BANDS = 10
CONF = os.path.expanduser("~/.config/pipewire/filter-chain.conf.d/comand-eq.conf")
PROFILE = os.path.expanduser("~/.config/comand/sound/eq.json")
# The driver's own settings (balance, fader, bass, treble, EQ), kept by the
# bridge in its settings file under "sound". Applied whether or not the
# calibration is on: they are taste, not correction.
SETTINGS = os.path.expanduser("~/.config/comand/settings.json")
USER_EQ = [60.0, 250.0, 1000.0, 4000.0, 12000.0]
BASS_F, TREBLE_F = 100.0, 10000.0
LEFT, RIGHT = {"FL", "RL", "SL"}, {"FR", "RR", "SR"}
FRONT, REAR = {"FL", "FR", "FC"}, {"RL", "RR", "SL", "SR"}
NODE = "comand_eq"
OUT_NODE = "comand_eq_out"
# Channel names in PipeWire's terms, and the short prefix used in the graph.
CHANNELS = {"FL": "l", "FR": "r", "RL": "rl", "RR": "rr", "FC": "c", "LFE": "sub",
            "SL": "sl", "SR": "sr"}
ENV = dict(os.environ, XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR", "/run/user/@UID@"),
           DBUS_SESSION_BUS_ADDRESS=os.environ.get("DBUS_SESSION_BUS_ADDRESS",
                                                   "unix:path=/run/user/@UID@/bus"))


def run(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=ENV)
        return r.returncode, r.stdout + r.stderr
    except Exception as e:  # noqa: BLE001 - reported by the caller
        return 1, str(e)


def load_profile() -> dict:
    try:
        with open(PROFILE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def load_user() -> dict:
    try:
        with open(SETTINGS) as fh:
            return dict(json.load(fh).get("sound") or {})
    except (OSError, ValueError):
        return {}


def biquad_db(kind: str, f0: float, q: float, g: float, f: float, rate: float = 48000) -> float:
    """RBJ cookbook response in dB, as PipeWire's bq_* compute it."""
    a = 10 ** (g / 40)
    w0 = 2 * math.pi * f0 / rate
    cw, al = math.cos(w0), math.sin(w0) / (2 * q)
    if kind == "peaking":
        b = (1 + al * a, -2 * cw, 1 - al * a)
        d = (1 + al / a, -2 * cw, 1 - al / a)
    else:
        sa = 2 * math.sqrt(a) * al
        if kind == "lowshelf":
            b = (a * ((a + 1) - (a - 1) * cw + sa), 2 * a * ((a - 1) - (a + 1) * cw),
                 a * ((a + 1) - (a - 1) * cw - sa))
            d = ((a + 1) + (a - 1) * cw + sa, -2 * ((a - 1) + (a + 1) * cw),
                 (a + 1) + (a - 1) * cw - sa)
        else:
            b = (a * ((a + 1) + (a - 1) * cw + sa), -2 * a * ((a - 1) + (a + 1) * cw),
                 a * ((a + 1) + (a - 1) * cw - sa))
            d = ((a + 1) - (a - 1) * cw + sa, 2 * ((a - 1) - (a + 1) * cw),
                 (a + 1) - (a - 1) * cw - sa)
    z = cmath.exp(-1j * 2 * math.pi * f / rate)
    h = (b[0] + b[1] * z + b[2] * z * z) / (d[0] + d[1] * z + d[2] * z * z)
    return 20 * math.log10(max(abs(h), 1e-9))


def side_db(n: int) -> float | None:
    """How far one side comes down for a balance or fader of n steps (1..10)
    towards the other side; None is off."""
    if n <= 0:
        return 0.0
    return None if n >= 10 else -2.5 * n


def tone(user: dict) -> list[tuple[str, float, float, float]]:
    """(kind, freq, q, gain) for bass, treble and the five EQ bands."""
    eq = list(user.get("eq") or [0] * len(USER_EQ)) + [0] * len(USER_EQ)
    return ([("lowshelf", BASS_F, 0.707, float(user.get("bass", 0))),
             ("highshelf", TREBLE_F, 0.707, float(user.get("treble", 0)))]
            + [("peaking", f, 1.0, float(eq[i])) for i, f in enumerate(USER_EQ)])


def headroom(filters: list[tuple[str, float, float, float]]) -> float:
    """The largest boost the tone settings add at any frequency, in dB: taken
    off the level, so turning the bass up can't clip."""
    worst = 0.0
    for k in range(120):
        f = 20 * (1000 ** (k / 119))
        worst = max(worst, sum(biquad_db(kind, f0, q, g, f) for kind, f0, q, g in filters))
    return worst


def values(channels: list[str], on: bool) -> dict[str, float]:
    """Every control in the graph, as "node:Control" -> value."""
    prof = (load_profile().get("channels") or {}) if on else {}
    user = load_user()
    filters = tone(user)
    lift = headroom(filters)
    bal, fad = int(user.get("balance", 0)), int(user.get("fader", 0))
    out: dict[str, float] = {}
    for ch in channels:
        p = CHANNELS[ch]
        c = prof.get(ch) or {}
        # Balance: negative is towards the left, so the right side comes down.
        db: float | None = 0.0
        for dial, low_side, high_side in ((bal, LEFT, RIGHT), (fad, FRONT, REAR)):
            cut = side_db(abs(dial))
            if (dial < 0 and ch in high_side) or (dial > 0 and ch in low_side):
                db = None if cut is None or db is None else db + cut
        out[f"u{p}:Mult"] = 0.0 if db is None else 10 ** ((db - lift) / 20)
        for i, (kind, f0, q, g) in enumerate(filters):
            out[f"t{p}{i}:Freq"], out[f"t{p}{i}:Q"], out[f"t{p}{i}:Gain"] = f0, q, g
        out[f"g{p}:Mult"] = 10 ** (float(c.get("gain_db", 0.0)) / 20)
        out[f"d{p}:Delay (s)"] = max(0.0, float(c.get("delay_ms", 0.0))) / 1000
        bands = list(c.get("bands") or [])[:BANDS]
        for i in range(BANDS):
            b = bands[i] if i < len(bands) else {}
            out[f"{p}{i + 1}:Freq"] = float(b.get("f", 1000.0))
            out[f"{p}{i + 1}:Q"] = float(b.get("q", 1.0))
            out[f"{p}{i + 1}:Gain"] = float(b.get("g", 0.0))
    return out


def conf(target: str, channels: list[str], on: bool) -> str:
    v = values(channels, on)
    nodes, links, ins, outs = [], [], [], []

    def ctl(name: str, keys: list[str]) -> str:
        return " ".join(f'"{k}" = {v[f"{name}:{k}"]:.6g}' for k in keys)

    for ch in channels:
        p = CHANNELS[ch]
        nodes.append(f'{{ type = builtin name = g{p} label = linear control = {{ {ctl("g" + p, ["Mult"])} "Add" = 0 }} }}')
        nodes.append(f'{{ type = builtin name = u{p} label = linear control = {{ {ctl("u" + p, ["Mult"])} "Add" = 0 }} }}')
        nodes.append(f'{{ type = builtin name = d{p} label = delay config = {{ "max-delay" = 0.05 }} '
                     f'control = {{ {ctl("d" + p, ["Delay (s)"])} }} }}')
        links.append(f'{{ output = "g{p}:Out" input = "u{p}:In" }}')
        links.append(f'{{ output = "u{p}:Out" input = "d{p}:In" }}')
        prev = f"d{p}"
        for i, (kind, _f, _q, _g) in enumerate(tone({})):
            n = f"t{p}{i}"
            nodes.append(f'{{ type = builtin name = {n} label = bq_{kind} control = {{ {ctl(n, ["Freq", "Q", "Gain"])} }} }}')
            links.append(f'{{ output = "{prev}:Out" input = "{n}:In" }}')
            prev = n
        for i in range(1, BANDS + 1):
            n = f"{p}{i}"
            nodes.append(f'{{ type = builtin name = {n} label = bq_peaking control = {{ {ctl(n, ["Freq", "Q", "Gain"])} }} }}')
            links.append(f'{{ output = "{prev}:Out" input = "{n}:In" }}')
            prev = n
        ins.append(f'"g{p}:In"')
        outs.append(f'"{prev}:Out"')
    pos = " ".join(channels)
    nl = "\n          "
    return f"""# Written by comand-eq.py; Settings > Sound. Do not edit: it is rewritten.
context.modules = [
  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = "COMAND sound"
      media.name = "COMAND sound"
      filter.graph = {{
        nodes = [
          {nl.join(nodes)}
        ]
        links = [
          {nl.join(links)}
        ]
        inputs = [ {" ".join(ins)} ]
        outputs = [ {" ".join(outs)} ]
      }}
      audio.channels = {len(channels)}
      audio.position = [ {pos} ]
      capture.props = {{
        node.name = "{NODE}"
        media.class = Audio/Sink
      }}
      playback.props = {{
        node.name = "{OUT_NODE}"
        node.passive = true
        stream.dont-remix = true
        target.object = "{target}"
      }}
    }}
  }}
]
"""


def node_id(name: str) -> int | None:
    code, out = run(["pw-dump"], timeout=10)
    try:
        for o in json.loads(out) if code == 0 else []:
            if o.get("type", "").endswith("Node") and \
                    o.get("info", {}).get("props", {}).get("node.name") == name:
                return int(o["id"])
    except ValueError:
        pass
    return None


def channels_of(target: str) -> list[str]:
    """The output's channels, as far as this stage handles them. Stereo for now."""
    return ["FL", "FR"]


def save_conf(text: str) -> None:
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF + ".tmp", "w") as fh:
        fh.write(text)
    os.replace(CONF + ".tmp", CONF)


def write(target: str, on: bool) -> int:
    chans = channels_of(target)
    text = conf(target, chans, on)
    try:
        with open(CONF) as fh:
            same = fh.read() == text
    except OSError:
        same = False
    if same and node_id(NODE) is not None:
        print("unchanged", flush=True)
        return 0
    save_conf(text)
    code, out = run(["systemctl", "--user", "restart", "filter-chain.service"], timeout=20)
    print(f"written, filter-chain restarted ({code}) {out.strip()}", flush=True)
    return code


def live(on: bool, target: str = "") -> int:
    """Values to the running stage; the config is rewritten to match, without a
    restart, so the next start comes up the same."""
    if target:
        save_conf(conf(target, channels_of(target), on))
    nid = node_id(NODE)
    if nid is None:
        print("no comand_eq node", file=sys.stderr)
        return 1
    v = values(channels_of(""), on)
    params = " ".join(f'"{k}" {val:.6g}' for k, val in v.items())
    code, out = run(["pw-cli", "s", str(nid), "Props", f"{{ params = [ {params} ] }}"])
    print(f"live {'on' if on else 'off'} ({code})", flush=True)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--target", default="")
    ap.add_argument("--off", action="store_true", help="flat, whatever the profile says")
    a = ap.parse_args()
    if a.write:
        if not a.target:
            print("--write needs --target", file=sys.stderr)
            return 2
        return write(a.target, not a.off)
    if a.live:
        return live(not a.off, a.target)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
