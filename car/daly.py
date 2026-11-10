#!/usr/bin/env python3
"""The Daly BMS protocol, as frames in and readings out.

Kept apart from the Bluetooth plumbing because it is the half that can be
checked without the car: frames are bytes, readings are numbers, and a wrong
scale factor is a bug you want to find at a desk rather than on a battery.

The BMS speaks a fixed 13-byte frame in both directions:

    A5 <addr> <cmd> 08 <8 data bytes> <checksum>

with the checksum a plain sum of the preceding twelve bytes. Over Bluetooth
the same frames are carried on a UART-style GATT service -- nothing is
reframed, which is why this module knows nothing about BLE.

Only the four commands worth showing a driver are here. The BMS answers many
more, and every one of them is another thing to keep working.
"""

from __future__ import annotations

import struct

START = 0xA5
HOST = 0x40          # us, asking
BMS = 0x01           # the BMS, answering

SOC = 0x90           # pack voltage, current, state of charge
CELLS = 0x91         # highest and lowest cell
TEMPS = 0x92         # highest and lowest sensor
MOS = 0x93           # what the pack is doing, and whether it will let it
STATUS = 0x94        # how many cells there are, and how many sensors
CELL_VOLTS = 0x95    # every cell, three per frame
BALANCE = 0x97       # which cells the balancer is working on
SET_SOC = 0x21       # overwrite the BMS's state of charge

COMMANDS = (SOC, CELLS, TEMPS, MOS, STATUS, CELL_VOLTS, BALANCE)
FRAME_LEN = 13


def checksum(frame: bytes) -> int:
    return sum(frame[:12]) & 0xFF


def request(command: int, data: bytes = bytes(8)) -> bytes:
    """The twelve-byte question plus its checksum."""
    body = bytes([START, HOST, command, 0x08]) + data
    return body + bytes([checksum(body)])


def set_soc(percent: float) -> bytes:
    """Tell the BMS what its charge is, in tenths of a percent.

    The BMS counts coulombs and only corrects itself at the top of a full
    charge, so a pack that never gets one keeps whatever figure it last had.
    This is the hand correction. The first six bytes are a date the BMS does
    not use; zeros are what the app sends.
    """
    tenths = max(0, min(1000, round(percent * 10)))
    return request(SET_SOC, bytes(6) + struct.pack(">H", tenths))


def valid(frame: bytes) -> bool:
    return (len(frame) == FRAME_LEN
            and frame[0] == START
            and frame[12] == checksum(frame))


def frames(buf: bytes) -> tuple[list[bytes], bytes]:
    """Split a stream into whole frames, returning the leftover.

    Notifications do not arrive one frame at a time: several can share a
    packet and one can be split across two, so the caller keeps a buffer and
    hands back what this could not use. Anything that is not a frame start is
    dropped a byte at a time rather than resynchronising on length, because a
    corrupted length would swallow the frame that follows it.
    """
    out: list[bytes] = []
    while len(buf) >= FRAME_LEN:
        if buf[0] != START:
            buf = buf[1:]
            continue
        frame, rest = buf[:FRAME_LEN], buf[FRAME_LEN:]
        if valid(frame):
            out.append(frame)
            buf = rest
        else:
            buf = buf[1:]
    return out, buf


def decode(frame: bytes) -> dict:
    """One answered frame as named readings, or {} for one we do not show."""
    if not valid(frame):
        return {}
    cmd, d = frame[2], frame[4:12]

    if cmd == SOC:
        volts, _gather, raw_current, soc = struct.unpack(">HHHH", d)
        return {
            "volts": volts / 10.0,
            # Offset by 30000 rather than signed: the BMS reports current as
            # an unsigned number about a midpoint, so 30000 is zero, above is
            # charge and below is discharge.
            "amps": (raw_current - 30000) / 10.0,
            "soc": soc / 10.0,
        }

    if cmd == CELLS:
        hi_mv, hi_n, lo_mv, lo_n = struct.unpack(">HBHB", d[:6])
        return {
            "cell_hi": hi_mv / 1000.0, "cell_hi_n": hi_n,
            "cell_lo": lo_mv / 1000.0, "cell_lo_n": lo_n,
            # The number that actually says whether a pack is healthy.
            "cell_spread": (hi_mv - lo_mv) / 1000.0,
        }

    if cmd == TEMPS:
        # Both sensors are offset by 40 so that cold weather stays positive.
        return {"temp_hi": d[0] - 40, "temp_lo": d[2] - 40}

    if cmd == MOS:
        state, charge_mos, discharge_mos, _life = d[0], d[1], d[2], d[3]
        capacity = struct.unpack(">I", d[4:8])[0]
        return {
            "flow": {0: "idle", 1: "charging", 2: "discharging"}.get(state, "?"),
            "charge_mos": bool(charge_mos),
            "discharge_mos": bool(discharge_mos),
            "capacity_ah": capacity / 1000.0,
        }

    if cmd == STATUS:
        # How many cells the BMS is configured for. Worth asking rather than
        # inferring: the cell voltages arrive three to a frame, so a four cell
        # pack reports six slots, and the two it does not have come back as
        # whatever the unused inputs float at -- which is not reliably zero
        # and showed up on the panel as two extra cells.
        return {"cell_count": d[0], "sensor_count": d[1]}

    if cmd == BALANCE:
        # A bit per cell, least significant first, across all eight bytes --
        # room for forty-eight cells whatever the pack actually has. Returned
        # as the cell numbers a person would use, counting from one.
        on = [n + 1 for n in range(len(d) * 8)
              if d[n // 8] & (1 << (n % 8))]
        return {"balancing": on}

    if cmd == SET_SOC:
        # The answer to set_soc(): a one in the first byte means taken.
        return {"soc_set": d[0] == 1}

    if cmd == CELL_VOLTS:
        # Three cells per frame, numbered from one, as many frames as it
        # takes. Returned as the block it is rather than merged here: one
        # frame is not an answer, and decode() only ever sees one.
        block = struct.unpack(">HHH", d[1:7])
        return {"cell_frame": d[0], "cell_block": [mv / 1000.0 for mv in block]}

    return {}


def cell_voltages(seen: list[bytes]) -> list[float]:
    """Every cell, in order, from however many 0x95 frames arrived.

    Trailing zeros are dropped rather than shown. The BMS always fills whole
    frames of three, so a pack with four cells reports two frames and two
    cells of 0.000 V -- which on a screen reads as two dead cells.
    """
    found: dict[int, float] = {}
    for frame in seen:
        if not valid(frame) or frame[2] != CELL_VOLTS:
            continue
        n = frame[4]
        if not n:
            continue
        for i, mv in enumerate(struct.unpack(">HHH", frame[5:11])):
            found[(n - 1) * 3 + i] = mv / 1000.0

    out = [found[i] for i in sorted(found)]
    while out and out[-1] == 0.0:
        out.pop()
    return out


def readings(seen: list[bytes]) -> dict:
    """Everything a pass collected, as one set of named readings."""
    out: dict = {}
    for frame in seen:
        out.update(decode(frame))
    # The per-frame keys are scaffolding for the assembly below, not readings.
    out.pop("cell_frame", None)
    out.pop("cell_block", None)
    cells = cell_voltages(seen)
    count = out.pop("cell_count", 0)
    if count and out.get("balancing"):
        # The bitmap is as wide as the BMS can address, not as wide as this
        # pack is. Bits above the cell count are noise, and shown they would
        # claim a cell nine is balancing on a bank of four.
        out["balancing"] = [n for n in out["balancing"] if n <= count]
    if count:
        # Trusted over the number of slots that arrived. Padding is only
        # recognisable as padding if you know where the pack ends.
        cells = cells[:count]
    if cells:
        out["cells"] = cells
    return out
