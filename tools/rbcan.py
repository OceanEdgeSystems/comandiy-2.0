#!/usr/bin/env python3
"""Talk to the RB-CAN box (CANable 1.0 with the rbcan firmware) on COMAND's media bus, from the head unit.

  rbcan.py mon                 print every frame on the media bus (Ctrl-C to stop; also logs to rbcan-<time>.log)
  rbcan.py f 2D 01 01          send a TV -> COMAND message (the box adds the sequence number)
  rbcan.py t 264 10 2D 01 01   send a raw frame
  rbcan.py text "HELLO"        8 characters to the instrument cluster (what LiviToSerial does)
  rbcan.py flash rbcan.bin     put the box in DFU (DFU_BOOT) and flash it with dfu-util
The serial port is found by USB id (0483:52a4, the box's CDC interface); --port overrides it.
Only one program can hold the port: stop LiviToSerial first if it runs.
"""
import argparse, glob, os, subprocess, sys, time
import serial  # python3-serial


def port():
    ids = sorted(glob.glob('/dev/serial/by-id/*STM*'))
    if not ids:
        sys.exit('no STM32 CDC port under /dev/serial/by-id (is the box plugged in?)')
    return ids[0]


def open_port(p):
    return serial.Serial(p, 115200, timeout=0.2)


def cmd(s, line, wait=1.0):
    s.reset_input_buffer(); s.write((line + '\n').encode())
    t = time.time(); out = b''
    while time.time() - t < wait:
        out += s.read(256)
        if b'OK' in out or b'ERR' in out:
            break
    # The firmware streams the bus at power-up (as upstream SW03.00); keep only the reply.
    return '\n'.join(l for l in out.decode(errors='replace').splitlines() if l.strip() and not l.startswith('RX ID'))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('--port')
    sub = ap.add_subparsers(dest='c', required=True)
    sub.add_parser('mon')
    f = sub.add_parser('f'); f.add_argument('bytes', nargs='+')
    t = sub.add_parser('t'); t.add_argument('id'); t.add_argument('bytes', nargs='*')
    x = sub.add_parser('text'); x.add_argument('text')
    fl = sub.add_parser('flash'); fl.add_argument('bin')
    a = ap.parse_args(); p = a.port or port()

    if a.c == 'flash':
        try:                                # the box drops off USB as it resets into the ROM bootloader
            with open_port(p) as s:
                s.write(b'DFU_BOOT\n'); time.sleep(0.5); print(s.read(64).decode(errors='replace').strip())
        except serial.SerialException:
            pass
        for _ in range(20):                 # wait for the ROM DFU device
            if subprocess.run(['dfu-util', '-l', '-d', '0483:df11'], capture_output=True, text=True).stdout.count('0483:df11'):
                break
            time.sleep(0.5)
        sys.exit(subprocess.call(['dfu-util', '-a', '0', '-d', '0483:df11', '-s', '0x08000000:leave', '-D', a.bin]))
    with open_port(p) as s:
        if a.c == 'f':
            print(cmd(s, '!F ' + ' '.join(a.bytes)))
        elif a.c == 't':
            print(cmd(s, '!T ' + ' '.join([a.id] + a.bytes)))
        elif a.c == 'text':
            s.write(a.text[:8].encode('ascii', 'replace')); time.sleep(0.3); print(s.read(256).decode(errors='replace').strip())
        elif a.c == 'mon':
            print(cmd(s, '!MON 1'))
            log = open(time.strftime('rbcan-%Y%m%d-%H%M%S.log'), 'w')
            try:
                buf = b''
                while True:
                    buf += s.read(256)
                    *lines, buf = buf.split(b'\n')
                    for l in lines:
                        l = l.decode(errors='replace').strip()
                        if l:
                            ts = time.strftime('%H:%M:%S') + f'.{int(time.time() * 1000) % 1000:03d}'
                            print(ts, l); log.write(f'{ts} {l}\n'); log.flush()
            except KeyboardInterrupt:
                pass
            finally:
                s.write(b'!MON 0\n')


if __name__ == '__main__':
    main()
