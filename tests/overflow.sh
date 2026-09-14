#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# dm-cdp overflow test: ring too small, no consumer, then recover via bitmap.
#
#   1. 16 MiB ring, write 64 MiB with nobody draining  -> overflow
#   2. start drain, write 32 MiB more                   -> GAP_BEFORE record
#   3. recover: apply pre-gap records, re-read dirty extents from the
#      device, clear bitmap, apply post-gap records
#   4. compare -> must be identical
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-/var/tmp/dm-cdp-test}
NAME=cdp-ovf
DM=/dev/mapper/$NAME

cleanup() {
	set +e
	[ -n "${DRAIN_PID:-}" ] && kill "$DRAIN_PID" 2>/dev/null && wait "$DRAIN_PID" 2>/dev/null
	dmsetup remove "$NAME" 2>/dev/null
	[ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null
	rmmod dm-cdp 2>/dev/null
}
trap cleanup EXIT

mkdir -p "$WORK"
rm -f "$WORK/src.img" "$WORK/base.img" "$WORK/stream.bin" "$WORK/extents.txt"
truncate -s 256M "$WORK/src.img"
cp --sparse=always "$WORK/src.img" "$WORK/base.img"
LOOP=$(losetup --find --show "$WORK/src.img")
SECT=$(blockdev --getsz "$LOOP")

echo "== load module with 16 MiB ring"
rmmod dm-cdp 2>/dev/null || true
insmod "$HERE/dm-cdp.ko" ring_mb=16
dmsetup create "$NAME" --table "0 $SECT cdp $LOOP 0"
MINOR=$(dmsetup status "$NAME" | awk '{print $4}' | sed 's/cdp//')
DEV=/dev/cdp$MINOR

echo "== phase 1: 64 MiB with no consumer"
dd if=/dev/urandom of=$DM bs=1M count=64 oflag=direct status=none
"$HERE/tools/cdp-drain" -S "$DEV" | grep -E 'flags|overflows'
"$HERE/tools/cdp-drain" -S "$DEV" | grep -q OVERFLOWED || { echo "FAIL: no overflow flagged"; exit 1; }

echo "== phase 2: start drain, 32 MiB more"
"$HERE/tools/cdp-drain" -o "$WORK/stream.bin" "$DEV" > "$WORK/records.txt" &
DRAIN_PID=$!
sleep 0.5
dd if=/dev/urandom of=$DM bs=1M count=32 seek=100 oflag=direct status=none
sync
for i in $(seq 1 50); do
	USED=$("$HERE/tools/cdp-drain" -S "$DEV" | awk '/^ring/{split($2,a,"/"); print a[1]}')
	[ "$USED" = "0" ] && break; sleep 0.2
done
grep -c GAP_BEFORE "$WORK/records.txt" || { echo "FAIL: no GAP_BEFORE record"; exit 1; }
GAPSEQ=$(grep -m1 GAP_BEFORE "$WORK/records.txt" | awk '{print $1}')
echo "gap at seq $GAPSEQ"

echo "== phase 3: recover"
set +e
"$HERE/tools/cdp-apply" "$WORK/stream.bin" "$WORK/base.img"; RC=$?
set -e
[ "$RC" = 3 ] || { echo "FAIL: expected apply to stop at gap (rc=3), got $RC"; exit 1; }

"$HERE/tools/cdp-drain" -B -c "$DEV" > "$WORK/extents.txt"
N=$(wc -l < "$WORK/extents.txt")
echo "re-reading $N dirty extents from device"
while read -r _ _ S _ L; do
	dd if=$DM of="$WORK/base.img" bs=512 skip="$S" seek="$S" count="$L" conv=notrunc status=none
done < "$WORK/extents.txt"
"$HERE/tools/cdp-apply" -f "$GAPSEQ" "$WORK/stream.bin" "$WORK/base.img"

kill "$DRAIN_PID"; wait "$DRAIN_PID" || true; DRAIN_PID=
dmsetup remove "$NAME"
losetup -d "$LOOP"; LOOP=

echo "== compare"
if cmp "$WORK/src.img" "$WORK/base.img"; then
	echo "PASS: recovered image identical to source"
else
	echo "FAIL: images differ"; exit 1
fi
