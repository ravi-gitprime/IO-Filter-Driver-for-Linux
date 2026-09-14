#!/bin/bash
# SPDX-License-Identifier: GPL-2.0
# dm-cdp fio harness:
#   - concurrent random writes (4 jobs x qd16, 4k-256k, direct) + buffered fsync job
#   - mid-run point-in-time snapshot via dmsetup suspend; replay-to-seq must match it
#   - full replay must match final image
#   - throughput: raw loop vs through cdp
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
WORK=${WORK:-/var/tmp/dm-cdp-test}
NAME=cdp-fio
DM=/dev/mapper/$NAME
SIZE_MB=${SIZE_MB:-1024}
RUNTIME=${RUNTIME:-20}
PIT_AT=${PIT_AT:-8}

command -v fio >/dev/null || { echo "install fio: apt install -y fio"; exit 2; }

cleanup() {
	set +e
	[ -n "${FIO_PID:-}" ] && kill "$FIO_PID" 2>/dev/null && wait "$FIO_PID" 2>/dev/null
	[ -n "${DRAIN_PID:-}" ] && kill "$DRAIN_PID" 2>/dev/null && wait "$DRAIN_PID" 2>/dev/null
	dmsetup remove "$NAME" 2>/dev/null
	[ -n "${LOOP:-}" ] && losetup -d "$LOOP" 2>/dev/null
}
trap cleanup EXIT

fio_job() {	# $1 = target device, $2 = name
	fio --name="$2" --filename="$1" --direct=1 --ioengine=libaio \
	    --rw=randwrite --bsrange=4k-256k --numjobs=4 --iodepth=16 \
	    --time_based --runtime="$RUNTIME" --group_reporting --output-format=terse \
	    --name=fsync --filename="$1" --direct=0 --ioengine=psync --rw=write \
	    --bs=64k --fsync=32 --numjobs=1 --time_based --runtime="$RUNTIME" \
	    --offset=512m --size=256m
}

bw_of() { awk -F';' 'NR==1{printf "%.0f", $48/1024}' "$1"; }	# write KiB/s -> MiB/s, first group

mkdir -p "$WORK"
rm -f "$WORK/src.img" "$WORK/base.img" "$WORK/pit.img" "$WORK/stream.bin"
truncate -s "${SIZE_MB}M" "$WORK/src.img"
cp --sparse=always "$WORK/src.img" "$WORK/base.img"
LOOP=$(losetup --find --show "$WORK/src.img")
SECT=$(blockdev --getsz "$LOOP")

echo "== baseline: fio on raw $LOOP for ${RUNTIME}s"
fio_job "$LOOP" raw > "$WORK/fio-raw.txt"
RAW_BW=$(bw_of "$WORK/fio-raw.txt")
# wipe so the cdp run starts from a known image
dd if=/dev/zero of="$LOOP" bs=1M status=none || true
blkdiscard "$LOOP" 2>/dev/null || true
sync

echo "== load module + target"
lsmod | grep -q '^dm_cdp' || insmod "$HERE/dm-cdp.ko"
dmsetup create "$NAME" --table "0 $SECT cdp $LOOP 0"
MINOR=$(dmsetup status "$NAME" | awk '{print $4}' | sed 's/cdp//')
DEV=/dev/cdp$MINOR
cp --sparse=always "$WORK/src.img" "$WORK/base.img"

echo "== drain -> stream.bin"
"$HERE/tools/cdp-drain" -q -o "$WORK/stream.bin" "$DEV" &
DRAIN_PID=$!
sleep 0.3

echo "== fio through cdp for ${RUNTIME}s, PIT snapshot at ${PIT_AT}s"
fio_job "$DM" cdp > "$WORK/fio-cdp.txt" &
FIO_PID=$!
sleep "$PIT_AT"

dmsetup suspend "$NAME"		# quiesces + flushes in-flight I/O
for i in $(seq 1 100); do
	USED=$("$HERE/tools/cdp-drain" -S "$DEV" | awk '/^ring/{split($2,a,"/"); print a[1]}')
	[ "$USED" = "0" ] && break; sleep 0.1
done
[ "$USED" = "0" ] || { echo "FAIL: ring not drained while suspended"; exit 1; }
PIT_SEQ=$("$HERE/tools/cdp-drain" -S "$DEV" | awk '/^seq_next/{print $2}')
dd if="$LOOP" of="$WORK/pit.img" bs=4M status=none
dmsetup resume "$NAME"
echo "PIT taken: seq_next=$PIT_SEQ"

wait "$FIO_PID"; FIO_PID=
sync
for i in $(seq 1 100); do
	USED=$("$HERE/tools/cdp-drain" -S "$DEV" | awk '/^ring/{split($2,a,"/"); print a[1]}')
	[ "$USED" = "0" ] && break; sleep 0.1
done
"$HERE/tools/cdp-drain" -S "$DEV" | grep -E 'flags|seq_next|overflows'
"$HERE/tools/cdp-drain" -S "$DEV" | grep -q OVERFLOWED && { echo "FAIL: overflowed (consumer too slow)"; exit 1; }
CDP_BW=$(bw_of "$WORK/fio-cdp.txt")

kill "$DRAIN_PID"; wait "$DRAIN_PID" || true; DRAIN_PID=
dmsetup remove "$NAME"
losetup -d "$LOOP"; LOOP=

echo "== replay to PIT seq $((PIT_SEQ - 1))"
"$HERE/tools/cdp-apply" -s $((PIT_SEQ - 1)) "$WORK/stream.bin" "$WORK/base.img"
cmp "$WORK/pit.img" "$WORK/base.img" && echo "PASS: PIT replay matches suspended snapshot" || { echo "FAIL: PIT mismatch"; exit 1; }

echo "== replay remainder"
"$HERE/tools/cdp-apply" -f "$PIT_SEQ" "$WORK/stream.bin" "$WORK/base.img"
cmp "$WORK/src.img" "$WORK/base.img" && echo "PASS: full replay matches final image" || { echo "FAIL: final mismatch"; exit 1; }

echo "== throughput (randwrite group): raw ${RAW_BW} MiB/s  cdp ${CDP_BW} MiB/s"
ls -lh "$WORK/stream.bin"
