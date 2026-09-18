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
     into the replica. A copy thread flags the 4 MB chunk it is on; records
     for that chunk are deferred until it lands, everything else applies
     immediately. No ordering race, nothing parked, no lock held during I/O. Recovery never runs
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
  consistent_sec  3600     how often to take a consistent point (0 = never)
  freeze_cap_sec  30       longest the filesystem may stay frozen for one
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


def boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


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


SEED_CHUNK = 65536


def data_regions(fd, size):
    """Yield (off, len) of the non-hole regions of a sparse file (SEEK_DATA/SEEK_HOLE)."""
    off = 0
    while off < size:
        try:
            d = os.lseek(fd, off, os.SEEK_DATA)
        except OSError:
            return
        if d >= size:
            return
        h = os.lseek(fd, d, os.SEEK_HOLE)
        yield d, h - d
        off = h


class SeedLayer:
    """seed.raw: sparse image the size of the disk holding, for every chunk that
    has changed since the seed point, what that chunk contained AT the seed
    point. Written at most once per chunk (copy-on-first-write); read only by
    a seed rebuild. Present only if rkcdp-seed created the file."""

    def __init__(self, path, size):
        self.path = path
        self.size = size
        self.fd = os.open(path, os.O_RDWR)
        self.nchunks = (size + SEED_CHUNK - 1) // SEED_CHUNK
        self.saved = bytearray((self.nchunks + 7) // 8)
        n = 0
        for off, length in data_regions(self.fd, size):
            for c in range(off // SEED_CHUNK, (off + length + SEED_CHUNK - 1) // SEED_CHUNK):
                self.saved[c >> 3] |= 1 << (c & 7)
                n += 1
        log("seed layer: %s, %d chunks already saved" % (path, n))

    def is_saved(self, c):
        return self.saved[c >> 3] & (1 << (c & 7))

    def preserve(self, replica_fd, off, length):
        """Before [off, off+length) of the replica is overwritten: save the
        chunks in that range that have not been saved yet."""
        c0, c1 = off // SEED_CHUNK, (off + length + SEED_CHUNK - 1) // SEED_CHUNK
        for c in range(c0, min(c1, self.nchunks)):
            if self.is_saved(c):
                continue
            o = c * SEED_CHUNK
            old = os.pread(replica_fd, min(SEED_CHUNK, self.size - o), o)
            if any(old):                       # a zero chunk is a hole either way
                os.pwrite(self.fd, old, o)
            self.saved[c >> 3] |= 1 << (c & 7)

    def fsync(self):
        os.fsync(self.fd)


class Replica:
    """replica.raw on NFS, with punch-hole and zero-fill fallback, plus the
    optional copy-on-first-write seed layer."""

    def __init__(self, path, size):
        self.path = path
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.truncate(size)
            log("created %s (%d MiB, sparse)" % (path, size >> 20))
        self.fd = os.open(path, os.O_RDWR)
        self._libc = None
        self._punch_ok = True
        seed_path = os.path.join(os.path.dirname(path), "seed.raw")
        self.seed = SeedLayer(seed_path, size) if os.path.exists(seed_path) else None

    def pwrite(self, buf, off):
        if self.seed:
            self.seed.preserve(self.fd, off, len(buf))
        os.pwrite(self.fd, buf, off)

    def write(self, rec, payload):
        if rec.type == dmcdp.REC_WRITE:
            self.pwrite(payload, rec.sector * 512)
        elif rec.type in (dmcdp.REC_DISCARD, dmcdp.REC_ZERO):
            if self.seed:
                self.seed.preserve(self.fd, rec.sector * 512, rec.len)
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
        if self.seed:
            self.seed.fsync()


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
        self.seed_required = bool(conf.get("seed_required")) and "base_end_seq" not in self.manifest
        self.replica = Replica(os.path.join(self.root, "replica.raw"), self.size)
        self.boot_id = boot_id()

        self.lock = threading.Lock()      # guards copying/deferred and each replica write
        self.copying = None               # (start_off, end_off) chunk a copy thread is on
        self.deferred = []                # records for that chunk, applied once it lands
        self.base_thread = None
        self.base_progress = {}
        self.recovery_thread = None
        self.recovery_queue = []        # gap seqs waiting for a recovery run
        self.consistent_sec = float(conf.get("consistent_sec") or 3600)
        self.freeze_cap_sec = float(conf.get("freeze_cap_sec") or 30)
        self.last_consistent = 0.0
        self.gap_seen = None
        self.last_seq = None
        self.last_time = None
        self.records = 0
        self.bytes = 0

    # ---------------------------------------------------------------- copy protocol
    def copy_chunk(self, dev, off, n, skip_zero=None):
        """Copy [off, off+n) from the device into the replica without racing the
        apply loop: flag the range (lock held only for the flag), do the I/O
        unlocked, then apply any records that arrived for that range meanwhile
        (they are newer than what we just wrote)."""
        with self.lock:
            self.copying = (off, off + n)
            self.deferred = []
        dev.seek(off)
        buf = dev.read(n)
        if buf and not (skip_zero is not None and buf == skip_zero[:len(buf)]):
            self.replica.pwrite(buf, off)
        with self.lock:
            self.copying = None
            late, self.deferred = self.deferred, []
        for r, payload in late:
            self.apply_one(r, payload)
        return len(buf)

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
                n = self.copy_chunk(fi, off, min(COPY_CHUNK, self.size - off), skip_zero=zero)
                if not n:
                    break
                off += n
                el = time.time() - t0
                rate = off / el if el > 0 else 0
                self.base_progress.update({"done": off, "mbps": round(rate / 1048576, 1),
                                           "eta_sec": int((self.size - off) / rate) if rate > 0 else None})
                thr.account(n)
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
                    n = self.copy_chunk(dev, off, min(remaining, COPY_CHUNK))
                    if not n:
                        break
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

    # ------------------------------------------------------- consistent point
    def mark_consistent(self):
        """Give the replica a point it is actually consistent AT.

        Found 18 Sep 2026: both managers' replica.raw failed e2fsck at rest —
        extents past their end, block-bitmap checksum mismatches. Nothing had
        crashed. Writes are applied in order, but each one lands at a
        different instant and the replica is never quiesced, so the image is
        a smear across time: metadata that never coexisted on the live disk.
        A rebuild from it mounted, hit a bad bitmap checksum seconds in and
        remounted read-only.

        So: freeze the filesystem, let the in-flight records land, fsync, and
        record the sequence number reached. The replica still has no single
        instant — but seq <= consistent_seq does, and that is the only point
        a rebuild may use. The node stalls for the freeze (sub-second when
        the writer is idle); postgres handles that, and heal-standby brings
        the DB forward from the peer afterwards, so the replica does not need
        to be current — only safe."""
        if self.paused() or self.recovery_active() or self.base_progress:
            return                                   # not a settled moment
        t0 = time.time()
        frozen = False
        try:
            r = subprocess.run(["fsfreeze", "-f", "/"], capture_output=True, text=True,
                               timeout=self.freeze_cap_sec)
            if r.returncode != 0:
                log("consistent point: freeze refused (%s)" % (r.stderr or "").strip()[:80])
                return
            frozen = True
            # Drain what the freeze flushed: the kernel ring holds it already,
            # we only have to read it out and apply it before thawing.
            deadline = t0 + self.freeze_cap_sec
            pending = b""
            while time.time() < deadline:
                try:
                    data = os.read(self.cdp_fd, READ_SZ)
                except (BlockingIOError, OSError):
                    data = b""
                if not data:
                    break
                pending = self.consume(pending + data)
            self.replica.fsync()
            seq = self.last_seq
        finally:
            if frozen:
                subprocess.run(["fsfreeze", "-u", "/"], capture_output=True)
        held = time.time() - t0
        if self.recovery_queue or self.recovery_active():
            log("consistent point abandoned: a gap appeared during the freeze")
            return
        self.manifest["consistent"] = {"seq": seq, "time": time.time(), "held_sec": round(held, 2)}
        write_json(self.manifest_path, self.manifest)
        self.last_consistent = time.time()
        log("consistent point at seq %s (froze %.2fs)" % (seq, held))

    def recovery_active(self):
        return self.recovery_thread is not None and self.recovery_thread.is_alive()

    # ---------------------------------------------------------------- apply
    def paused(self):
        """Records are parked only while a rebuild is copying the replica."""
        return os.path.exists(self.lock_path)

    def apply_one(self, r, payload):
        start, end = r.sector * 512, r.sector * 512 + r.len
        with self.lock:
            c = self.copying
            if c is not None and start < c[1] and end > c[0]:
                self.deferred.append((r, bytes(payload)))   # chunk in flight: apply after it lands
                return
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

    # ---------------------------------------------------------------- seed
    def check_seed(self):
        """rkcdp-seed writes manifest.seeded = {boot_id, time} once replica.raw
        holds a copy of the disk taken during this boot. Everything written
        since boot is in the kernel bitmap, so one recovery brings the replica
        current. A seed from another boot is unusable (bitmap gone): base copy."""
        m = load_json(self.manifest_path) or {}
        seed = m.get("seeded")
        if not seed:
            return
        self.manifest = m
        self.seed_required = False
        seed_path = os.path.join(self.root, "seed.raw")
        if self.replica.seed is None and os.path.exists(seed_path):
            self.replica.seed = SeedLayer(seed_path, self.size)
        if seed.get("boot_id") == self.boot_id:
            log("seed adopted (boot %s); draining changes since boot from the bitmap" % self.boot_id[:8])
            self.recovery_queue.append(-1)          # sentinel: not a ring gap, a seed drain
            self.manifest.update({"base_end_seq": 0, "base_time": seed.get("time", time.time())})
            write_json(self.manifest_path, self.manifest)
        else:
            log("seed is from a different boot; falling back to a base copy")
            self.base_thread = threading.Thread(target=self.base_copy, daemon=False)
            self.base_thread.start()

    # ---------------------------------------------------------------- status
    def status_loop(self):
        while not stop:
            try:
                self.write_status()
            except Exception as e:
                log("status: %s" % e)
            time.sleep(1.0)

    def write_status(self):
        st = dmcdp.status(self.cdp_fd)
        if self.seed_required:
            state = "WAITING"          # replica must be seeded by setup first
        elif self.base_progress:
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
            "node": self.conf["node"], "state": state, "time": time.time(), "boot_id": self.boot_id,
            "last_seq": self.last_seq, "last_time": self.last_time,
            "seq_next": st["seq_next"], "ring_used": st["ring_used"], "ring_size": st["ring_size"],
            "overflows": st["overflows"], "records": self.records, "bytes": self.bytes,
            "base_copy": dict(self.base_progress) if self.base_progress else None,
            "recovery_gap_seq": rec["gap_seq"] if rec else None,
            "consistent": self.manifest.get("consistent"),
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
        if "base_end_seq" not in self.manifest and not self.seed_required:
            self.base_thread = threading.Thread(target=self.base_copy, daemon=False)
            self.base_thread.start()
        if self.seed_required:
            log("waiting for seed (setup stage seed_mgr_replica); no base copy")

        poll = select.poll()
        poll.register(self.cdp_fd, select.POLLIN)
        pending = b""
        last_cycle = time.time()
        threading.Thread(target=self.status_loop, daemon=True).start()
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

            # seeded by setup? adopt it: replica = disk at boot + bitmap since boot
            if self.seed_required:
                self.check_seed()

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
                last_cycle = now
            if self.consistent_sec > 0 and now - self.last_consistent >= self.consistent_sec \
                    and "base_end_seq" in self.manifest:
                self.mark_consistent()
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
