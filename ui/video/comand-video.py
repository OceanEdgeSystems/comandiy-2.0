#!/usr/bin/env python3
"""COMAND picture settings: brightness, contrast, gamma and colour balance for the output that feeds COMAND's RGB input.

Holds sway's gamma tables for that output (wlr gamma-control protocol), so the settings apply to everything shown on
COMAND (UI, CarPlay, Android, video) at no rendering cost. The tables stay only while this process runs; sway
restores the output when it exits. Settings come from /var/lib/comand/video.json (written by comand-bridge, /cmd
{"video": {...}}) and are re-read whenever the file changes:

  {"brightness": 0, "contrast": 0, "gamma": 0, "red": 0, "green": 0, "blue": 0}     each -100 … 100, 0 = unchanged

  comand-video.py [--output DP-2] [--print]        --print: show the curve for the current settings and exit
"""
import argparse, json, os, select, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pywayland.client import Display  # noqa: E402
from pywayland.protocol.wayland import WlOutput  # noqa: E402
from comand_wl.wlr_gamma_control_unstable_v1 import ZwlrGammaControlManagerV1  # noqa: E402

SETTINGS = os.environ.get('COMAND_VIDEO_SETTINGS', '/var/lib/comand/video.json')
KEYS = ('brightness', 'contrast', 'gamma', 'red', 'green', 'blue')


def load():
    try:
        with open(SETTINGS) as f: s = json.load(f)
    except (OSError, ValueError):
        s = {}
    return {k: max(-100, min(100, float(s.get(k, 0) or 0))) for k in KEYS}


def ramps(size, s):
    """Three uint16 ramps (red, green, blue) of `size` entries for settings `s`."""
    offset = s['brightness'] / 200                       # ±0.5 of full scale
    contrast = 1 + s['contrast'] / 100                   # 0 … 2, around mid-grey
    gamma = 2 ** (s['gamma'] / 100)                      # 0.5 … 2
    out = bytearray()
    for ch in ('red', 'green', 'blue'):
        gain = 1 + s[ch] / 200                           # 0.5 … 1.5
        for i in range(size):
            x = i / (size - 1)
            y = ((x - 0.5) * contrast + 0.5 + offset) * gain
            y = min(1.0, max(0.0, y)) ** (1 / gamma)
            out += int(round(y * 65535)).to_bytes(2, sys.byteorder)
    return bytes(out)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--output', default=os.environ.get('COMAND_VIDEO_OUTPUT', 'DP-2'))
    ap.add_argument('--print', action='store_true'); a = ap.parse_args()
    if a.print:
        s = load(); r = ramps(9, s)
        print(s); print([int.from_bytes(r[i:i + 2], sys.byteorder) for i in range(0, 18, 2)]); return

    d = Display(); d.connect()
    reg = d.get_registry(); outputs, mgr = [], []

    def glob(r, name, iface, ver):
        if iface == 'wl_output':
            o = r.bind(name, WlOutput, min(ver, 4)); o.user_data = None
            o.dispatcher['name'] = lambda o, n: setattr(o, 'user_data', n)
            outputs.append(o)
        elif iface == 'zwlr_gamma_control_manager_v1':
            mgr.append(r.bind(name, ZwlrGammaControlManagerV1, 1))
    reg.dispatcher['global'] = glob
    d.roundtrip(); d.roundtrip()
    if not mgr: sys.exit('compositor has no gamma control')
    out = next((o for o in outputs if o.user_data == a.output), None)
    if not out: sys.exit(f'no output {a.output} (have {[o.user_data for o in outputs]})')

    state = {'size': 0, 'failed': False}
    ctl = mgr[0].get_gamma_control(out)
    ctl.dispatcher['gamma_size'] = lambda c, n: state.update(size=n)
    ctl.dispatcher['failed'] = lambda c: state.update(failed=True)
    d.roundtrip()
    if state['failed'] or not state['size']: sys.exit(f'gamma control on {a.output} refused (another client holds it?)')

    def apply(s):
        fd = os.memfd_create('comand-gamma')
        os.write(fd, ramps(state['size'], s)); os.lseek(fd, 0, os.SEEK_SET)
        ctl.set_gamma(fd); d.flush(); os.close(fd)
        print(f'{a.output}: {s}', flush=True)

    mtime = None
    while not state['failed']:
        try: m = os.stat(SETTINGS).st_mtime_ns
        except OSError: m = 0
        if m != mtime:
            mtime = m; apply(load())
        d.flush()
        if select.select([d.get_fd()], [], [], 0.2)[0]:
            d.dispatch(block=True)
    sys.exit(f'gamma control on {a.output} failed (output gone?)')


if __name__ == '__main__':
    main()
