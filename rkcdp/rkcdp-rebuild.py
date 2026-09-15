#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
rkcdp-rebuild - rebuild a dead node from its replica.raw on a Proxmox host.

  rkcdp-rebuild --node km-tgt1 --host 192.168.1.180 [--user root] [--from replica|seed]
                [--vmid 9107] [--storage local-lvm] [--bridge vmbr0]
                [--journal /replication/_kvmdr/rkcdp]
                [--host-journal /replication/_kvmdr/rkcdp]   path as the host sees it
                [--progress /run/rkcdp-rebuild-km-tgt1.json]
                [--force]      node still reports alive
                [--keep-copy]  leave the rebuild copy on NFS

Steps (each written to --progress):
  1 check     manifest/applied present, node not alive (unless --force)
  2 quiesce   lock the node dir; a live rkcdpd parks its writes
  3 copy      replica.raw -> rebuild/<node>-<ts>.raw (sparse)
  4 mark      touch /etc/kvmdr/rkcdp-rebuilt-standby inside the copy
  5 create    qm create + importdisk + boot on the host
  6 boot      wait for the node's IP to answer
The node comes up STANDBY; rkcdp-firstboot runs kvmdr-heal-standby.
Runs from a surviving manager or directly on a PVE host (needs NFS + ssh to host).
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import time

TOTAL = 6


class Progress:
    def __init__(self, path, node):
        self.path = path
        self.d = {"node": node, "step": 0, "total": TOTAL, "state": "running",
                  "msg": "", "steps": [], "started": time.time(), "error": None}
        self.flush()

    def step(self, n, msg):
        self.d["step"] = n
        self.d["msg"] = msg
        self.d["steps"].append({"n": n, "msg": msg, "t": time.time()})
        sys.stderr.write("[%d/%d] %s\n" % (n, TOTAL, msg))
        self.flush()

    def done(self, msg):
        self.d["state"] = "done"
        self.d["msg"] = msg
        self.d["finished"] = time.time()
        self.flush()

    def fail(self, err):
        self.d["state"] = "failed"
        self.d["error"] = str(err)
        self.d["finished"] = time.time()
        self.flush()

    def flush(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)


def sh(cmd, check=True, timeout=None):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError("%s: rc=%d %s" % (cmd.split()[0], r.returncode, (r.stderr or r.stdout).strip()))
    return r


def ssh_script(host, user, script, timeout=600):
    b = base64.b64encode(script.encode()).decode()
    cmd = ("ssh -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=10 "
           "%s@%s 'echo %s | base64 -d | bash'" % (user, host, b))
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("host script failed rc=%d: %s" % (r.returncode, (r.stderr or r.stdout).strip()[-800:]))
    return r.stdout


def load_json(p):
    with open(p) as f:
        return json.load(f)


def mark_standby(copy_path):
    """Loop-mount the copy's ext4 root and drop the standby marker."""
    loop = sh("losetup -Pf --show %s" % copy_path).stdout.strip()
    try:
        time.sleep(0.5)
        parts = sh("lsblk -rno NAME,FSTYPE,SIZE %s" % loop).stdout.split("\n")
        root = None
        best = -1
        for ln in parts:
            f = ln.split()
            if len(f) >= 2 and f[1] in ("ext4", "xfs") and f[0] != os.path.basename(loop):
                sz = sh("blockdev --getsize64 /dev/%s" % f[0]).stdout.strip()
                if int(sz) > best:
                    best, root = int(sz), "/dev/" + f[0]
        if not root:
            raise RuntimeError("no ext4/xfs partition in %s" % copy_path)
        mnt = "/run/rkcdp-mnt-%d" % os.getpid()
        os.makedirs(mnt, exist_ok=True)
        sh("mount %s %s" % (root, mnt))
        try:
            os.makedirs(os.path.join(mnt, "etc/kvmdr"), exist_ok=True)
            with open(os.path.join(mnt, "etc/kvmdr/rkcdp-rebuilt-standby"), "w") as f:
                f.write("%s\n" % time.strftime("%Y-%m-%dT%H:%M:%S"))
        finally:
            sh("umount %s" % mnt)
            os.rmdir(mnt)
    finally:
        sh("losetup -d %s" % loop, check=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--user", default="root")
    ap.add_argument("--vmid", type=int)
    ap.add_argument("--storage", default="local-lvm")
    ap.add_argument("--bridge", default="vmbr0")
    ap.add_argument("--journal", default="/replication/_kvmdr/rkcdp")
    ap.add_argument("--host-journal", default=None)
    ap.add_argument("--progress")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-copy", action="store_true")
    ap.add_argument("--no-standby", action="store_true", help="do not mark standby (both-managers-down case)")
    ap.add_argument("--from", dest="source", choices=["replica", "seed"], default="replica",
                    help="replica = latest disk (default); seed = clean post-setup copy")
    a = ap.parse_args()
    host_journal = a.host_journal or a.journal

    nd = os.path.join(a.journal, a.node)
    pg = Progress(a.progress, a.node)
    lock = os.path.join(nd, ".rebuild.lock")
    copy_path = None
    try:
        # 1 check
        pg.step(1, "checking replica for %s" % a.node)
        manifest = load_json(os.path.join(nd, "manifest.json"))
        status = load_json(st_path0) if os.path.exists(st_path0 := os.path.join(nd, "status.json")) else {}
        node = load_json(os.path.join(nd, "node.json")) if os.path.exists(os.path.join(nd, "node.json")) else {}
        replica = os.path.join(nd, "seed.raw" if a.source == "seed" else "replica.raw")
        if not os.path.exists(replica):
            raise RuntimeError("no %s for %s" % (os.path.basename(replica), a.node))
        if a.source == "replica" and "base_end_seq" not in manifest:
            raise RuntimeError("base copy never completed for %s" % a.node)
        st_path = os.path.join(nd, "status.json")
        if os.path.exists(st_path):
            age = time.time() - load_json(st_path).get("time", 0)
            if age < 60 and not a.force:
                raise RuntimeError("%s reported alive %.0fs ago; use --force to rebuild anyway" % (a.node, age))

        # 2 quiesce: lock the dir; a live rkcdpd parks its writes and reports PAUSED
        pg.step(2, "quiescing replica writes")
        with open(lock, "w") as f:
            f.write(str(os.getpid()))
        for _ in range(30):
            st = load_json(st_path0) if os.path.exists(st_path0) else {}
            if st.get("state") == "PAUSED" or time.time() - st.get("time", 0) > 30:
                break
            time.sleep(1)
        last_seq = status.get("last_seq")
        last_time = status.get("last_time")

        # 3 copy
        ts = time.strftime("%Y%m%d-%H%M%S")
        rdir = os.path.join(a.journal, "rebuild")
        os.makedirs(rdir, exist_ok=True)
        copy_path = os.path.join(rdir, "%s-%s.raw" % (a.node, ts))
        if a.source == "seed":
            pg.step(3, "copying clean seed (post-setup image)")
        else:
            pg.step(3, "copying replica (as of seq %s, %s)" % (last_seq, time.strftime("%H:%M:%S", time.localtime(last_time or 0))))
        sh("cp --sparse=always %s %s" % (replica, copy_path), timeout=3600)
        os.remove(lock)
        lock = None

        # 4 (the boot fence decides primary/replica on first boot; nothing to mark)
        pg.step(4, "copy ready; boot fence will place the node as replica")

        # 5 create VM
        mem = int(node.get("mem_mb") or 4096)
        cpus = int(node.get("cpus") or 2)
        macs = node.get("macs") or {}
        mac = next(iter(macs.values()), None)
        net = "virtio=%s,bridge=%s" % (mac, a.bridge) if mac else "virtio,bridge=%s" % a.bridge
        efi = node.get("firmware", "efi") == "efi"
        host_copy = os.path.join(host_journal, "rebuild", os.path.basename(copy_path))
        pg.step(5, "creating VM on %s (%d MB, %d cores, %s)" % (a.host, mem, cpus, "UEFI" if efi else "BIOS"))
        vmid_expr = str(a.vmid) if a.vmid else "$(for n in $(seq 9100 9199); do qm status $n >/dev/null 2>&1 || { echo $n; break; }; done)"
        bios = "--bios ovmf --efidisk0 %s:1,efitype=4m,pre-enrolled-keys=0" % a.storage if efi else ""
        script = f"""set -e
test -f {host_copy} || {{ echo "host cannot see {host_copy} (NFS not mounted?)" >&2; exit 2; }}
V={vmid_expr}
test -n "$V" || {{ echo "no free vmid" >&2; exit 3; }}
qm create $V --name {a.node} --memory {mem} --cores {cpus} --cpu host --ostype l26 \\
  --scsihw virtio-scsi-single --net0 {net} --agent enabled=1 {bios}
qm importdisk $V {host_copy} {a.storage} >/dev/null
DISK=$(qm config $V | grep -oE '^unused0: .+' | cut -d' ' -f2)
qm set $V --scsi0 $DISK,discard=on,iothread=1 --boot order=scsi0 >/dev/null
qm start $V
echo VMID=$V
"""
        out = ssh_script(a.host, a.user, script, timeout=3600)
        vmid = next((ln[5:] for ln in out.splitlines() if ln.startswith("VMID=")), "?")
        pg.d["vmid"] = vmid

        # 6 wait for boot
        ips = node.get("ips") or []
        pg.step(6, "VM %s started; waiting for %s" % (vmid, ips[0] if ips else "boot"))
        if ips:
            up = False
            for _ in range(150):
                if subprocess.run(["ping", "-c1", "-W1", ips[0]], capture_output=True).returncode == 0:
                    up = True
                    break
                time.sleep(2)
            if not up:
                raise RuntimeError("VM %s started but %s not reachable after 5 min" % (vmid, ips[0]))
        if not a.keep_copy:
            os.remove(copy_path)
        pg.done("%s rebuilt as VM %s on %s, booting as STANDBY (heal-standby runs on first boot)" % (a.node, vmid, a.host))
        return 0
    except Exception as e:
        pg.fail(e)
        sys.stderr.write("FAILED: %s\n" % e)
        return 1
    finally:
        if lock and os.path.exists(lock):
            os.remove(lock)


if __name__ == "__main__":
    sys.exit(main())
