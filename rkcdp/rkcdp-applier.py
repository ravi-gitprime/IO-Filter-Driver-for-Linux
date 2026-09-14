#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
rkcdp-applier - keep every node's replica.raw current.

For each <journal>/<node>/ that has a replica.raw:
  - apply cycles/*.bin in name order into replica.raw (pwrite at sector*512)
  - fsync, then delete the cycle file
  - track progress in applied.json (last_seq, last_time)

Never touches the node's DB or API: NFS files only.

Usage: rkcdp-applier [--journal DIR] [--once] [--include-self]
"""
import argparse
import json
import os
import socket
import sys
import time
import signal
import fcntl

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dmcdp  # noqa: E402

FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02
stop = False


def log(msg):
    sys.stderr.write("rkcdp-applier: %s\n" % msg)
    sys.stderr.flush()


def on_sig(*_):
    global stop
    stop = True


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def punch(fd, off, length):
    try:
        os.posix_fallocate  # noqa: B018 - presence check only
        import ctypes
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.fallocate(fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
                          ctypes.c_int64(off), ctypes.c_int64(length)) == 0:
            return
    except Exception:
        pass
    os.pwrite(fd, bytes(length), off)


def apply_file(path, rfd, expect_seq):
    """Apply one cycle file. Returns (last_seq, bytes_written, records)."""
    with open(path, "rb") as f:
        data = f.read()
    last = expect_seq
    nbytes = nrec = 0
    for r, payload in dmcdp.iter_records(data):
        if r.flags & dmcdp.F_ERROR:
            raise RuntimeError("%s: ERROR record at seq %d" % (path, r.seq))
        if expect_seq is not None and r.seq < expect_seq:
            # recovery cycles reuse the gap seq; re-applying is harmless
            pass
        if r.type == dmcdp.REC_WRITE:
            os.pwrite(rfd, payload, r.sector * 512)
            nbytes += r.len
        elif r.type in (dmcdp.REC_DISCARD, dmcdp.REC_ZERO):
            punch(rfd, r.sector * 512, r.len)
        last = r.seq
        nrec += 1
    return last, nbytes, nrec


def process_node(node_dir):
    replica = os.path.join(node_dir, "replica.raw")
    cycles = os.path.join(node_dir, "cycles")
    if not os.path.exists(replica) or not os.path.isdir(cycles):
        return 0
    if os.path.exists(os.path.join(node_dir, ".rebuild.lock")):
        return 0  # rkcdp-rebuild is copying the replica; leave it untouched
    applied_path = os.path.join(node_dir, "applied.json")
    state = {"last_seq": None, "last_time": None, "cycles": 0, "bytes": 0}
    if os.path.exists(applied_path):
        with open(applied_path) as f:
            state.update(json.load(f))

    # Process complete files in name order, but stop at the first incomplete
    # one (.tmp): a pending bitmap-recovery file sorts before the records that
    # follow the gap and must be applied first.
    files = []
    for n in sorted(os.listdir(cycles)):
        if n.endswith(".bin.tmp"):
            break
        if n.endswith(".bin"):
            files.append(n)
    if not files:
        return 0

    n_done = 0
    rfd = os.open(replica, os.O_RDWR)
    try:
        for name in files:
            if stop:
                break
            path = os.path.join(cycles, name)
            last, nbytes, nrec = apply_file(path, rfd, state["last_seq"])
            os.fsync(rfd)
            state["last_seq"] = last
            state["last_time"] = time.time()
            state["cycles"] += 1
            state["bytes"] += nbytes
            write_json(applied_path, state)
            os.unlink(path)
            n_done += 1
    finally:
        os.close(rfd)
    return n_done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default="/replication/_kvmdr/rkcdp")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--include-self", action="store_true",
                    help="also apply this host's own cycles (testing)")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    me = socket.gethostname()

    # single active applier per journal; others wait (standby) until the
    # holder exits or dies, then take over automatically
    lockf = open(os.path.join(args.journal, ".applier.lock"), "w")
    waited = False
    while not stop:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if not waited:
                log("standby: another applier is active on %s" % args.journal)
                waited = True
            time.sleep(5)
    if stop:
        return

    log("active: watching %s" % args.journal)
    while not stop:
        total = 0
        for node in sorted(os.listdir(args.journal)):
            if node == me and not args.include_self:
                continue
            d = os.path.join(args.journal, node)
            if not os.path.isdir(d):
                continue
            try:
                total += process_node(d)
            except Exception as e:
                log("%s: %s" % (node, e))
        if args.once:
            log("applied %d cycle files" % total)
            break
        if total == 0:
            time.sleep(args.interval)
    log("stopped")


if __name__ == "__main__":
    main()
