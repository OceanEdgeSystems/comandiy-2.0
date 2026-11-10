#!/bin/bash
# Start the AirPlay receiver with the fitting the panel needs.
#
# A wrapper rather than a longer ExecStart because two of these settings are
# GStreamer pipeline fragments containing spaces, and systemd's $VAR expansion
# splits on whitespace without regard for quotes -- so a fragment written into
# EnvironmentFile arrives at uxplay as several mangled arguments.
#
# Settings come from /etc/default/comand-mirror:
#
#   MIRROR_FIT     crop (default) | fit
#   MIRROR_ASPECT  panel aspect as a GStreamer fraction, default 5/3
#   MIRROR_ROTATE  empty (default), or "-r R" / "-r L"
#   MIRROR_NAME    what the phone calls this car
#   MIRROR_DEBUG   1 to log the client's own streaming performance reports
#                  (how many frames it sent, and how many it gave up on)
set -u

FIT=${MIRROR_FIT:-crop}
ASPECT=${MIRROR_ASPECT:-5/3}
ROTATE=${MIRROR_ROTATE:-}
NAME=${MIRROR_NAME:-@CAR_NAME@}

# The phone always sends its own screen's shape, whatever display size we ask
# for: 1200x552 held landscape, 332x720 held portrait. Neither is the panel's
# 5:3, so something has to give.
#
#   crop  trim the sides to the panel's shape before scaling. Fills the
#         screen, nothing is distorted, and what is lost is the outer edge of
#         a picture that was letterboxed into black bars anyway.
#   fit   leave it alone: the whole phone screen, with black bars.
#
# The crop happens in uxplay's videoconverter slot, which sits between the
# decoder and videoscale -- so it is the scaler that sees an already-correct
# shape, and the sink has no bars left to add.
case "$FIT" in
    crop) VC=(-vc "videoconvert ! aspectratiocrop aspect-ratio=$ASPECT") ;;
    fit)  VC=() ;;
    *)    echo "MIRROR_FIT=$FIT is not crop or fit; using fit" >&2; VC=() ;;
esac

# -s 800x480 asks the phone to render at the panel's size. It does not decide
# the shape, but it does decide how many pixels arrive: this machine has no
# hardware H.264 decoder (the CM5 silicon dropped the block), so every frame
# is decoded in software, and asking for 1080p we then throw away is work for
# nothing.
#
# -reset 0 never tears a session down for client silence; -nofreeze clears the
# last frame rather than leaving it up when one ends.
# -dacp writes the client's DACP-ID and Active-Remote token to a file as soon
# as it connects. Those two are what let the car send play/pause and skip back
# to the phone over AirPlay's own remote-control channel -- which matters
# because a phone mirroring over the car's LAN need not be paired over
# Bluetooth, and AVRCP is not there when it is not.
# The client reports its own view of the stream -- frames sent, frames
# dropped. Off by default because it prints continuously; the only way to tell
# a receiver that cannot keep up from a network that cannot deliver.
DEBUG=()
[ "${MIRROR_DEBUG:-0}" = 1 ] && DEBUG=(-FPSdata)

exec /usr/bin/stdbuf -oL -eL /usr/bin/uxplay \
    -n "$NAME" -nh \
    "${DEBUG[@]}" \
    -dacp /var/lib/comand/uxplay.dacp \
    -s 800x480 \
    -reset 0 -nofreeze \
    $ROTATE \
    "${VC[@]}" \
    -vs waylandsink
