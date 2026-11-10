#!/bin/bash
# Build the UEFI USB installer on a Mac. ERASES the stick. (No Mac? See docs/INSTALL.md,
# "Without a Mac": the standard Debian installer does the same job with a few more questions.)
#
#   stick/make-stick.sh disk5            find the disk with: diskutil list external
#   OUT=/Volumes/X stick/make-stick.sh   write to an already formatted FAT32 volume instead
#
# Asks for the user name and password the laptop will have. Only a salted hash of
# the password goes on the stick. Optional: KEY=~/.ssh/id_ed25519.pub lets that key log in.
set -euo pipefail
cd "$(dirname "$0")"
RELEASE="$(cd .. && pwd)"
CACHE="${CACHE:-$HOME/Downloads/comand-usb-cache}"

# Pinned: the installer's kernel must be the ISO's kernel (checked below). When Debian
# moves on from this point release the download fails its checksum: use "Without a Mac".
ISO=debian-13.7.0-amd64-netinst.iso
ISO_URL=https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/$ISO
ISO_SHA=a7ef94ac2fb9a7fec454552abd629b7cc9d5155c886165a45649f5ce6167e355
HDM=https://deb.debian.org/debian/dists/trixie/main/installer-amd64/current/images/hd-media
VMLINUZ_SHA=2b2358b37674d2505350528875bb17afae2a36522a9e8a9417eaca65a7da0e08
INITRD_SHA=586d75b66f63c2f11e68f7c01f0ccb4e7c0a36608f86bce085bba20a68c09b7f

sha() { shasum -a 256 "$1" | cut -d' ' -f1; }
fetch() {   # fetch <url> <file> <sha256>
  [ -f "$2" ] && [ "$(sha "$2")" = "$3" ] && return 0
  echo "downloading $(basename "$2")"
  curl -fL --progress-bar -o "$2.part" "$1"
  [ "$(sha "$2.part")" = "$3" ] || { echo "checksum mismatch: $2 (Debian has probably moved to a newer point release)"; exit 1; }
  mv "$2.part" "$2"
}

D="${1:-}"; D="${D#/dev/}"; OUT="${OUT:-}"
[ -n "$D$OUT" ] || { echo "usage: $0 diskN" >&2; exit 2; }
if [ -z "$OUT" ]; then
  info=$(diskutil info "$D")
  grep -q 'Device Location: *External' <<<"$info" || { echo "$D is not an external disk -- refusing."; exit 1; }
  grep -q 'Whole: *Yes' <<<"$info" || { echo "Give the whole disk (diskN), not a partition."; exit 1; }
fi

U="${COMAND_USER:-}"; [ -n "$U" ] || read -r -p "User name for the laptop (lower case, e.g. headunit): " U
grep -Eq '^[a-z][a-z0-9-]{0,30}$' <<<"$U" || { echo "Use lower-case letters, digits and dashes, starting with a letter."; exit 1; }
HASH="${COMAND_PASSWORD_HASH:-}"
if [ -z "$HASH" ]; then
  read -r -s -p "Password for $U: " P1; echo
  read -r -s -p "Again: " P2; echo
  [ "$P1" = "$P2" ] && [ -n "$P1" ] || { echo "Passwords differ or are empty."; exit 1; }
  HASH="$(printf '%s' "$P1" | openssl passwd -6 -stdin)"; unset P1 P2
fi
TZ_NAME="${COMAND_TIMEZONE:-}"
if [ -z "$TZ_NAME" ]; then
  guess="$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')"
  read -r -p "Time zone [${guess:-UTC}]: " TZ_NAME; TZ_NAME="${TZ_NAME:-${guess:-UTC}}"
fi

mkdir -p "$CACHE"
fetch "$ISO_URL"       "$CACHE/$ISO"       "$ISO_SHA"
fetch "$HDM/vmlinuz"   "$CACHE/vmlinuz"    "$VMLINUZ_SHA"
fetch "$HDM/initrd.gz" "$CACHE/initrd.gz"  "$INITRD_SHA"

X="$CACHE/iso"; rm -rf "$X"; mkdir -p "$X"
bsdtar -xf "$CACHE/$ISO" -C "$X" EFI/boot/bootx64.efi EFI/boot/grubx64.efi boot/grub/font.pf2 install.amd/vmlinuz
[ "$(sha "$X/install.amd/vmlinuz")" = "$VMLINUZ_SHA" ] || { echo "Installer kernel does not match the ISO."; exit 1; }

if [ -z "$OUT" ]; then
  echo "$info" | grep -E 'Device / Media Name|Disk Size'
  read -r -p "ERASE /dev/$D and write the COMAND installer? Type YES: " ok
  [ "$ok" = YES ] || { echo "Cancelled."; exit 1; }
  diskutil eraseDisk FAT32 COMAND MBRFormat "/dev/$D"
  V=/Volumes/COMAND
else
  V="$OUT"
fi

install -d "$V/EFI/boot" "$V/EFI/debian" "$V/boot/grub" "$V/hd-media" "$V/comand-release"
cp "$X/EFI/boot/bootx64.efi" "$X/EFI/boot/grubx64.efi" "$V/EFI/boot/"
cp efi-debian-grub.cfg "$V/EFI/debian/grub.cfg"     # the signed GRUB reads this first
cp "$X/boot/grub/font.pf2" grub.cfg "$V/boot/grub/"
cp "$CACHE/vmlinuz" "$CACHE/initrd.gz" "$V/hd-media/"
sed -e "s|@USERNAME@|$U|g" -e "s|@TIMEZONE@|$TZ_NAME|g" -e "s|@PASSWORD_HASH@|$HASH|" preseed.cfg.in > "$V/preseed.cfg"
touch "$V/COMAND-INSTALLER"                         # what efi-debian-grub.cfg searches for
[ -n "${KEY:-}" ] && cp "$KEY" "$V/authorized_keys"
( cd "$RELEASE" && tar cf - --exclude .git --exclude 'docs/*.pdf' . ) | ( cd "$V/comand-release" && tar xmf - --no-same-owner )
echo "copying the ISO"
cp "$CACHE/$ISO" "$V/"
sync
[ "$(sha "$V/$ISO")" = "$ISO_SHA" ] || { echo "ISO copy is corrupt."; exit 1; }

mdutil -i off "$V" >/dev/null 2>&1 || true
find "$V" -name '._*' -not -path "$V/.Spotlight-V100*" -delete 2>/dev/null || true
rm -rf "$V/.fseventsd" 2>/dev/null || true
[ -n "$OUT" ] || diskutil eject "/dev/$D"
echo "Done. Boot the L560 from it (F12), on wired network."
