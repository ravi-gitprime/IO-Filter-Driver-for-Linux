#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
rkcdp-seed - seed this node's replica from its own VM disk, copied by the host.

Runs on the manager itself (pairing stage seed_mgr_replica, or by hand):
  1. find which Proxmox host runs this VM (match our MAC across
     /opt/kvmdr/hypervisors.json hosts) and the VM's disk
  2. on that host: chunked dd of the disk -> <journal>/<node>/replica.raw
     (64 MiB chunks, sparse, in place; the VM keeps running). Resumable:
     seed.json remembers the last finished chunk, a retry continues from it
  3. create an empty sparse seed.raw beside it and record the seed point;
     from then on rkcdpd saves each chunk's seed-time content into seed.raw
     the first time that chunk changes (copy-on-first-write)
  4. write manifest.seeded = {boot_id, time}; rkcdpd sees it, drains the
     changes made since boot from the kernel bitmap, and reports CDP

Progress goes to <node>/seed.json (phase, percent, bytes, rate).

  rkcdp-seed [--journal DIR] [--host IP] [--vmid N]   (both auto-detected)
  rkcdp-seed --status                                  print seed.json
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

CONF = "/etc/rkcdp/rkcdp.conf"
HOSTS_FILE = "/opt/kvmdr/hypervisors.json"
SSH = ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


def log(msg):
    sys.stderr.write("rkcdp-seed: %s\n" % msg)
    sys.stderr.flush()


def ssh(host, cmd, timeout=60):
    return subprocess.run(SSH + ["root@%s" % host, cmd], capture_output=True, text=True, timeout=timeout)


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def my_macs():
    macs = set()
    for n in os.listdir("/sys/class/net"):
        if n == "lo":
            continue
        try:
            with open("/sys/class/net/%s/address" % n) as f:
                macs.add(f.read().strip().lower())
        except OSError:
            pass
    return macs


def boot_id():
    with open("/proc/sys/kernel/random/boot_id") as f:
        return f.read().strip()


def find_vm(hosts, macs):
    """Return (host_ip, vmid) of the VM whose config carries one of our MACs."""
    pat = "|".join(re.escape(m) for m in macs)
    # judge by output only: grep's exit code is 1 whenever a file does not match
    script = "grep -liE '%s' /etc/pve/qemu-server/*.conf 2>/dev/null; true" % pat
    for h in hosts:
        ip = h.get("ip") or h.get("hostname")
        if not ip:
            continue
        r = ssh(ip, script, timeout=30)
        m = re.search(r"/(\d+)\.conf", r.stdout or "")
        if m:
            return ip, int(m.group(1))
    return None, None


def disk_size(host, path):
    r = ssh(host, "blockdev --getsize64 %s 2>/dev/null || stat -c %%s %s" % (path, path))
    try:
        return int(r.stdout.split()[0])
    except (ValueError, IndexError):
        return 0


def vm_disk(host, vmid):
    r = ssh(host, "qm config %d | grep -E '^(scsi|virtio|sata)0:' | head -1" % vmid)
    m = re.search(r":\s*([^,\s]+)", r.stdout)
    if not m:
        raise SystemExit("cannot find boot disk of VM %d on %s: %s" % (vmid, host, r.stdout.strip() or r.stderr.strip()))
    vol = m.group(1)
    r = ssh(host, "pvesm path %s" % vol)
    path = r.stdout.strip()
    if r.returncode != 0 or not path:
        raise SystemExit("pvesm path %s failed on %s: %s" % (vol, host, r.stderr.strip()))
    return vol, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal")
    ap.add_argument("--host")
    ap.add_argument("--vmid", type=int)
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    conf = {"journal": "/replication/_kvmdr/rkcdp", "node": socket.gethostname()}
    conf.update(load_json(CONF, {}) or {})
    journal = a.journal or conf["journal"]
    node = conf.get("node") or socket.gethostname()
    nd = os.path.join(journal, node)
    os.makedirs(nd, exist_ok=True)
    seed_json = os.path.join(nd, "seed.json")

    if a.status:
        print(json.dumps(load_json(seed_json, {"phase": "none"}), indent=1))
        return 0

    resume = dict((load_json(seed_json, {}) or {}).get("resume") or {})

    def progress(phase, pct=None, msg="", **kw):
        d = {"phase": phase, "percent": pct, "msg": msg, "time": time.time(), "node": node}
        d.update(kw)
        if "chunk" in kw:
            resume.update(host=kw.get("host"), vmid=kw.get("vmid"), total=kw.get("total"), chunk=kw["chunk"])
        if phase == "done":
            resume.clear()
        d["resume"] = dict(resume)
        write_json(seed_json, d)
        log("%s %s%s" % (phase, ("%d%% " % pct) if pct is not None else "", msg))

    try:
        # 1. locate the VM
        progress("locate", None, "finding this VM on its host")
        host, vmid = a.host, a.vmid
        if not (host and vmid):
            hosts = load_json(HOSTS_FILE, []) or []
            if a.host:
                hosts = [{"ip": a.host}]
            host, vmid = find_vm(hosts, my_macs())
        if not host or not vmid:
            raise RuntimeError("could not find a VM with this node's MAC on any host in %s" % HOSTS_FILE)
        vol, disk = vm_disk(host, vmid)
        total = disk_size(host, disk)
        r = ssh(host, "test -d %s && echo ok" % journal)
        if "ok" not in r.stdout:
            raise RuntimeError("host %s cannot see %s (replication share not mounted there)" % (host, journal))
        progress("locate", 0, "VM %d on %s, disk %s" % (vmid, host, vol), host=host, vmid=vmid, disk=disk)

        # 2. replica.raw on the host, in place, chunked and resumable:
        #    64 MiB chunks, one ssh session; the host prints each finished
        #    chunk index, seed.json keeps it, a retry resumes from there.
        replica = os.path.join(nd, "replica.raw")
        if not os.path.exists(replica):
            with open(replica, "wb") as f:
                f.truncate(total)
        CH = 64 << 20
        nchunks = (total + CH - 1) // CH
        start = 0
        if resume.get("host") == host and resume.get("vmid") == vmid and resume.get("total") == total \
                and isinstance(resume.get("chunk"), int):
            start = resume["chunk"] + 1
            log("resuming copy at chunk %d/%d" % (start, nchunks))
        script = ("for i in $(seq %d %d); do "
                  "dd if=%s of=%s bs=%d skip=$i seek=$i count=1 conv=notrunc,sparse iflag=fullblock status=none || exit 1; "
                  "echo $i; done" % (start, nchunks - 1, disk, replica, CH))
        cmd = SSH + ["root@%s" % host, script]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        t0 = time.time()
        samples = []
        last_pct = -1
        last_w = 0
        for line in p.stdout:
            line = line.strip()
            if not line.isdigit():
                continue
            i = int(line)
            done_b = min(total, (i + 1) * CH)
            frac = done_b / total
            pct = int(frac * 100)
            now = time.time()
            samples.append((now, done_b))
            samples = [x for x in samples if now - x[0] <= 60] or samples[-1:]
            rate = 0.0
            if len(samples) >= 2 and samples[-1][0] > samples[0][0]:
                rate = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
            eta = int((total - done_b) / rate) if rate > 0 else None
            if pct != last_pct or now - last_w > 5:
                progress("copy", pct, "copying disk on %s" % host, host=host, vmid=vmid,
                         chunk=i, done=done_b, total=total, mbps=round(rate / 1048576, 1), eta_sec=eta)
                last_pct, last_w = pct, now
        rc = p.wait()
        if rc != 0:
            raise RuntimeError("copy failed on %s (rc=%s): %s" % (host, rc, (p.stderr.read() or "")[-200:].strip()))
        progress("copy", 100, "replica.raw written", host=host, vmid=vmid)

        # 3. seed point: empty sparse seed.raw; rkcdpd fills it copy-on-first-write
        seed = os.path.join(nd, "seed.raw")
        if not os.path.exists(seed):
            with open(seed, "wb") as f:
                f.truncate(total)

        # 4. tell rkcdpd
        mp = os.path.join(nd, "manifest.json")
        m = load_json(mp, {}) or {}
        now = time.time()
        m["seeded"] = {"boot_id": boot_id(), "time": now, "host": host, "vmid": vmid, "disk": vol}
        m["seed_point"] = {"time": now, "boot_id": boot_id()}
        m["size"] = total
        write_json(mp, m)
        progress("drain", None, "waiting for rkcdpd to drain changes since boot")

        # 5. wait for CDP; no cap — a live daemon is progress, only a dead
        #    one (no status update for 5 min) or a failure state ends this
        st_path = os.path.join(nd, "status.json")
        while True:
            st = load_json(st_path, {}) or {}
            state = st.get("state")
            if state == "CDP":
                progress("done", 100, "protected")
                return 0
            age = time.time() - float(st.get("time") or 0)
            if st and age > 300:
                raise RuntimeError("rkcdpd stopped updating status (%d s ago)" % age)
            if state not in ("WAITING", "RECOVERING", "CDP", None):
                progress("drain", None, "rkcdpd state %s" % state)
            time.sleep(2)
    except Exception as e:
        progress("failed", None, str(e))
        log("FAILED: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
