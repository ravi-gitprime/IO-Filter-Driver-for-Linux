#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
rkcdpd - ship every write of this node's disk to the replica journal.

Flow:
  1. attach dm-cdp under <device> (dmsetup create <dm_name>)      [once]
  2. open /dev/cdpN and start writing 1 s cycle files to NFS        [always]
  3. if no replica.raw yet: live copy of the disk -> replica.raw      [once]
     (writes during the copy are already in the cycles: no gap)
  4. on GAP_BEFORE: read dirty bitmap, re-read those extents, emit a
     recovery cycle. CDP degrades to CBT, never full resync.
  5. write status.json every cycle, node.json hourly

Config: /etc/rkcdp/rkcdp.conf (JSON), keys:
  device      omit = use the initramfs-wrapped root disk; or "/dev/sdb" etc.
  dm_name     "rkcdp"              device-mapper name
  journal     "/replication/_kvmdr/rkcdp"
  node        hostname             directory name under journal
  cycle_sec   1.0
"""
import json
import os
import socket
import subprocess
import sys
import time
import signal
import struct
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dmcdp  # noqa: E402

CONF = "/etc/rkcdp/rkcdp.conf"
READ_SZ = 16 << 20
COPY_CHUNK = 4 << 20

stop = False
base_progress = {}       # filled by base_copy(), reported in status.json


def log(msg):
    sys.stderr.write("rkcdpd: %s\n" % msg)
    sys.stderr.flush()


def on_sig(*_):
    global stop
    stop = True


def run(cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def load_conf():
    conf = {"device": None, "dm_name": "rkcdp",
            "journal": "/replication/_kvmdr/rkcdp",
            "node": socket.gethostname(), "cycle_sec": 1.0,
            "base_copy_mbps": 40}
    if os.path.exists(CONF):
        with open(CONF) as f:
            conf.update(json.load(f))
    return conf


def blockdev_sectors(dev):
    return int(run(["blockdev", "--getsz", dev]).stdout.strip())


def root_disk():
    """Whole disk that holds / (parent of the root partition)."""
    src = run(["findmnt", "-n", "-o", "SOURCE", "/"]).stdout.strip()
    name = os.path.basename(os.path.realpath(src))
    parent = os.path.realpath("/sys/class/block/%s/.." % name)
    return "/dev/" + os.path.basename(parent)


def table_device(name):
    """Backing device of an existing cdp mapping (from its table's major:minor)."""
    tbl = run(["dmsetup", "table", name]).stdout.split()
    if len(tbl) < 5 or tbl[2] != "cdp":
        raise SystemExit("dm %s is not a cdp target: %s" % (name, " ".join(tbl)))
    devname = os.path.basename(os.path.realpath("/sys/dev/block/" + tbl[3]))
    return "/dev/" + devname


def ensure_target(conf):
    """Use the existing dm-cdp mapping (initramfs-wrapped root disk) or create
    one for conf['device']. Sets conf['device'] to the real backing disk.
    Returns /dev/cdpN."""
    name = conf["dm_name"]
    st = run(["dmsetup", "status", name], check=False)
    if st.returncode == 0:
        conf["device"] = table_device(name)
    else:
        if not conf.get("device"):
            conf["device"] = root_disk()
            if conf["device"] == root_disk():
                raise SystemExit("root disk %s is not wrapped: install the initramfs hook and reboot, "
                                 "or set 'device' in %s to a non-root disk" % (conf["device"], CONF))
        if run(["lsmod"]).stdout.find("dm_cdp") < 0:
            run(["modprobe", "dm-cdp"])
        sectors = blockdev_sectors(conf["device"])
        run(["dmsetup", "create", name, "--table",
             "0 %d cdp %s 0" % (sectors, conf["device"])])
        st = run(["dmsetup", "status", name])
        log("attached %s as /dev/mapper/%s" % (conf["device"], name))
    fields = st.stdout.split()
    if len(fields) < 4 or fields[2] != "cdp":
        raise SystemExit("dm %s is not a cdp target: %s" % (name, st.stdout.strip()))
    return "/dev/" + fields[3]


class Journal:
    def __init__(self, conf):
        self.root = os.path.join(conf["journal"], conf["node"])
        self.cycles = os.path.join(self.root, "cycles")
        os.makedirs(self.cycles, exist_ok=True)
        self.manifest_path = os.path.join(self.root, "manifest.json")
        self.manifest = self._load(self.manifest_path)

    @staticmethod
    def _load(p):
        if os.path.exists(p):
            with open(p) as f:
                return json.load(f)
        return None

    def write_json(self, name, obj):
        p = os.path.join(self.root, name)
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    def cycle_path(self, seq, kind):
        # kind 'a' = bitmap recovery data, 'b' = normal stream; 'a' sorts first
        return os.path.join(self.cycles, "%020d.%s.bin" % (seq, kind))


class Cycle:
    """One cycle file, written as .tmp and renamed complete on close."""

    def __init__(self, path):
        self.path = path
        self.tmp = path + ".tmp"
        self.f = open(self.tmp, "wb")
        self.bytes = 0
        self.first_seq = None
        self.last_seq = None

    def write(self, data, first_seq, last_seq):
        self.f.write(data)
        self.bytes += len(data)
        if self.first_seq is None:
            self.first_seq = first_seq
        self.last_seq = last_seq

    def close(self):
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()
        if self.bytes == 0:
            os.unlink(self.tmp)
            return None
        os.replace(self.tmp, self.path)
        return self.path


def base_copy(conf, jn, cdp_fd, src):
    """Live copy of the disk into replica.raw (sparse). Journaling is already on."""
    dst = os.path.join(jn.root, "replica.raw")
    tmp = dst + ".tmp"
    size = blockdev_sectors(src) * 512
    log("base copy %s (%d MiB) -> %s" % (src, size >> 20, dst))
    t0 = time.time()
    mbps = float(conf.get("base_copy_mbps") or 0)
    base_progress.update({"done": 0, "total": size, "started": t0, "mbps": 0.0, "eta_sec": None})
    with open(src, "rb", buffering=0) as fi, open(tmp, "wb") as fo:
        fo.truncate(size)
        off = 0
        zero = bytes(COPY_CHUNK)
        while off < size and not stop:
            buf = fi.read(COPY_CHUNK)
            if not buf:
                break
            if buf != zero[:len(buf)]:
                fo.seek(off)
                fo.write(buf)
            off += len(buf)
            el = time.time() - t0
            rate = off / el if el > 0 else 0
            base_progress.update({"done": off, "mbps": round(rate / 1048576, 1),
                                  "eta_sec": int((size - off) / rate) if rate > 0 else None})
            if mbps > 0:
                # throttle: never let the base copy starve the node's own I/O
                expected = off / (mbps * 1048576)
                ahead = expected - (time.time() - t0)
                if ahead > 0:
                    time.sleep(min(ahead, 1.0))
        fo.flush()
        os.fsync(fo.fileno())
    if stop:
        os.unlink(tmp)
        raise SystemExit("interrupted during base copy")
    os.replace(tmp, dst)
    base_progress.clear()
    end_seq = dmcdp.status(cdp_fd)["seq_next"]
    log("base copy done in %.0fs, base_end_seq=%d" % (time.time() - t0, end_seq))
    return {"size": size, "base_end_seq": end_seq, "base_time": time.time()}


def node_json(conf):
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
    ips = run(["hostname", "-I"], check=False).stdout.split()
    return {"hostname": socket.gethostname(), "device": conf["device"],
            "disk_bytes": blockdev_sectors(conf["device"]) * 512,
            "cpus": os.cpu_count(), "mem_mb": mem_kb // 1024,
            "macs": macs, "ips": ips,
            "firmware": "efi" if os.path.exists("/sys/firmware/efi") else "bios",
            "time": time.time()}


class Recovery(threading.Thread):
    """Ring overflowed: re-read every dirty chunk from the device into a
    recovery cycle that sorts before the record carrying GAP_BEFORE.
    Runs in its own thread so the ship loop keeps draining the ring;
    the .tmp placeholder is created up front so the applier holds at it."""

    def __init__(self, jn, cdp_fd, dm_dev, gap_seq, mbps):
        super().__init__(daemon=True)
        self.jn, self.cdp_fd, self.dm_dev, self.gap_seq, self.mbps = jn, cdp_fd, dm_dev, gap_seq, mbps
        self.path = jn.cycle_path(gap_seq, "a")
        open(self.path + ".tmp", "wb").close()   # placeholder: applier stops here

    def run(self):
        try:
            recover_from_bitmap(self.jn, self.cdp_fd, self.dm_dev, self.gap_seq, self.path, self.mbps)
        except Exception as e:
            log("bitmap recovery failed: %s" % e)


def recover_from_bitmap(jn, cdp_fd, dm_dev, gap_seq, path, mbps=0):
    bm, chunk, nbits = dmcdp.read_bitmap(cdp_fd, clear=True)
    total = 0
    ts = time.time_ns()
    t0 = time.time()
    with open(dm_dev, "rb", buffering=0) as dev, open(path + ".tmp", "wb") as out:
        for sector, nsect in dmcdp.bitmap_extents(bm, chunk, nbits):
            off = sector * 512
            remaining = nsect * 512
            while remaining:
                n = min(remaining, COPY_CHUNK)
                dev.seek(off)
                data = dev.read(n)
                out.write(dmcdp.pack_rec(dmcdp.REC_WRITE, 0, gap_seq, off // 512, len(data), ts, data))
                off += n
                remaining -= n
                total += n
                if mbps > 0:
                    ahead = total / (mbps * 1048576) - (time.time() - t0)
                    if ahead > 0:
                        time.sleep(min(ahead, 1.0))
        out.flush()
        os.fsync(out.fileno())
    os.replace(path + ".tmp", path)
    log("bitmap recovery: %d MiB re-read into %s" % (total >> 20, os.path.basename(path)))


def main():
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    conf = load_conf()
    jn = Journal(conf)
    cdp_path = ensure_target(conf)
    dm_dev = "/dev/mapper/" + conf["dm_name"]

    cdp_fd = os.open(cdp_path, os.O_RDONLY | os.O_NONBLOCK)
    st = dmcdp.status(cdp_fd)
    log("reading %s (dev %d MiB, ring %d MiB)" % (cdp_path, st["dev_sectors"] // 2048, st["ring_size"] >> 20))

    if jn.manifest is None:
        jn.manifest = {"node": conf["node"], "device": conf["device"],
                       "dm_name": conf["dm_name"], "gen": 1, "abi": dmcdp.ABI_VERSION}
        jn.write_json("manifest.json", jn.manifest)

    # start shipping BEFORE the base copy so nothing is missed
    shipper = threading.Thread(target=ship_loop, args=(conf, jn, cdp_fd, dm_dev), daemon=True)
    shipper.start()

    if "base_end_seq" not in jn.manifest:
        jn.manifest.update(base_copy(conf, jn, cdp_fd, dm_dev))
        jn.write_json("manifest.json", jn.manifest)

    last_node = 0
    while not stop:
        if time.time() - last_node > 3600:
            jn.write_json("node.json", node_json(conf))
            last_node = time.time()
        time.sleep(1)
    shipper.join(5)
    log("stopped")


def ship_loop(conf, jn, cdp_fd, dm_dev):
    import select
    poll = select.poll()
    poll.register(cdp_fd, select.POLLIN)
    cycle_sec = float(conf["cycle_sec"])
    pending = b""
    cur = None
    cur_started = 0.0
    last_seq = None
    shipped_bytes = 0
    last_status = 0.0
    recovery = None          # running Recovery thread
    recovery_queue = []      # created (placeholder written) but not yet started
    mbps = float(conf.get("base_copy_mbps") or 0)

    while not stop:
        if cur is None:
            cur_started = time.time()
        poll.poll(200)
        try:
            data = os.read(cdp_fd, READ_SZ)
        except BlockingIOError:
            data = b""
        except OSError as e:
            log("read error: %s" % e)
            time.sleep(1)
            continue

        if data:
            buf = pending + data
            first = last = None
            consumed = 0
            gap = None
            for r, _ in dmcdp.iter_records(buf):
                if r.flags & dmcdp.F_GAP_BEFORE:
                    gap = r.seq
                    break
                if first is None:
                    first = r.seq
                last = r.seq
                consumed += r.rec_len
            if gap is not None:
                # close what we have, recover, then continue with the gap record
                if consumed:
                    if cur is None:
                        cur = Cycle(jn.cycle_path(first, "b"))
                    cur.write(buf[:consumed], first, last)
                    cur.close()
                    cur = None
                # placeholder is written now (ordering); the thread starts when
                # the previous recovery is done. Draining never waits.
                recovery_queue.append(Recovery(jn, cdp_fd, dm_dev, gap, mbps))
                # strip the flag so the applier treats it as a normal record
                rest = bytearray(buf[consumed:])
                struct.pack_into("<I", rest, 8, struct.unpack_from("<I", rest, 8)[0] & ~dmcdp.F_GAP_BEFORE)
                pending = bytes(rest)
                continue
            if consumed:
                if cur is None:
                    cur = Cycle(jn.cycle_path(first, "b"))
                cur.write(buf[:consumed], first, last)
                last_seq = last
                shipped_bytes += consumed
            pending = buf[consumed:]

        if cur is not None and time.time() - cur_started >= cycle_sec:
            cur.close()
            cur = None

        if recovery_queue and (recovery is None or not recovery.is_alive()):
            recovery = recovery_queue.pop(0)
            recovery.start()

        if time.time() - last_status >= cycle_sec:
            st = dmcdp.status(cdp_fd)
            state = "CDP"
            if st["flags"] & dmcdp.S_OVERFLOWED:
                state = "BITMAP"
            if base_progress:
                state = "SYNCING"
            jn.write_json("status.json", {
                "node": conf["node"], "state": state, "time": time.time(),
                "base_copy": dict(base_progress) if base_progress else None,
                "last_seq": last_seq, "seq_next": st["seq_next"],
                "ring_used": st["ring_used"], "ring_size": st["ring_size"],
                "overflows": st["overflows"], "shipped_bytes": shipped_bytes})
            last_status = time.time()

    if cur is not None:
        cur.close()


if __name__ == "__main__":
    main()
