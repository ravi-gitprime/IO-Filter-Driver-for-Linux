#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
rkcdp-setup - single-file installer for linux_rkcdp on a KVMDR manager.

Everything it needs is embedded at build time (assets/):
  dm-cdp.ko, rkcdpd, rkcdp-rebuild, rkcdp-firstboot.sh, systemd units,
  initramfs hook + local-top script, udev rule.

  rkcdp-setup            install/upgrade (idempotent), regenerate initramfs
  rkcdp-setup --check    report what is installed, exit 1 if incomplete
  rkcdp-setup --remove   remove hook/units/binaries (module stays until reboot)

Run once on mastercut (kvmdr-scrub calls it) so every image boots with dm-cdp
under the root disk; run again on a live node to upgrade the userspace.
"""
import json
import os
import shutil
import subprocess
import sys

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
KVER = os.uname().release
JOURNAL_DEFAULT = "/replication/_kvmdr/rkcdp"

FILES = [
    # (asset name,                 destination,                                            mode)
    ("dm-cdp.ko",                  f"/lib/modules/{KVER}/extra/dm-cdp.ko",                 0o644),
    ("rkcdpd",                     "/opt/rkcdp/rkcdpd",                                     0o755),
    ("rkcdp-rebuild",              "/opt/rkcdp/rkcdp-rebuild",                              0o755),
    ("rkcdp-seed",                 "/opt/rkcdp/rkcdp-seed",                                 0o755),
    ("rkcdpd.service",             "/etc/systemd/system/rkcdpd.service",                    0o644),
    ("hook",                       "/etc/initramfs-tools/hooks/rkcdp",                      0o755),
    ("local-top",                  "/etc/initramfs-tools/scripts/local-top/rkcdp",          0o755),
    ("99-rkcdp.rules",             "/etc/udev/rules.d/99-rkcdp.rules",                      0o644),
]
CONF_DIR = "/etc/rkcdp"
CONF = f"{CONF_DIR}/rkcdp.conf"


def sh(cmd, check=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise SystemExit(f"rkcdp-setup: {cmd.split()[0]} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def install_file(asset, dst, mode):
    src = os.path.join(ASSETS, asset)
    if not os.path.exists(src):
        raise SystemExit(f"rkcdp-setup: embedded asset missing: {asset}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".new"
    shutil.copyfile(src, tmp)
    os.chmod(tmp, mode)
    os.replace(tmp, dst)          # atomic; safe over a running binary


def root_disk():
    src = sh("findmnt -n -o SOURCE /").stdout.strip()
    name = os.path.basename(os.path.realpath(src))
    parent = os.path.realpath(f"/sys/class/block/{name}/..")
    return "/dev/" + os.path.basename(parent) if os.path.basename(parent) != "block" else src


def do_install(journal):
    if os.geteuid() != 0:
        raise SystemExit("rkcdp-setup: run as root")
    sh("apt-get install -y -q kpartx dmsetup >/dev/null 2>&1", check=False)

    for asset, dst, mode in FILES:
        install_file(asset, dst, mode)
    # leftovers from earlier builds
    for p in ("/etc/systemd/system/rkcdp-applier.service", "/opt/rkcdp/rkcdp-applier",
              "/etc/systemd/system/rkcdp-firstboot.service", "/opt/rkcdp/rkcdp-firstboot.sh"):
        if os.path.exists(p):
            os.remove(p)
    for p in ("/opt/rkcdp/rkcdpd.py", "/opt/rkcdp/rkcdp-rebuild.py", "/opt/rkcdp/dmcdp.py",
              "/opt/rkcdp/rkcdp-applier.py"):
        if os.path.exists(p):
            os.remove(p)

    os.makedirs(CONF_DIR, exist_ok=True)
    with open(f"{CONF_DIR}/initramfs.conf", "w") as f:
        f.write("DM_NAME=rkcdp\n")
    os.makedirs("/etc/modprobe.d", exist_ok=True)
    with open("/etc/modprobe.d/dm-cdp.conf", "w") as f:
        f.write("options dm-cdp ring_mb=1024\n")
    os.makedirs("/etc/initramfs-tools/conf.d", exist_ok=True)
    with open("/etc/initramfs-tools/conf.d/resume", "w") as f:
        f.write("RESUME=none\n")
    if not os.path.exists(CONF):
        with open(CONF, "w") as f:
            json.dump({"dm_name": "rkcdp", "journal": journal, "cycle_sec": 1.0,
                       "base_copy_mbps": 15, "seed_required": True}, f, indent=1)
            f.write("\n")

    # cgroup read cap on the disk rkcdpd reads (works with any I/O scheduler)
    dev = root_disk()
    os.makedirs("/etc/systemd/system/rkcdpd.service.d", exist_ok=True)
    with open("/etc/systemd/system/rkcdpd.service.d/io.conf", "w") as f:
        f.write(f"[Service]\nIOReadBandwidthMax={dev} 20M\n")

    sh("depmod -a")
    sh("update-initramfs -u")
    sh("systemctl daemon-reload")
    sh("systemctl disable rkcdp-applier rkcdp-firstboot >/dev/null 2>&1", check=False)
    # rkcdpd is NOT enabled here: setup's seed stage starts it once the node
    # has its identity and its replica is seeded. An already-running daemon
    # (upgrade on a live node) is restarted in place.
    if sh("systemctl is-active rkcdpd", check=False).stdout.strip() == "active":
        sh("systemctl restart rkcdpd", check=False)
        print("rkcdp-setup: upgraded, rkcdpd restarted")
    else:
        print("rkcdp-setup: installed. dm-cdp wraps the root disk from the next boot; rkcdpd starts after seeding.")


def do_check():
    ok = True
    def line(label, good, detail=""):
        nonlocal ok
        ok = ok and good
        print(f"  {label:<18}{'ok' if good else 'MISSING'}  {detail}")
    line("module", os.path.exists(f"/lib/modules/{KVER}/extra/dm-cdp.ko"), KVER)
    line("initramfs hook", os.path.exists("/etc/initramfs-tools/scripts/local-top/rkcdp"))
    n = sh(f"lsinitramfs /boot/initrd.img-{KVER} 2>/dev/null | grep -c dm-cdp.ko", check=False).stdout.strip()
    line("module in initrd", n not in ("", "0"))
    line("daemon binary", os.path.exists("/opt/rkcdp/rkcdpd"))
    en = sh("systemctl is-enabled rkcdpd 2>/dev/null", check=False).stdout.strip()
    print(f"  {'rkcdpd unit':<18}{en or 'absent'}  (enabled by the seed stage)")
    wrapped = sh("findmnt -n -o SOURCE /", check=False).stdout.strip().startswith("/dev/mapper/rkcdp")
    print(f"  {'root wrapped':<18}{'yes' if wrapped else 'no (takes effect at next boot)'}")
    return 0 if ok else 1


def do_remove():
    sh("systemctl disable --now rkcdpd >/dev/null 2>&1", check=False)
    for _, dst, _ in FILES:
        if dst.startswith(f"/lib/modules/"):
            continue
        if os.path.exists(dst):
            os.remove(dst)
    shutil.rmtree("/etc/systemd/system/rkcdpd.service.d", ignore_errors=True)
    for p in ("/etc/udev/rules.d/99-rkcdp.rules", f"{CONF_DIR}/initramfs.conf"):
        if os.path.exists(p):
            os.remove(p)
    sh("update-initramfs -u")
    sh("systemctl daemon-reload")
    print("rkcdp-setup: removed; root boots unwrapped from the next boot")


def main():
    a = sys.argv[1:]
    journal = JOURNAL_DEFAULT
    if "--journal" in a:
        journal = a[a.index("--journal") + 1]
    if "--check" in a:
        return do_check()
    if "--remove" in a:
        do_remove()
        return 0
    do_install(journal)
    return 0


if __name__ == "__main__":
    sys.exit(main())
