#!/usr/bin/env python3
"""Settings > Sound > Calibrate: measure the car with the UMIK-1 and fit a correction.

One recording, every channel in turn: a sweep on one speaker, a pause, the
next. A single recording because the speakers' arrival times are compared, and
two separately started recordings carry their own start jitter.

    sweep -> output (not through the correction) -> cabin -> UMIK-1
          -> deconvolve -> impulse response per speaker
          -> spectrum, less the UMIK's own calibration, smoothed
          -> against a car target: up to BANDS peaking filters, a level, a delay

Mostly cuts. A cabin's peaks are standing waves and come down cleanly; its dips
are cancellations, and boosting into one only spends amplifier power on a null
that stays where it is. So boosts are small, wide, and only where the dip is.

Writes comand-eq.py's profile, and the measurement next to it for a graph.

A preset can be measured at several seats ("Front seats", "All seats"). Each
seat is one recording; per speaker the seats' spectra are averaged on power
and their arrival times averaged, so the correction suits all of them rather
than one head. Only one seat lines the speakers' timing up exactly.

Progress is printed one JSON object per line, for the bridge:
    {"step": "place", "position": "driver", "pos_index": 0, "pos_count": 2}
        waits for a line on stdin (the driver pressing "ready")
    {"step": "countdown", "s": 5, ...}
    {"step": "measuring", "channel": "FL", "index": 0, "count": 2, "ch_progress": 0.4,
     "progress": 0.2, "position": ..., "pos_index": ..., "pos_count": ...}
    {"step": "analysing"} ... {"step": "done"} / {"step": "error", "msg": ...}

Usage:
    comand-cal.py --hw SINK --mic SOURCE --cal UMIK.txt [--channels FL,FR]
                  [--positions driver,passenger] [--out DIR] [--name "Front seats"]
    comand-cal.py --simulate      fit a made-up car, no sound
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np
from scipy.signal import fftconvolve

RATE = 48000
F0, F1 = 20.0, 20000.0
SWEEP_S = 6.0
GAP_S = 2.5            # long enough for the cabin to go quiet and the next sweep not to land in the tail
LEVEL = 0.25           # -12 dBFS; the driver's volume sets how loud that is
BANDS = 10
FIT_LO, FIT_HI = 30.0, 2000.0     # a cabin's problems are below here; above, the mic's position decides
BOOST_LO, BOOST_HI, BOOST_MAX = 45.0, 1000.0, 3.0
SOUND_DIR = os.path.expanduser("~/.config/comand/sound")
COUNTDOWN_S = 5        # after "ready": time to step back from the mic and keep still


def say(**kw) -> None:
    print(json.dumps(kw), flush=True)


# ----------------------------------------------------------------- signals --
def sweep() -> tuple[np.ndarray, np.ndarray]:
    """Farina's exponential sweep and its inverse filter."""
    n = int(SWEEP_S * RATE)
    t = np.arange(n) / RATE
    k = np.log(F1 / F0)
    s = np.sin(2 * np.pi * F0 * SWEEP_S / k * (np.exp(t * k / SWEEP_S) - 1))
    fade = int(0.05 * RATE)
    s[:fade] *= np.hanning(2 * fade)[:fade]
    s[-fade:] *= np.hanning(2 * fade)[fade:]
    inv = s[::-1] * np.exp(-t * k / SWEEP_S)
    # Scaled so a sweep straight through comes back as an impulse of height 1.
    inv /= np.max(np.abs(fftconvolve(s, inv)))
    return s * LEVEL, inv


def program(channels: list[str]) -> tuple[np.ndarray, list[int]]:
    """The file to play: channel i's sweep starts at starts[i]."""
    s, _ = sweep()
    lead = int(0.5 * RATE)
    seg = len(s) + int(GAP_S * RATE)
    out = np.zeros((lead + seg * len(channels), len(channels)))
    starts = []
    for i in range(len(channels)):
        a = lead + i * seg
        out[a:a + len(s), i] = s
        starts.append(a)
    return out, starts


def write_wav(path: str, x: np.ndarray) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def read_wav(path: str) -> np.ndarray:
    with wave.open(path) as w:
        ch, width = w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width == 4:
        x = np.frombuffer(raw, "<i4").astype(float) / 2 ** 31
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        x = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8)
             | (b[:, 2].astype(np.int8).astype(np.int32) << 16)).astype(float) / 2 ** 23
    else:
        x = np.frombuffer(raw, "<i2").astype(float) / 32768
    return x.reshape(-1, ch)[:, 0]


# ---------------------------------------------------------------- analysis --
GRID = np.geomspace(F0, F1, 600)          # ~1/60 octave


def impulse(rec: np.ndarray, start: int, inv: np.ndarray) -> tuple[np.ndarray, int]:
    """The impulse response of one sweep, and where its peak landed (samples
    from the start of the recording)."""
    n = len(inv)
    a = max(0, start - int(0.2 * RATE))
    b = min(len(rec), start + n + int(GAP_S * RATE))
    ir = fftconvolve(rec[a:b], inv)
    # The linear response sits after the sweep's length; harmonics land before it.
    lin = ir[n - 1:]
    peak = int(np.argmax(np.abs(lin)))
    return lin, a + peak


def spectrum(ir: np.ndarray, peak_in_ir: int) -> np.ndarray:
    """dB on GRID, from a windowed impulse response: 5 ms before the peak to
    300 ms after, which keeps the cabin's own modes (it is a small room) and
    drops the noise floor behind them."""
    a = max(0, peak_in_ir - int(0.005 * RATE))
    b = peak_in_ir + int(0.3 * RATE)
    seg = ir[a:b].copy()
    tail = int(0.1 * RATE)
    if len(seg) > tail:
        seg[-tail:] *= np.hanning(2 * tail)[tail:]
    nfft = 1 << 17
    mag = np.abs(np.fft.rfft(seg, nfft))
    f = np.fft.rfftfreq(nfft, 1 / RATE)
    db = 20 * np.log10(np.maximum(mag, 1e-12))
    return np.interp(GRID, f, db)


def smooth(db: np.ndarray, octave: float) -> np.ndarray:
    """Fractional-octave smoothing on GRID, done on power."""
    lg = np.log2(GRID)
    p = 10 ** (db / 10)
    out = np.empty_like(db)
    half = octave / 2
    for i, x in enumerate(lg):
        m = np.abs(lg - x) <= half
        out[i] = 10 * np.log10(np.mean(p[m]))
    return out


def umik(path: str) -> np.ndarray:
    """The mic's own deviation on GRID, to be taken off the measurement."""
    f, d = [], []
    with open(path) as fh:
        for line in fh:
            parts = line.replace(",", " ").split()
            try:
                f.append(float(parts[0]))
                d.append(float(parts[1]))
            except (IndexError, ValueError):
                continue
    if not f:
        return np.zeros_like(GRID)
    return np.interp(np.log(GRID), np.log(f), d)


def target() -> np.ndarray:
    """A car target: +5 dB of bass below 40 Hz easing to flat at 150 Hz, flat
    mids, and 1 dB an octave down above 1 kHz. Cars sound thin when measured
    flat; this is the usual shape people end up preferring."""
    lg = np.log2(GRID)
    t = np.zeros_like(GRID)
    lo = GRID <= 150
    t[lo] = 5 * np.clip((np.log2(150) - lg[lo]) / (np.log2(150) - np.log2(40)), 0, 1)
    hi = GRID > 1000
    t[hi] = -(lg[hi] - np.log2(1000))
    return t


def peaking(f0: float, q: float, g: float) -> np.ndarray:
    """RBJ peaking filter's response on GRID in dB: what PipeWire's bq_peaking
    computes at RATE."""
    a = 10 ** (g / 40)
    w0 = 2 * np.pi * f0 / RATE
    alpha = np.sin(w0) / (2 * q)
    b = np.array([1 + alpha * a, -2 * np.cos(w0), 1 - alpha * a])
    aa = np.array([1 + alpha / a, -2 * np.cos(w0), 1 - alpha / a])
    z = np.exp(-1j * 2 * np.pi * GRID / RATE)
    h = (b[0] + b[1] * z + b[2] * z * z) / (aa[0] + aa[1] * z + aa[2] * z * z)
    return 20 * np.log10(np.abs(h))


def fit(meas: np.ndarray, fine: np.ndarray | None = None) -> tuple[list[dict], np.ndarray, float]:
    """Greedy peaking-filter fit of a smoothed response to the target.

    Returns the bands, the response with them applied, and the offset that
    lines the measurement up with the target (its level in the mids).
    """
    tgt = target()
    mids = (GRID >= 150) & (GRID <= 2000)
    off = float(np.mean(meas[mids] - tgt[mids]))
    err = meas - off - tgt
    # The same at fine resolution: a null is far deeper there than smoothed.
    fine_err = (fine if fine is not None else meas) - off - tgt
    rng = (GRID >= FIT_LO) & (GRID <= FIT_HI)
    bands: list[dict] = []
    for _ in range(BANDS):
        e = np.where(rng, err, 0.0)
        # Peaks first and in full; dips only if wide and not too deep to be a null.
        can_boost = (GRID >= BOOST_LO) & (GRID <= BOOST_HI)
        score = np.where(e > 0, e, np.where((e < -2.5) & can_boost, -e * 0.5, 0.0))
        i = int(np.argmax(score))
        if score[i] < 1.0:
            break
        f0, depth = float(GRID[i]), float(e[i])
        # Width: where the deviation falls to half either side.
        lg = np.log2(GRID)
        j = i
        while j > 0 and abs(e[j]) > abs(depth) / 2 and np.sign(e[j]) == np.sign(depth):
            j -= 1
        k = i
        while k < len(e) - 1 and abs(e[k]) > abs(depth) / 2 and np.sign(e[k]) == np.sign(depth):
            k += 1
        bw = max(lg[k] - lg[j], 0.1)
        q = float(np.sqrt(2 ** bw) / (2 ** bw - 1))
        if depth > 0:
            gain, q = -min(depth, 12.0), float(np.clip(q, 0.7, 8.0))
        else:
            null = float(np.min(fine_err[j:k + 1])) < depth - 6.0
            if q > 2.0 or null:              # a narrow or deep dip is a cancellation: leave it
                rng = rng & ((GRID < GRID[j]) | (GRID > GRID[k]))
                continue
            gain, q = min(-depth * 0.5, BOOST_MAX), float(np.clip(q, 0.7, 2.0))
        bands.append({"f": round(f0, 1), "q": round(q, 2), "g": round(gain, 1)})
        err = err + peaking(f0, q, gain)
    return bands, err + off + tgt, off


def analyse(takes: list[tuple[np.ndarray, list[int]]], channels: list[str], cal: np.ndarray) -> dict:
    """takes: one (recording, sweep starts) per seat measured."""
    _, inv = sweep()
    result, arrivals, levels, raw = {}, {}, {}, {}
    per = {ch: ([], []) for ch in channels}          # spectra, arrivals per seat
    for rec, starts in takes:
        peak = float(np.max(np.abs(rec)))
        if peak > 0.98:
            raise RuntimeError("too loud: the microphone clipped, turn the volume down")
        for ch, st in zip(channels, starts):
            lin, at = impulse(rec, st, inv)
            snr = 20 * np.log10(np.max(np.abs(lin)) / (np.median(np.abs(lin)) + 1e-12))
            if snr < 25:
                raise RuntimeError(f"too quiet on {ch}: turn the volume up")
            per[ch][0].append(spectrum(lin, int(np.argmax(np.abs(lin)))) - cal)
            per[ch][1].append((at - st) / RATE)
    for ch in channels:
        specs, arr = per[ch]
        # Seats averaged on power: a peak every seat hears stays, one seat's null fills in.
        raw[ch] = 10 * np.log10(np.mean([10 ** (d / 10) for d in specs], axis=0))
        arrivals[ch] = float(np.mean(arr))
    for ch in channels:
        sm = smooth(raw[ch], 1 / 6)
        bands, after, off = fit(sm, smooth(raw[ch], 1 / 24))
        result[ch] = {"bands": bands, "after": after, "smoothed": sm}
        levels[ch] = off
    # Level: everything down to the quietest speaker, and further down by the
    # largest boost any band adds, so the correction can never clip.
    quiet = min(levels.values())
    boost = max([b["g"] for ch in channels for b in result[ch]["bands"]] + [0.0])
    # Delay: hold back the nearer speakers so all arrive with the farthest.
    last = max(arrivals.values())
    prof, meas = {"channels": {}}, {"grid": [round(f, 2) for f in GRID], "target": [], "channels": {}}
    tgt = target()
    for ch in channels:
        prof["channels"][ch] = {
            "gain_db": round(quiet - levels[ch] - boost, 2),
            "delay_ms": round((last - arrivals[ch]) * 1000, 3),
            "bands": result[ch]["bands"],
        }
        meas["channels"][ch] = {
            "before": [round(v - levels[ch], 2) for v in result[ch]["smoothed"]],
            "after": [round(v - levels[ch], 2) for v in result[ch]["after"]],
            "arrival_ms": round(arrivals[ch] * 1000, 3),
        }
    meas["target"] = [round(v, 2) for v in tgt]
    meas["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meas["seats"] = len(takes)
    return {"profile": prof, "measurement": meas}


# ----------------------------------------------------------------- measure --
def measure(hw: str, mic: str, channels: list[str], where: dict | None = None) -> tuple[np.ndarray, list[int]]:
    where = where or {}
    prog, starts = program(channels)
    # The output's own channel layout: a stereo file on a stereo output. A
    # channel not measured stays silent.
    tmp = tempfile.mkdtemp(prefix="comand-cal-")
    play, rec = os.path.join(tmp, "sweep.wav"), os.path.join(tmp, "rec.wav")
    write_wav(play, prog)
    r = subprocess.Popen(["pw-record", "--target", mic, "--rate", str(RATE), "--channels", "1",
                          "--format", "s32", rec],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    time.sleep(1.0)
    total = len(prog) / RATE
    seg = SWEEP_S + GAP_S
    p = subprocess.Popen(["pw-play", "--target", hw, play], stdout=subprocess.DEVNULL,
                         stderr=subprocess.PIPE, text=True)
    t0, last = time.monotonic(), None
    while p.poll() is None:
        el = time.monotonic() - t0
        if el > total + 30:
            p.kill()
            break
        # Which speaker the sweep is on now, from the program's own timing.
        k = int(min(len(channels) - 1, max(0.0, el - 0.5) // seg))
        frac = min(1.0, max(0.0, (el - 0.5 - k * seg) / SWEEP_S))
        now = (k, round(frac, 1), round(min(1.0, el / total), 2))
        if now != last:
            last = now
            say(step="measuring", channel=channels[k], index=k, count=len(channels), channels=channels,
                ch_progress=now[1], progress=now[2], seconds=round(total), **where)
        time.sleep(0.25)
    err = p.stderr.read() if p.stderr else ""
    time.sleep(1.0)
    r.terminate()
    r.wait(timeout=5)
    if p.returncode != 0:
        raise RuntimeError(f"playback failed: {err.strip()[:200]}")
    x = read_wav(rec)
    for f in (play, rec):
        os.unlink(f)
    os.rmdir(tmp)
    # Line the recording up with the program: the recorder started first, so
    # find the program's first sweep by cross-correlating its opening second.
    s, _ = sweep()
    probe = s[:RATE]
    c = fftconvolve(x[:min(len(x), 8 * RATE)], probe[::-1])
    first = int(np.argmax(np.abs(c))) - (len(probe) - 1)
    shift = first - starts[0]
    return x, [st + shift for st in starts]


def save(out: dict, folder: str) -> None:
    os.makedirs(folder, exist_ok=True)
    for path, data in ((os.path.join(folder, "eq.json"), out["profile"]),
                       (os.path.join(folder, "measurement.json"), out["measurement"])):
        with open(path + ".tmp", "w") as fh:
            json.dump(data, fh)
        os.replace(path + ".tmp", path)


def simulate() -> int:
    """A made-up car: FL booms at 55 and 120 Hz, FR has a 90 Hz null and sits
    1.5 ms further away, 4 dB quieter. The fit should cut the booms, leave the
    null mostly alone, delay FL by 1.5 ms and turn FL down 4 dB."""
    rng = np.random.default_rng(1)
    prog, starts = program(["FL", "FR"])

    def room(x, booms, delay_ms, gain_db):
        n = 1 << 15
        f = np.fft.rfftfreq(n, 1 / RATE)
        h = np.ones_like(f, dtype=complex)
        for f0, q, g in booms:
            a = 10 ** (g / 40); w0 = 2 * np.pi * f0 / RATE; al = np.sin(w0) / (2 * q)
            z = np.exp(-1j * 2 * np.pi * f / RATE)
            h *= ((1 + al * a) - 2 * np.cos(w0) * z + (1 - al * a) * z * z) / \
                 ((1 + al / a) - 2 * np.cos(w0) * z + (1 - al / a) * z * z)
        h *= 10 ** (gain_db / 20) * np.exp(-1j * 2 * np.pi * f * (0.004 + delay_ms / 1000))
        ir = np.fft.irfft(h, n)
        return fftconvolve(x, ir)[:len(x)]

    rec = room(prog[:, 0], [(55, 5, 9), (120, 3, 6)], 0.0, 0.0) + \
        room(prog[:, 1], [(90, 6, -14)], 1.5, -4.0)
    rec = rec * 0.5 + rng.normal(0, 1e-4, len(rec))
    out = analyse([(rec, starts)], ["FL", "FR"], np.zeros_like(GRID))
    print(json.dumps(out["profile"], indent=1))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hw")
    ap.add_argument("--mic")
    ap.add_argument("--cal", default="")
    ap.add_argument("--channels", default="FL,FR")
    ap.add_argument("--positions", default="", help="seats in turn; each waits for a line on stdin")
    ap.add_argument("--out", default=SOUND_DIR, help="folder for eq.json and measurement.json")
    ap.add_argument("--name", default="")
    ap.add_argument("--simulate", action="store_true")
    a = ap.parse_args()
    if a.simulate:
        return simulate()
    if not (a.hw and a.mic):
        ap.print_help()
        return 2
    channels = [c for c in a.channels.split(",") if c]
    try:
        cal = umik(a.cal) if a.cal else np.zeros_like(GRID)
        seats = [p for p in a.positions.split(",") if p]
        takes = []
        for i, seat in enumerate(seats or [""]):
            where = {"position": seat, "pos_index": i, "pos_count": max(1, len(seats))}
            if seats:
                say(step="place", **where)
                if not sys.stdin.readline():
                    raise RuntimeError("Stopped")
                for n in range(COUNTDOWN_S, 0, -1):
                    say(step="countdown", s=n, **where)
                    time.sleep(1)
            takes.append(measure(a.hw, a.mic, channels, where))
        say(step="analysing")
        out = analyse(takes, channels, cal)
        out["measurement"]["name"] = a.name
        out["measurement"]["positions"] = seats
        save(out, a.out)
        say(step="done", profile=out["profile"])
        return 0
    except Exception as e:  # noqa: BLE001 - the menu says what went wrong
        say(step="error", msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
