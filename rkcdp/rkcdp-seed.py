#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
rkcdp-seed - seed this node's replica from its own VM disk, copied by the host.

Runs on the manager itself (pairing stage seed_mgr_replica, or by hand):
  1. find which Proxmox host runs this VM (match our MAC across
     /opt/kvmdr/hypervisors.json hosts) and the VM's disk
  2. on that host:  qemu-img convert -p disk -> <journal>/<node>/seed.raw
     (host-local read, NFS write; the VM keeps running)
  3. copy seed.raw into replica.raw in place (dd conv=notrunc,sparse) so the
     running rkcdpd keeps its file descriptor
  4. write manifest.seeded = {boot_id, time}; rkcdpd sees it, drains the
     changes made since boot from the kernel bitmap, and reports CDP

seed.raw is kept: it is the "clean, post-setup" image for a rebuild after
ransomware/corruption. Progress goes to <node>/seed.json (phase, percent).

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
    script = ("for v in $(qm list 2>/dev/null | awk 'NR>1{print $1}'); do "
              "qm config $v 2>/dev/null | grep -qiE '%s' && echo $v; done" % pat)
    for h in hosts:
        ip = h.get("ip") or h.get("hostname")
        if not ip:
            continue
        r = ssh(ip, script, timeout=60)
        if r.returncode == 0 and r.stdout.strip():
            return ip, int(r.stdout.split()[0])
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

    def progress(phase, pct=None, msg="", **kw):
        d = {"phase": phase, "percent": pct, "msg": msg, "time": time.time(), "node": node}
        d.update(kw)
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

        # 2. seed.raw on the host, streaming progress
        seed = os.path.join(nd, "seed.raw")
        cmd = SSH + ["root@%s" % host,
                     "qemu-img convert -p -f raw -O raw -S 4k %s %s.tmp && mv -f %s.tmp %s" % (disk, seed, seed, seed)]
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        last = -1
        buf = ""
        t0 = time.time()
        samples = []          # (time, fraction) for a moving rate
        while True:
            ch = p.stdout.read(1)
            if not ch:
                break
            buf += ch
            if ch in "\r\n":
                m = re.search(r"\((\d+(?:\.\d+)?)/100%\)", buf)
                if m:
                    frac = float(m.group(1)) / 100.0
                    pct = int(frac * 100)
                    now = time.time()
                    samples.append((now, frac))
                    samples = [x for x in samples if now - x[0] <= 60] or samples[-1:]
                    rate = 0.0
                    if len(samples) >= 2 and samples[-1][0] > samples[0][0]:
                        rate = (samples[-1][1] - samples[0][1]) * total / (samples[-1][0] - samples[0][0])
                    eta = int((1 - frac) * total / rate) if rate > 0 else None
                    if pct != last or now - t0 > 5:
                        progress("seed", pct, "copying disk on %s" % host, host=host, vmid=vmid,
                                 done=int(frac * total), total=total,
                                 mbps=round(rate / 1048576, 1), eta_sec=eta)
                        last = pct
                        t0 = now
                buf = ""
        if p.wait() != 0 or not os.path.exists(seed):
            raise RuntimeError("qemu-img convert failed on %s (rc=%s)" % (host, p.returncode))
        progress("seed", 100, "seed.raw written", host=host, vmid=vmid)

        # 3. replica.raw <- seed.raw, in place (rkcdpd keeps its open fd)
        progress("replica", 0, "copying seed into replica.raw")
        replica = os.path.join(nd, "replica.raw")
        size = os.path.getsize(seed)
        if not os.path.exists(replica):
            with open(replica, "wb") as f:
                f.truncate(size)
        r = subprocess.run(["dd", "if=%s" % seed, "of=%s" % replica, "bs=4M", "conv=notrunc,sparse", "status=none"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("dd into replica.raw failed: %s" % r.stderr.strip()[:200])
        progress("replica", 100, "replica.raw seeded")

        # 4. tell rkcdpd
        mp = os.path.join(nd, "manifest.json")
        m = load_json(mp, {}) or {}
        m["seeded"] = {"boot_id": boot_id(), "time": time.time(), "host": host, "vmid": vmid, "disk": vol}
        m["seed_size"] = size
        write_json(mp, m)
        progress("drain", None, "waiting for rkcdpd to drain changes since boot")

        # 5. wait for CDP (rkcdpd polls the manifest every second)
        st_path = os.path.join(nd, "status.json")
        for _ in range(1800):
            st = load_json(st_path, {}) or {}
            if st.get("state") == "CDP":
                progress("done", 100, "protected")
                return 0
            if st.get("state") not in ("WAITING", "RECOVERING", "CDP", None):
                progress("drain", None, "rkcdpd state %s" % st.get("state"))
            time.sleep(2)
        raise RuntimeError("rkcdpd did not reach CDP within 60 min")
    except Exception as e:
        progress("failed", None, str(e))
        log("FAILED: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
