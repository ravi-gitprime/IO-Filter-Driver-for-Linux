#!/bin/bash
# Install linux_rkcdp on a Debian 12 node: module, daemon, initramfs hook.
# Run from the repo checkout (or an unpacked copy) as root. Reboot afterwards.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
KO=${KO:-$HERE/../dm-cdp.ko}
JOURNAL=${JOURNAL:-/replication/_kvmdr/rkcdp}

[ -f "$KO" ] || { echo "dm-cdp.ko not found at $KO (build it first or set KO=)"; exit 1; }
apt-get install -y -q kpartx dmsetup >/dev/null

install -d /lib/modules/$(uname -r)/extra /opt/rkcdp /etc/rkcdp
install -m 644 "$KO" /lib/modules/$(uname -r)/extra/dm-cdp.ko
depmod -a
if [ -x "$HERE/dist/rkcdpd" ]; then
    # compiled (Nuitka) binaries
    install -m 755 "$HERE"/dist/rkcdpd "$HERE"/dist/rkcdp-rebuild /opt/rkcdp/
    rm -f /opt/rkcdp/*.py
else
    # source fallback: same names, no .py, executable via shebang
    install -m 755 "$HERE"/rkcdpd.py /opt/rkcdp/rkcdpd
    install -m 755 "$HERE"/rkcdp-rebuild.py /opt/rkcdp/rkcdp-rebuild
    install -m 644 "$HERE"/dmcdp.py /opt/rkcdp/
fi
install -m 755 "$HERE"/rkcdp-firstboot.sh /opt/rkcdp/
rm -f /etc/systemd/system/rkcdp-applier.service /opt/rkcdp/rkcdp-applier
install -m 644 "$HERE"/systemd/*.service /etc/systemd/system/
# cgroup read limit on the disk rkcdpd reads (base copy / recovery); enforced
# regardless of I/O scheduler, unlike ionice. Tune in this drop-in.
ROOTDEV=$(findmnt -n -o SOURCE / | sed -E 's|/dev/mapper/rkcdp[0-9]+|/dev/sda|')
[ -b "$ROOTDEV" ] || ROOTDEV=/dev/sda
mkdir -p /etc/systemd/system/rkcdpd.service.d
cat > /etc/systemd/system/rkcdpd.service.d/io.conf <<IOC
[Service]
IOReadBandwidthMax=$ROOTDEV 20M
IOC
install -m 755 "$HERE"/initramfs/hook /etc/initramfs-tools/hooks/rkcdp
install -m 755 "$HERE"/initramfs/local-top /etc/initramfs-tools/scripts/local-top/rkcdp
install -m 644 "$HERE"/udev/99-rkcdp.rules /etc/udev/rules.d/
echo "DM_NAME=rkcdp" > /etc/rkcdp/initramfs.conf
echo "options dm-cdp ring_mb=1024" > /etc/modprobe.d/dm-cdp.conf
echo "RESUME=none" > /etc/initramfs-tools/conf.d/resume
[ -f /etc/rkcdp/rkcdp.conf ] || cat > /etc/rkcdp/rkcdp.conf <<CONF
{ "dm_name": "rkcdp", "journal": "$JOURNAL", "cycle_sec": 1.0, "base_copy_mbps": 15 }
CONF
update-initramfs -u
systemctl daemon-reload
systemctl disable rkcdp-applier >/dev/null 2>&1 || true
systemctl enable rkcdpd rkcdp-firstboot >/dev/null
echo "rkcdp installed. Reboot; root will mount via /dev/mapper/rkcdp<N>. Disable with kernel arg kvmdr.cdp=0."
