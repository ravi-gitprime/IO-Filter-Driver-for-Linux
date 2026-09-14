#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# dm-cdp smoke test: prove the drained stream reproduces the device.
#
#   1. 1 GiB loop-backed device wrapped by a cdp target
#   2. drain /dev/cdpN into stream.bin while mkfs + writes happen
#   3. replay stream.bin onto a pristine copy of the image
#   4. compare → must be identical
#
# Run as root from the repo root after `make`.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-/var/tmp/dm-cdp-test}
NAME=cdp-smoke
SIZE_MB=${SIZE_MB:-1024}
WRITE_MB=${WRITE_MB:-200}

cleanup() {
	set +e
	[ -n "${DRAIN_PID:-}" ] && kill "$DRAIN_PID" 2>/dev/null
	mountpoint -q "$WORK/mnt" && umount "$WORK/mnt"
	dmsetup remove "$NAME" 2>/dev/null
	[ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null
	[ -n "${DRAIN_PID:-}" ] && wait "$DRAIN_PID" 2>/dev/null
}
trap cleanup EXIT

mkdir -p "$WORK/mnt"
rm -f "$WORK/src.img" "$WORK/base.img" "$WORK/stream.bin"

echo "== create ${SIZE_MB} MiB image"
truncate -s "${SIZE_MB}M" "$WORK/src.img"
cp --sparse=always "$WORK/src.img" "$WORK/base.img"
LOOP=$(losetup --find --show "$WORK/src.img")
SECT=$(blockdev --getsz "$LOOP")

echo "== load module"
lsmod | grep -q '^dm_cdp' || insmod "$HERE/dm-cdp.ko"

echo "== create target"
dmsetup create "$NAME" --table "0 $SECT cdp $LOOP 0"
dmsetup status "$NAME"
MINOR=$(dmsetup status "$NAME" | awk '{print $4}' | sed 's/cdp//')
DEV=/dev/cdp$MINOR
[ -c "$DEV" ] || { echo "no $DEV"; exit 1; }

echo "== start drain -> $WORK/stream.bin"
"$HERE/tools/cdp-drain" -q -o "$WORK/stream.bin" "$DEV" &
DRAIN_PID=$!
sleep 0.5

echo "== mkfs + write ${WRITE_MB} MiB"
mkfs.ext4 -q -F /dev/mapper/$NAME
mount /dev/mapper/$NAME "$WORK/mnt"
dd if=/dev/urandom of="$WORK/mnt/a" bs=1M count=$((WRITE_MB / 2)) status=none
dd if=/dev/urandom of="$WORK/mnt/b" bs=4k count=$((WRITE_MB * 128)) status=none
sync
rm -f "$WORK/mnt/a"
fstrim "$WORK/mnt" 2>/dev/null || true
sync
umount "$WORK/mnt"
sync

kill -0 "$DRAIN_PID" 2>/dev/null || { echo "FAIL: drain died"; wait "$DRAIN_PID"; exit 1; }

echo "== wait for ring to drain"
for i in $(seq 1 50); do
	USED=$("$HERE/tools/cdp-drain" -S "$DEV" | awk '/^ring/{split($2,a,"/"); print a[1]}')
	[ "$USED" = "0" ] && break
	sleep 0.2
done
[ "$USED" = "0" ] || { echo "FAIL: ring not drained ($USED bytes left)"; exit 1; }
"$HERE/tools/cdp-drain" -S "$DEV"
"$HERE/tools/cdp-drain" -B "$DEV" | tail -1

echo "== stop drain"
kill "$DRAIN_PID"; wait "$DRAIN_PID" || true
DRAIN_PID=

echo "== remove target"
dmsetup remove "$NAME"
losetup -d "$LOOP"; LOOP=

echo "== replay onto base.img"
"$HERE/tools/cdp-apply" "$WORK/stream.bin" "$WORK/base.img"

echo "== compare"
if cmp "$WORK/src.img" "$WORK/base.img"; then
	echo "PASS: replayed image identical to source"
else
	echo "FAIL: images differ"
	exit 1
fi
ls -lh "$WORK/stream.bin"
