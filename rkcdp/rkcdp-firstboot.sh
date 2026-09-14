#!/bin/bash
# rkcdp-firstboot: if this node was rebuilt by rkcdp-rebuild, it must come up
# as STANDBY. The marker is dropped into the replica copy before boot.
M=/etc/kvmdr/rkcdp-rebuilt-standby
[ -f "$M" ] || exit 0
echo "rkcdp-firstboot: rebuilt node, healing as standby (marker $(cat $M))"
rm -f "$M"
if [ -x /opt/kvmdr/kvmdr-heal-standby ]; then
    /opt/kvmdr/kvmdr-heal-standby || echo "rkcdp-firstboot: heal-standby rc=$?"
else
    echo "rkcdp-firstboot: kvmdr-heal-standby not found"
fi
