#!/bin/sh
# Flash a knob sketch, working around two hazards on this workstation.
#
# 1. esptool cannot auto-reset a board running a TinyUSB CDC sketch; it needs a
#    1200-baud touch first, after which the board re-enumerates in download
#    mode under a DIFFERENT port name.
# 2. Port names here are not stable and not unique-looking. A Home Assistant
#    Voice Kit enumerates as /dev/cu.usbmodem21101 while the knob's download
#    port is /dev/cu.usbmodem212101 -- one digit apart. Flashing by a
#    hardcoded name, as the README used to suggest, can hit the wrong device.
#
# Usage:  esp32-knob/flash.sh [sketch-dir] [run-port]
set -e

SKETCH="${1:-esp32-knob/knob-ui}"
FQBN="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=16M,PSRAM=opi,PartitionScheme=default_8MB"

# The knob runs TinyUSB, so its running port is the MAC-derived long name.
PORT="${2:-$(ls /dev/cu.usbmodem* 2>/dev/null | awk -F usbmodem 'length($2) > 8' | head -1)}"
[ -n "$PORT" ] || { echo "no TinyUSB port found; is the knob plugged in?" >&2; exit 1; }

echo "compiling $SKETCH"
arduino-cli compile -b "$FQBN" "$SKETCH"

echo "running port: $PORT"
BEFORE=$(ls /dev/cu.usbmodem* 2>/dev/null)

# pyserial lives in the repo venv, not in the system python -- using the wrong
# one makes the touch silently do nothing and the upload then fails to connect.
PY_BIN=python3
[ -x .venv/bin/python ] && PY_BIN=.venv/bin/python

"$PY_BIN" - "$PORT" <<'PY' || true
import sys, time
try:
    import serial
except ImportError:
    print("pyserial missing in %s -- cannot touch; hold BOOT, tap RST" % sys.executable)
    sys.exit(0)
try:
    s = serial.Serial(sys.argv[1], 1200, timeout=0)
    s.dtr = False; s.rts = False
    time.sleep(0.2); s.close()
    print("1200-baud touch sent")
except Exception as e:
    print("touch failed (%s) -- hold BOOT, tap RST, then re-run" % e)
PY

# The board can take a while to drop the CDC port and come back in download
# mode -- 15 s was not always enough.
n=0
while [ $n -lt 40 ]; do
  sleep 1
  # grep -vxF with a multi-line pattern excludes every port seen before.
  NEW=$(ls /dev/cu.usbmodem* 2>/dev/null | grep -vxF "$BEFORE" | head -1)
  [ -n "$NEW" ] && break
  n=$((n+1))
done
[ -n "$NEW" ] || { echo "board did not enter download mode after 40 s; hold BOOT, tap RST, re-run" >&2; exit 1; }

echo "download-mode port: $NEW"
arduino-cli upload -b "$FQBN" -p "$NEW" "$SKETCH"
