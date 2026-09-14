# dm-cdp

Device-mapper target for in-guest continuous data protection. Kernel half of
`linux_rkcdp`. See `Documentation/admin-guide/device-mapper/dm-cdp.rst`.

```
make            # module + tools (needs linux-headers for the running kernel)
make load       # insmod
tests/smoke.sh  # loop device, write, drain, replay, compare (as root)
```

Layout: `drivers/md/dm-cdp.c` target, `include/uapi/linux/dm-cdp.h` ABI,
`tools/cdp-drain` reference consumer, `tools/cdp-apply` stream replay.
Ships as DKMS (`dkms.conf`).

Build-verified (W=1, no warnings): 6.1.0-49 (Debian 12), 6.8.0-146 (Ubuntu 24.04), 6.12.107 (Debian 13).
Runtime-verified: 6.1.0-49 (smoke, overflow, fio/PIT, real virtual disk).

## rkcdp (userspace, `rkcdp/`)

Bare-metal recovery for KVMDR managers built on dm-cdp:
`rkcdpd` (ship writes to NFS, base copy, bitmap fallback) →
`rkcdp-applier` (fold into `replica.raw` on the peer) →
`rkcdp-rebuild` (recreate the VM on Proxmox, boots STANDBY) +
`api_rkcdp.py` / `ui/` (Settings → Manager Recovery). See `rkcdp/docs/manager-recovery.md`.
