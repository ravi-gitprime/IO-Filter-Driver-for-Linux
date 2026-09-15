#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
rkcdpd - keep this node's replica.raw on NFS identical to its disk.

  dm-cdp ring  --->  pwrite into <journal>/<node>/replica.raw

The replica is only read after this node is dead, so the node itself keeps
it current; nothing sits in between.

Flow:
  1. adopt the dm-cdp mapping the initramfs made (or create one for
     conf["device"] on a non-root disk), open /dev/cdpN
  2. create replica.raw (sparse, full size) if missing
  3. apply loop: every record from the ring -> pwrite into replica.raw,
     fsync once per cycle_sec, status.json every second
  4. base copy (once, throttled, own thread): disk -> replica.raw. Writes
     during the copy are applied concurrently; re-applying is idempotent,
     so base + live writes = the disk. manifest.base_end_seq marks completion.
  5. overflow (GAP_BEFORE): bitmap recovery thread re-reads dirty chunks
     into the replica. Copy threads and the apply loop share one lock per
     chunk/record, so live records keep applying during a base copy or a
     recovery with no ordering race and nothing parked. Recovery never runs
     concurrently with the base copy.
  6. <node>/.rebuild.lock present -> records are parked in pending.bin until
     it is gone (the only time anything is parked).

Config /etc/rkcdp/rkcdp.conf (JSON):
  device          omit = initramfs-wrapped root disk; or "/dev/sdb"
  dm_name         "rkcdp"
  journal         "/replication/_kvmdr/rkcdp"
  node            hostname
  cycle_sec       1.0      fsync + status interval
  base_copy_mbps  15       throttle for base copy and bitmap recovery
"""
import fcntl
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dmcdp  # noqa: E402

CONF = "/etc/rkcdp/rkcdp.conf"
READ_SZ = 16 << 20
COPY_CHUNK = 4 << 20
FALLOC_FL_KEEP_SIZE = 0x01
FALLOC_FL_PUNCH_HOLE = 0x02

stop = False


def log(msg):
    sys.stderr.write("rkcdpd: %s\n" % msg)
    sys.stderr.flush()


def on_sig(*_):
    global stop
    stop = True


def run(cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def load_conf():
    conf = {"device": None, "dm_name": "rkcdp", "journal": "/replication/_kvmdr/rkcdp",
            "node": socket.gethostname(), "cycle_sec": 1.0, "base_copy_mbps": 15}
    if os.path.exists(CONF):
        with open(CONF) as f:
            conf.update(json.load(f))
    return conf


def blockdev_sectors(dev):
    return int(run(["blockdev", "--getsz", dev]).stdout.strip())


def root_disk():
    src = run(["findmnt", "-n", "-o", "SOURCE", "/"]).stdout.strip()
    name = os.path.basename(os.path.realpath(src))
    return "/dev/" + os.path.basename(os.path.realpath("/sys/class/block/%s/.." % name))


def table_device(name):
    tbl = run(["dmsetup", "table", name]).stdout.split()
    if len(tbl) < 5 or tbl[2] != "cdp":
        raise SystemExit("dm %s is not a cdp target: %s" % (name, " ".join(tbl)))
    return "/dev/" + os.path.basename(os.path.realpath("/sys/dev/block/" + tbl[3]))


def ensure_target(conf):
    """Adopt the existing mapping or create one. Returns /dev/cdpN."""
    name = conf["dm_name"]
    st = run(["dmsetup", "status", name], check=False)
    if st.returncode == 0:
        conf["device"] = table_device(name)
    else:
        if not conf.get("device"):
            raise SystemExit("root disk %s is not wrapped: install the initramfs hook and reboot, "
                             "or set 'device' in %s to a non-root disk" % (root_disk(), CONF))
        if run(["lsmod"]).stdout.find("dm_cdp") < 0:
            run(["modprobe", "dm-cdp"])
        run(["dmsetup", "create", name, "--table",
             "0 %d cdp %s 0" % (blockdev_sectors(conf["device"]), conf["device"])])
        st = run(["dmsetup", "status", name])
        log("attached %s as /dev/mapper/%s" % (conf["device"], name))
    fields = st.stdout.split()
    if len(fields) < 4 or fields[2] != "cdp":
        raise SystemExit("dm %s is not a cdp target: %s" % (name, st.stdout.strip()))
    return "/dev/" + fields[3]


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_json(path, default=None):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


class Throttle:
    """Keep a byte stream at or under mbps (0 = unlimited)."""

    def __init__(self, mbps):
        self.rate = float(mbps or 0) * 1048576
        self.t0 = time.time()
        self.done = 0

    def account(self, n):
        self.done += n
        if self.rate <= 0:
            return
        ahead = self.done / self.rate - (time.time() - self.t0)
        if ahead > 0:
            time.sleep(min(ahead, 1.0))


class Replica:
    """replica.raw on NFS, with punch-hole and zero-fill fallback."""

    def __init__(self, path, size):
        self.path = path
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.truncate(size)
            log("created %s (%d MiB, sparse)" % (path, size >> 20))
        self.fd = os.open(path, os.O_RDWR)
        self._libc = None
        self._punch_ok = True

    def write(self, rec, payload):
        if rec.type == dmcdp.REC_WRITE:
            os.pwrite(self.fd, payload, rec.sector * 512)
        elif rec.type in (dmcdp.REC_DISCARD, dmcdp.REC_ZERO):
            self.punch(rec.sector * 512, rec.len)

    def punch(self, off, length):
        if self._punch_ok:
            try:
                if self._libc is None:
                    import ctypes
                    self._libc = ctypes.CDLL("libc.so.6", use_errno=True)
                if self._libc.fallocate(self.fd, FALLOC_FL_PUNCH_HOLE | FALLOC_FL_KEEP_SIZE,
                                        ctypes.c_int64(off), ctypes.c_int64(length)) == 0:
                    return
            except Exception:
                pass
            self._punch_ok = False
        os.pwrite(self.fd, bytes(length), off)

    def fsync(self):
        os.fsync(self.fd)


class Daemon:
    def __init__(self, conf):
        self.conf = conf
        self.root = os.path.join(conf["journal"], conf["node"])
        os.makedirs(self.root, exist_ok=True)
        self.manifest_path = os.path.join(self.root, "manifest.json")
        self.status_path = os.path.join(self.root, "status.json")
        self.pending_path = os.path.join(self.root, "pending.bin")
        self.recovery_path = os.path.join(self.root, "recovery.json")
        self.lock_path = os.path.join(self.root, ".rebuild.lock")
        self.mbps = conf.get("base_copy_mbps") or 0
        self.cycle_sec = float(conf["cycle_sec"])

        self.cdp_path = ensure_target(conf)
        self.dm_dev = "/dev/mapper/" + conf["dm_name"]
        self.cdp_fd = os.open(self.cdp_path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.cdp_fd, dmcdp.IOC_RESET_STATS)
        except OSError:
            pass
        st = dmcdp.status(self.cdp_fd)
        self.size = st["dev_sectors"] * 512
        log("reading %s (dev %d MiB, ring %d MiB)" % (self.cdp_path, self.size >> 20, st["ring_size"] >> 20))

        self.manifest = load_json(self.manifest_path) or {
            "node": conf["node"], "device": conf["device"], "dm_name": conf["dm_name"],
            "gen": 1, "abi": dmcdp.ABI_VERSION, "size": self.size}
        write_json(self.manifest_path, self.manifest)
        self.replica = Replica(os.path.join(self.root, "replica.raw"), self.size)

        self.lock = threading.Lock()
        self.base_thread = None
        self.base_progress = {}
        self.recovery_thread = None
        self.recovery_queue = []        # gap seqs waiting for a recovery run
        self.gap_seen = None
        self.last_seq = None
        self.last_time = None
        self.records = 0
        self.bytes = 0

    # ---------------------------------------------------------------- base copy
    def base_copy(self):
        thr = Throttle(self.mbps)
        t0 = time.time()
        zero = bytes(COPY_CHUNK)
        log("base copy %s -> replica.raw at %s MB/s" % (self.dm_dev, self.mbps or "unlimited"))
        self.base_progress = {"done": 0, "total": self.size, "started": t0, "mbps": 0.0, "eta_sec": None}
        with open(self.dm_dev, "rb", buffering=0) as fi:
            off = 0
            while off < self.size and not stop:
                # read+write under the lock so a live record for this chunk
                # lands either before the read (then it is in buf) or after
                # the write (then it wins). No parking needed during the copy.
                with self.lock:
                    buf = fi.read(COPY_CHUNK)
                    if buf and buf != zero[:len(buf)]:
                        os.pwrite(self.replica.fd, buf, off)
                if not buf:
                    break
                off += len(buf)
                el = time.time() - t0
                rate = off / el if el > 0 else 0
                self.base_progress.update({"done": off, "mbps": round(rate / 1048576, 1),
                                           "eta_sec": int((self.size - off) / rate) if rate > 0 else None})
                thr.account(len(buf))
        if stop:
            log("base copy interrupted; restarts from scratch next run")
            self.base_progress = {}
            return
        self.replica.fsync()
        end_seq = dmcdp.status(self.cdp_fd)["seq_next"]
        with self.lock:
            self.manifest.update({"base_end_seq": end_seq, "base_time": time.time()})
            write_json(self.manifest_path, self.manifest)
        self.base_progress = {}
        log("base copy done in %.0fs, base_end_seq=%d" % (time.time() - t0, end_seq))

    # ---------------------------------------------------------------- recovery
    def start_recovery(self, gap_seq):
        """Read-and-clear the bitmap now, persist the extents, re-read in a thread."""
        bm, chunk, nbits = dmcdp.read_bitmap(self.cdp_fd, clear=True)
        extents = list(dmcdp.bitmap_extents(bm, chunk, nbits))
        rec = {"gap_seq": gap_seq, "extents": extents, "started": time.time()}
        write_json(self.recovery_path, rec)      # crash safety: redone on restart
        self.recovery_thread = threading.Thread(target=self.recover, args=(rec,), daemon=False)
        self.recovery_thread.start()

    def recover(self, rec):
        thr = Throttle(self.mbps)
        total = 0
        with open(self.dm_dev, "rb", buffering=0) as dev:
            for sector, nsect in rec["extents"]:
                off, remaining = sector * 512, nsect * 512
                while remaining and not stop:
                    n = min(remaining, COPY_CHUNK)
                    with self.lock:
                        dev.seek(off)
                        data = dev.read(n)
                        os.pwrite(self.replica.fd, data, off)
                    off += n
                    remaining -= n
                    total += n
                    thr.account(n)
        if stop:
            log("bitmap recovery interrupted; redone on restart")
            return
        self.replica.fsync()
        os.unlink(self.recovery_path)
        log("bitmap recovery: %d MiB re-read (gap at seq %s)" % (total >> 20, rec["gap_seq"]))

    def recovery_active(self):
        return self.recovery_thread is not None and self.recovery_thread.is_alive()

    # ---------------------------------------------------------------- apply
    def paused(self):
        """Records are parked only while a rebuild is copying the replica."""
        return os.path.exists(self.lock_path)

    def apply_one(self, r, payload):
        with self.lock:
            self.replica.write(r, payload)
        self.last_seq = r.seq
        self.records += 1
        if r.type == dmcdp.REC_WRITE:
            self.bytes += r.len

    def consume(self, buf):
        """Apply or park every complete record in buf. Returns the unconsumed tail."""
        off = 0
        park = self.paused()
        parked = bytearray()
        for r, payload in dmcdp.iter_records(buf):
            if r.flags & dmcdp.F_GAP_BEFORE and r.seq != self.gap_seen:
                self.gap_seen = r.seq
                self.recovery_queue.append(r.seq)
            if park:
                parked += buf[off:off + r.rec_len]
            else:
                self.apply_one(r, payload)
            off += r.rec_len
        if parked:
            with open(self.pending_path, "ab") as f:
                f.write(parked)
        return buf[off:]

    def apply_pending(self):
        """Apply records parked in pending.bin, in order, streaming (never load it whole)."""
        if not os.path.exists(self.pending_path):
            return
        n = 0
        tail = b""
        with open(self.pending_path, "rb") as f:
            while True:
                chunk = f.read(READ_SZ)
                if not chunk:
                    break
                buf = tail + chunk
                off = 0
                for r, payload in dmcdp.iter_records(buf):
                    self.apply_one(r, payload)
                    n += 1
                    off += r.rec_len
                tail = buf[off:]
        os.unlink(self.pending_path)
        if n:
            log("applied %d parked records" % n)

    # ---------------------------------------------------------------- status
    def write_status(self):
        st = dmcdp.status(self.cdp_fd)
        if self.base_progress:
            state = "SYNCING"
        elif self.recovery_active() or self.recovery_queue:
            state = "RECOVERING"
        elif os.path.exists(self.lock_path):
            state = "PAUSED"
        elif st["flags"] & dmcdp.S_OVERFLOWED:
            state = "BITMAP"
        else:
            state = "CDP"
        rec = load_json(self.recovery_path)
        write_json(self.status_path, {
            "node": self.conf["node"], "state": state, "time": time.time(),
            "last_seq": self.last_seq, "last_time": self.last_time,
            "seq_next": st["seq_next"], "ring_used": st["ring_used"], "ring_size": st["ring_size"],
            "overflows": st["overflows"], "records": self.records, "bytes": self.bytes,
            "base_copy": dict(self.base_progress) if self.base_progress else None,
            "recovery_gap_seq": rec["gap_seq"] if rec else None,
            "pending_bytes": os.path.getsize(self.pending_path) if os.path.exists(self.pending_path) else 0,
        })

    def node_json(self):
        macs = {}
        for n in os.listdir("/sys/class/net"):
            if n == "lo":
                continue
            try:
                with open("/sys/class/net/%s/address" % n) as f:
                    macs[n] = f.read().strip()
            except OSError:
                pass
        mem_kb = 0
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_kb = int(line.split()[1])
        return {"hostname": socket.gethostname(), "device": self.conf["device"], "disk_bytes": self.size,
                "cpus": os.cpu_count(), "mem_mb": mem_kb // 1024, "macs": macs,
                "ips": run(["hostname", "-I"], check=False).stdout.split(),
                "firmware": "efi" if os.path.exists("/sys/firmware/efi") else "bios", "time": time.time()}

    # ---------------------------------------------------------------- main loop
    def main(self):
        prev = load_json(self.recovery_path)
        if prev:
            log("resuming interrupted bitmap recovery (gap at seq %s)" % prev.get("gap_seq"))
            self.recovery_thread = threading.Thread(target=self.recover, args=(prev,), daemon=False)
            self.recovery_thread.start()
        if "base_end_seq" not in self.manifest:
            self.base_thread = threading.Thread(target=self.base_copy, daemon=False)
            self.base_thread.start()

        poll = select.poll()
        poll.register(self.cdp_fd, select.POLLIN)
        pending = b""
        last_cycle = time.time()
        last_node = 0.0

        while not stop:
            poll.poll(200)
            try:
                data = os.read(self.cdp_fd, READ_SZ)
            except BlockingIOError:
                data = b""
            except OSError as e:
                log("read error: %s" % e)
                time.sleep(1)
                continue
            if data:
                pending = self.consume(pending + data)

            # a queued recovery starts once the base copy is done and no other recovery runs
            if self.recovery_queue and not self.recovery_active() and not self.base_progress:
                self.start_recovery(self.recovery_queue.pop(0))
            # recovery finished / rebuild lock gone -> apply what was parked
            if not self.paused() and os.path.exists(self.pending_path):
                self.apply_pending()

            now = time.time()
            if now - last_cycle >= self.cycle_sec:
                self.replica.fsync()
                self.last_time = now
                self.write_status()
                last_cycle = now
            if now - last_node > 3600:
                write_json(os.path.join(self.root, "node.json"), self.node_json())
                last_node = now

        for t in (self.base_thread, self.recovery_thread):
            if t is not None and t.is_alive():
                t.join(30)
        self.replica.fsync()
        self.write_status()
        log("stopped")


def main():
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    Daemon(load_conf()).main()


if __name__ == "__main__":
    main()
