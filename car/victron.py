#!/usr/bin/env python3
"""Victron "Instant Readout" advertisements, decrypted and parsed.

The Orion-Tr Smart says what it is doing in its BLE advertisement, so nothing
has to connect to it: no pairing, no GATT, no airtime held open next to the
phone's audio. The catch is that the payload is encrypted, and the key is
per-device -- VictronConnect shows it under Product info, "Instant readout via
Bluetooth".

Layout of the manufacturer data (company 0x02E1):

    0       0x10, "product advertisement"
    1..3    product identification
    4       record type -- 0x04 is a DC/DC converter, which is this charger
    5..6    nonce / data counter, little endian
    7       the first byte of the encryption key, so a wrong key is obvious
    8..     the encrypted record

Those offsets are where the car's own charger puts them, which is worth
saying because published descriptions of this format disagree by a byte. Read
one byte earlier, this Orion's record type comes out as 0xa3 -- not a type
Victron defines -- and the ciphertext is 11 bytes for a record that is 10.
Read as above it is 0x04, a DC/DC converter, with exactly 10 bytes after it.

The cipher is AES-128 in counter mode, with the counter block being the nonce
little endian followed by fourteen zero bytes. Byte 6 is worth checking before
decrypting: without it a wrong key yields plausible-looking rubbish rather
than an error, and rubbish about a charger is worse than nothing.
"""

from __future__ import annotations

import struct

VICTRON_COMPANY_ID = 0x02E1

SOLAR_CHARGER = 0x01
DC_DC_CONVERTER = 0x04

# What the charger says it is doing. Victron's device-state table is shared
# across products; these are the states a DC/DC converter actually reports.
DEVICE_STATE = {
    0: "off", 2: "fault", 3: "bulk", 4: "absorption", 5: "float",
    11: "other fault", 245: "starting", 252: "external control",
}

# Why it is off, as a bit field, in the order Victron numbers them.
OFF_REASON = {
    0x00000001: "no input power",
    0x00000002: "switched off (switch)",
    0x00000004: "switched off (register)",
    0x00000008: "remote input",
    0x00000010: "protection active",
    0x00000020: "paygo",
    0x00000040: "BMS",
    0x00000080: "engine shutdown",
    0x00000100: "analysing input voltage",
}


class WrongKey(ValueError):
    """The key does not belong to this device."""


def decrypt(data: bytes, key: bytes) -> tuple[int, bytes]:
    """Return (record type, plaintext) from one advertisement payload.

    Raises WrongKey when the key check byte disagrees, and ValueError when the
    payload is not a Victron product advertisement at all.
    """
    if len(data) < 9 or data[0] != 0x10:
        raise ValueError("not a Victron product advertisement")
    if len(key) != 16:
        raise ValueError("a Victron encryption key is 16 bytes")

    record_type = data[4]
    nonce = data[5:7]
    if data[7] != key[0]:
        raise WrongKey(f"advertisement expects a key starting {data[7]:02x}")

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    counter = bytes(nonce) + bytes(14)
    cipher = Cipher(algorithms.AES(key), modes.CTR(counter))
    plain = cipher.decryptor().update(data[8:])
    return record_type, plain


def _off_reasons(bits: int) -> list[str]:
    return [name for bit, name in OFF_REASON.items() if bits & bit]


def parse_dcdc(plain: bytes) -> dict:
    """A DC/DC converter record: what it is doing and at what voltages.

        0     device state
        1     charger error
        2..3  input voltage,  0.01 V, signed
        4..5  output voltage, 0.01 V, signed
        6..9  off reason, bit field

    Voltages read 0x7FFF / 0xFFFF when the charger has nothing to report,
    which is not the same as zero volts and must not be shown as though it
    were -- an aux battery reading 0.00 V would send somebody looking for a
    broken cable.
    """
    if len(plain) < 10:
        return {}
    state, error = plain[0], plain[1]
    vin, vout = struct.unpack("<hh", plain[2:6])
    off = struct.unpack("<I", plain[6:10])[0]

    out = {
        "state": DEVICE_STATE.get(state, f"state {state}"),
        "error": error,
        "charging": state in (3, 4, 5),
    }
    if vin not in (0x7FFF, -1):
        out["vin"] = vin / 100.0
    if vout not in (0x7FFF, -1):
        out["vout"] = vout / 100.0
    if state == 0:
        out["off_reason"] = _off_reasons(off)
    return out


# What a solar charger says it is doing. Shares the numbering with the DC/DC
# converter for the states both have, and adds the ones only an MPPT reaches.
SOLAR_STATE = dict(DEVICE_STATE)
SOLAR_STATE.update({1: "low power", 6: "storage", 7: "equalize",
                    245: "starting", 247: "auto equalize"})


def parse_solar(plain: bytes) -> dict:
    """A solar charger record.

        0     device state
        1     charger error
        2..3  battery voltage, 0.01 V, signed
        4..5  battery current, 0.1 A, signed
        6..7  yield today,     0.01 kWh
        8..9  PV power, watts
        10..  load current, 0.1 A, nine bits

    Unlike the DC/DC record, this layout has not been checked against the
    car's own charger -- the MPPT is not broadcasting yet -- so it is written
    defensively and the fields that look unreported are left out rather than
    shown as zero. The DC/DC one needed a byte's correction when its first
    real advertisement arrived; expect the same scrutiny here.
    """
    if len(plain) < 10:
        return {}
    state, error = plain[0], plain[1]
    volts, amps, yield_today, watts = struct.unpack("<hhHH", plain[2:10])

    out = {
        "state": SOLAR_STATE.get(state, f"state {state}"),
        "error": error,
        "charging": state in (3, 4, 5, 6, 7),
    }
    if volts not in (0x7FFF, -1):
        out["volts"] = volts / 100.0
    if amps not in (0x7FFF, -1):
        out["amps"] = amps / 10.0
    if yield_today != 0xFFFF:
        out["yield_kwh"] = yield_today / 100.0
    if watts != 0xFFFF:
        out["watts"] = watts
    if len(plain) >= 12:
        load = struct.unpack("<H", plain[10:12])[0] & 0x01FF
        if load != 0x01FF:
            out["load_amps"] = load / 10.0
    return out


PARSERS = {
    DC_DC_CONVERTER: parse_dcdc,
    SOLAR_CHARGER: parse_solar,
}

# What each record is called where the car shows it.
NAMES = {DC_DC_CONVERTER: "charger", SOLAR_CHARGER: "solar"}


def reading(data: bytes, key: bytes) -> tuple[str, dict]:
    """One advertisement to (name, reading), or ("", {}) if it is not ours."""
    record_type, plain = decrypt(data, key)
    parser = PARSERS.get(record_type)
    if not parser:
        return "", {}
    return NAMES[record_type], parser(plain)
