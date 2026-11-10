#!/bin/bash
# Turn a fresh Debian 13 (trixie) amd64 install on a ThinkPad L560 into the COMAND head unit.
#
# Run it from this folder, on the laptop, as the ordinary user that will run the
# head unit (not root; sudo is used where needed):
#
#   ./install.sh                     asks for the name the phone will see
#   ./install.sh --car-name "My Car" --yes
#   ./install.sh --no-android        skip the Android image (saves about 2 GB)
#   ./install.sh --no-voice          skip voice commands and YouTube's helper
#
# Run it, reboot, and run it again: the Android image can only be fetched after the
# reboot. Running it again is always safe, and never overwrites your settings in
# /etc/default/comand*. See docs/INSTALL.md.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UI=/usr/local/share/comand-ui
SYS=/etc/systemd/system
CONF=/etc/default/comand
export DEBIAN_FRONTEND=noninteractive
ANDROID=1 VOICE=1 ASSUME_YES=0 CAR_NAME="${CAR_NAME:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --car-name) CAR_NAME="${2:?--car-name needs a value}"; shift ;;
    --no-android) ANDROID=0 ;;
    --no-voice) VOICE=0 ;;
    --yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,14p' "$0" | cut -c3-; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

[ "$(id -u)" != 0 ] || { echo "Run this as your ordinary user, not root." >&2; exit 1; }
[ "$(uname -m)" = x86_64 ] || { echo "This release is for amd64 (ThinkPad L560)." >&2; exit 1; }
. /etc/os-release
[ "${VERSION_CODENAME:-}" = trixie ] || { echo "This release needs Debian 13 (trixie); found ${PRETTY_NAME:-unknown}." >&2; exit 1; }
sudo -n true 2>/dev/null || sudo -v

U="$(id -un)"; G="$(id -gn)"; ID_U="$(id -u)"; H="$HOME"

if sudo test -f "$CONF"; then
  saved="$(sudo sed -n 's/^CAR_NAME="\(.*\)"$/\1/p' "$CONF")"
  CAR_NAME="${CAR_NAME:-$saved}"
fi
if [ -z "$CAR_NAME" ] && [ "$ASSUME_YES" = 0 ]; then
  read -r -p "Name your phone will see for this head unit [COMAND]: " CAR_NAME
fi
CAR_NAME="${CAR_NAME:-COMAND}"
case "$CAR_NAME" in
  *[!A-Za-z0-9\ ._-]*) echo "The name may use letters, digits, spaces, dot, dash and underscore." >&2; exit 1 ;;
esac

step() { echo; echo "== $*"; }

# A private copy of the release with this machine's user and name filled in.
STAGE="$(mktemp -d)"; trap 'rm -rf "$STAGE"' EXIT
cp -R "$SRC/." "$STAGE/"
grep -rlI --exclude=install.sh -e '@USER@' -e '@UID@' -e '@HOME@' -e '@CAR_NAME@' "$STAGE" | while read -r f; do
  sed -i -e "s|@USER@|$U|g" -e "s|@UID@|$ID_U|g" -e "s|@HOME@|$H|g" -e "s|@CAR_NAME@|$CAR_NAME|g" "$f"
done
put() { sudo install -D -m "${3:-644}" "$STAGE/$1" "$2"; }   # put <release path> <dest> [mode]

step "packages"
# firmware-* needs non-free-firmware, which a plain Debian install may not have enabled.
if ! grep -rqs 'non-free-firmware' /etc/apt/sources.list /etc/apt/sources.list.d/; then
  echo "deb http://deb.debian.org/debian trixie main contrib non-free-firmware" \
    | sudo tee /etc/apt/sources.list.d/comand-firmware.list >/dev/null
fi
sudo apt-get update -q
sudo -E apt-get install -y -q \
  osmium-tool squeezelite libasound2-plugins \
  sway swaybg cage cog foot grim wlr-randr seatd \
  python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-webkit-6.0 \
  gir1.2-gtk4layershell-1.0 libgtk4-layer-shell0 python3-cairo \
  python3-dbus python3-serial python3-usb python3-websockets python3-bleak python3-cryptography \
  python3-evdev python3-pil python3-numpy python3-scipy python3-venv python3-pip python3-paho-mqtt python3-pywayland sqlite3 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-vaapi \
  pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth pulseaudio-utils \
  mpd mpc mpv uxplay avahi-daemon bluez bluez-obexd ofono gpsd gpsd-clients \
  hfsplus udisks2 libmtp-runtime usb-modeswitch \
  plymouth plymouth-themes lxc curl jq nodejs dfu-util \
  can-utils evtest ethtool edid-decode libdrm-tests \
  intel-media-va-driver firmware-intel-graphics firmware-realtek firmware-sof-signed

if [ "$ANDROID" = 1 ]; then
  step "waydroid (its own repository; not packaged in trixie)"
  if [ ! -f /etc/apt/sources.list.d/waydroid.list ]; then
    curl -fsSL https://repo.waydro.id/waydroid.gpg | sudo tee /usr/share/keyrings/waydroid.gpg >/dev/null
    echo "deb [signed-by=/usr/share/keyrings/waydroid.gpg] https://repo.waydro.id/ trixie main" \
      | sudo tee /etc/apt/sources.list.d/waydroid.list >/dev/null
    sudo apt-get update -q
  fi
  sudo -E apt-get install -y -q waydroid
  # All three binder nodes; the module is permanent once loaded, so this takes a reboot.
  put l560/modprobe-binder.conf /etc/modprobe.d/comand-binder.conf
  put l560/modules-load-binder.conf /etc/modules-load.d/comand-binder.conf
fi

step "head unit files"
for f in ui/comand-bridge.py ui/comand-overlay.py ui/comand-keys.py ui/history.py ui/main-menu.html \
         ui/guest.html ui/qrcode.js ui/sway-headunit.conf audio/comand-eq.py audio/comand-cal.py \
         carplay/carplay.py \
         car/aux-battery.py car/ooono-ble.py car/voice.py car/map-update.py car/cameras.py car/ma-link.py \
         car/video-link.py car/squeezelite-comand.sh car/daly.py car/victron.py car/vehicle.py car/speedlimits.py \
         phone/bt-agent.py phone/bt-audio.py phone/call-audio.py phone/cover-art.py phone/pbap-sync.py \
         audio/fetch-logos.py audio/ipod-watch.py \
         android/gps-bridge.py android/nav-watch.py; do
  put "$f" "$UI/$(basename "$f")"
done
(cd "$STAGE/ui" && find video -type f ! -name '*.pyc') | while read -r f; do put "ui/$f" "$UI/$f"; done
for f in "$STAGE"/ui/icons/*.png;      do put "ui/icons/$(basename "$f")" "$UI/icons/$(basename "$f")"; done
put boot-splash/plymouth/comand/splash.png "$UI/boot-frame.png"
put l560/sway-l560.conf "$UI/sway-l560.conf"
put l560/comand-power.py        /usr/local/bin/comand-power.py 755
put mirror/comand-mirror-run.sh /usr/local/bin/comand-mirror-run.sh 755
put mirror/mirror-watch.py      /usr/local/bin/mirror-watch.py 755
put tools/comand-build-map      /usr/local/bin/comand-build-map 755
put tools/comand-check          /usr/local/bin/comand-check 755
put tools/rbcan.py              /usr/local/bin/rbcan 755

step "services"
for f in ui/systemd/comand-bridge.service ui/systemd/comand-session.service ui/systemd/comand-keys.service \
         carplay/systemd/comand-carplay.service \
         car/systemd/comand-aux-battery.service car/systemd/comand-ooono.service car/systemd/comand-voice.service \
         car/systemd/comand-ma-player.service car/systemd/comand-ma-link.service car/systemd/comand-video-link.service \
         car/systemd/comand-map-update.service car/systemd/comand-map-update.timer car/systemd/comand-vehicle.service \
         phone/systemd/comand-bt-agent.service phone/systemd/comand-bt-audio.service \
         audio/systemd/comand-ipod.service android/systemd/comand-gps.service \
         mirror/systemd/comand-mirror.service mirror/systemd/comand-mirror-watch.service \
         l560/systemd/comand-power.service l560/systemd/comand-battery-care.service \
         ui/systemd/comand-dp-force.service l560/systemd/can0.service; do
  put "$f" "$SYS/$(basename "$f")"
done
put boot-splash/systemd/after-splash.conf  "$SYS/comand-session.service.d/after-splash.conf"
put l560/systemd/comand-session-l560.conf  "$SYS/comand-session.service.d/l560.conf"
put carplay/systemd/comand-carplay.service.d/size-l560.conf "$SYS/comand-carplay.service.d/size.conf"
put l560/systemd/bluetooth.service.d/no-hostname.conf "$SYS/bluetooth.service.d/no-hostname.conf"
put boot-splash/systemd/retain-splash.conf "$SYS/plymouth-quit.service.d/retain-splash.conf"
put ui/systemd/99-comand.rules /etc/udev/rules.d/99-comand.rules
put tools/70-rbcan.rules       /etc/udev/rules.d/70-rbcan.rules
# mpd runs as the session user, and plymouth must not repaint over the panel on the way down.
for u in mpd plymouth-halt plymouth-kexec plymouth-poweroff plymouth-reboot plymouth-switch-root-initramfs; do
  sudo ln -sf /dev/null "$SYS/$u.service"
done

step "configuration"
put phone/bluetooth-main.conf    /etc/bluetooth/main.conf
put phone/wireplumber-bluez.conf /etc/wireplumber/wireplumber.conf.d/50-comand-bluez.conf
put android/gpsd.default         /etc/default/gpsd
install -D -m 644 "$STAGE/audio/mpd.conf" "$H/.config/mpd/mpd.conf"
install -D -m 644 "$STAGE/phone/wireplumber-no-msbc.conf" "$H/.config/wireplumber/wireplumber.conf.d/51-no-msbc.conf"
sudo install -d -o "$U" -g "$G" /var/lib/comand
for f in audio/stations.json phone/contacts.json; do
  sudo test -f "/var/lib/comand/$(basename "$f")" || sudo install -o "$U" -g "$G" -m 644 "$STAGE/$f" "/var/lib/comand/$(basename "$f")"
done
# The name lives here; your optional accounts live in the other files. Never overwritten.
sudo test -f "$CONF" || { put config/comand "$CONF" 600; sudo chown "$U:$G" "$CONF"; }
sudo sed -i "s|^CAR_NAME=.*|CAR_NAME=\"$CAR_NAME\"|" "$CONF"
for f in comand-vehicle comand-plex comand-ma comand-aux; do
  sudo test -f "/etc/default/$f" || { put "config/$f" "/etc/default/$f" 600; sudo chown "$U:$G" "/etc/default/$f"; }
done
sudo test -f /etc/default/comand-mirror || put mirror/comand-mirror.default /etc/default/comand-mirror

# The waydroid and map-update calls the services make. Nothing else gets sudo without a password.
printf '%s ALL=(root) NOPASSWD: /usr/bin/waydroid, /usr/bin/systemctl start --no-block comand-map-update\n' "$U" \
  | sudo tee /etc/sudoers.d/comand-headunit >/dev/null
sudo chmod 440 /etc/sudoers.d/comand-headunit
sudo visudo -cf /etc/sudoers.d/comand-headunit >/dev/null

# Advertise AirPlay on the car network only, never on lxcbr0/waydroid0.
IFACE="$(ip -o route show default | awk '{print $5; exit}')"
if [ -n "$IFACE" ]; then
  sudo sed -i -E "s/^#?allow-interfaces=.*/allow-interfaces=$IFACE/" /etc/avahi/avahi-daemon.conf
  echo "AirPlay advertised on $IFACE"
fi

step "boot splash"
sudo install -d /usr/share/plymouth/themes/comand
for f in "$STAGE"/boot-splash/plymouth/comand/*; do
  put "boot-splash/plymouth/comand/$(basename "$f")" "/usr/share/plymouth/themes/comand/$(basename "$f")"
done
sudo plymouth-set-default-theme comand

step "boot settings"
# No boot text on the panel: COMAND shows it garbled. Hold Shift or Esc at power-on for the GRUB menu.
want="quiet splash loglevel=0 systemd.show_status=false rd.systemd.show_status=false rd.udev.log_level=0 udev.log_level=0 vt.global_cursor_default=0 fbcon=vc:2-6"
# The RTL8153 USB ethernet adapter drops off the bus unless USB3 link power management is off.
for d in /sys/bus/usb/devices/*; do
  [ "$(cat "$d/idVendor" 2>/dev/null):$(cat "$d/idProduct" 2>/dev/null)" = 0bda:8153 ] \
    && { want="$want usbcore.quirks=0bda:8153:k"; break; }
done
for w in $want; do
  grep -qE "^GRUB_CMDLINE_LINUX_DEFAULT=.*(\"| )$w(\"| )" /etc/default/grub \
    || sudo sed -i "s/^GRUB_CMDLINE_LINUX_DEFAULT=\"\(.*\)\"/GRUB_CMDLINE_LINUX_DEFAULT=\"\1 $w\"/" /etc/default/grub
done
sudo sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=0/' /etc/default/grub
if grep -q '^GRUB_TIMEOUT_STYLE=' /etc/default/grub; then
  sudo sed -i 's/^GRUB_TIMEOUT_STYLE=.*/GRUB_TIMEOUT_STYLE=hidden/' /etc/default/grub
else
  echo 'GRUB_TIMEOUT_STYLE=hidden' | sudo tee -a /etc/default/grub >/dev/null
fi
# The laptop keeps running with its lid closed.
sudo install -d /etc/systemd/logind.conf.d /etc/systemd/system.conf.d
printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\nNAutoVTs=0\nReserveVT=0\n' \
  | sudo tee /etc/systemd/logind.conf.d/comand.conf >/dev/null
printf '[Manager]\nShowStatus=no\n' | sudo tee /etc/systemd/system.conf.d/comand.conf >/dev/null
# No login prompt on the panel: it would sit over the UI. SSH is the way in from here on.
sudo systemctl mask getty@tty1.service
sudo systemctl set-default multi-user.target
sudo update-grub 2>&1 | tail -1
sudo update-initramfs -u 2>&1 | tail -1

step "user"
for g in adm dialout audio video plugdev input render netdev bluetooth; do
  getent group "$g" >/dev/null && sudo usermod -aG "$g" "$U"
done
# PipeWire and mpd are user services and must run without a login.
sudo loginctl enable-linger "$U"
mkdir -p "$H/.config/systemd/user/default.target.wants"
ln -sf /usr/lib/systemd/user/mpd.service "$H/.config/systemd/user/default.target.wants/mpd.service"
# WebKit's portal lookup stalls start-up by about 25 s.
ln -sf /dev/null "$H/.config/systemd/user/xdg-desktop-portal.service"
ln -sf /dev/null "$H/.config/systemd/user/xdg-desktop-portal-gtk.service"

if [ "$VOICE" = 1 ]; then
  step "voice commands and YouTube helper"
  sudo install -d -o "$U" -g "$G" /opt/comand-voice /var/lib/comand/vosk
  [ -d /opt/comand-voice/venv ] || python3 -m venv --system-site-packages /opt/comand-voice/venv
  /opt/comand-voice/venv/bin/pip -q install vosk "yt-dlp[default]"
  if [ ! -d /var/lib/comand/vosk/vosk-model-small-en-us-0.15 ]; then
    T="$(mktemp --suffix=.zip)"
    curl -fsSL -o "$T" https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
    python3 -m zipfile -e "$T" /var/lib/comand/vosk/; rm -f "$T"
  fi
fi

step "enable"
sudo systemctl daemon-reload
sudo udevadm control --reload
# comand-session has no enable of its own: comand-carplay BindsTo it, which starts it.
sudo systemctl enable can0 comand-bridge comand-keys comand-bt-agent comand-bt-audio comand-carplay \
  comand-gps comand-ipod comand-mirror comand-mirror-watch comand-power comand-battery-care comand-dp-force comand-video-link \
  comand-map-update.timer gpsd.socket ofono
sudo systemctl disable gpsd.service 2>/dev/null || true
[ "$VOICE" = 1 ] && sudo systemctl enable comand-voice
[ "$ANDROID" = 1 ] && sudo systemctl enable waydroid-container
# Optional parts start only once their settings are filled in; re-run this script after editing.
has() { sudo grep -qE "^$2=\"?[^\"]" "/etc/default/$1"; }
toggle() { if "${@:2}"; then sudo systemctl enable "$1"; else sudo systemctl disable "$1" 2>/dev/null || true; fi; }
toggle comand-vehicle    has comand-vehicle HA_TOKEN
toggle comand-ma-player  has comand HA_HOSTS
toggle comand-ma-link    has comand-ma MA_TOKEN
toggle comand-aux-battery has comand-aux DALY_MAC

reboot_needed=0
if [ "$ANDROID" = 1 ] && ! sudo test -f /var/lib/waydroid/images/system.img; then
  step "android image"
  if [ -e /dev/vndbinder ]; then
    sudo systemctl stop waydroid-container
    sudo waydroid init -s GAPPS
    sudo systemctl start waydroid-container
  else
    reboot_needed=1
  fi
fi

echo
if [ "$reboot_needed" = 1 ]; then
  echo "Almost there. Reboot, then run ./install.sh once more to fetch the Android image."
  echo "From the reboot on, the laptop's own screen stays dark: connect with ssh $U@<address>."
else
  echo "Done. Reboot to start the head unit, then run: comand-check"
fi
