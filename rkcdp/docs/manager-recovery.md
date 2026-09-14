# Manager recovery (rkcdp)

Each KVMDR manager protects its own disk: `dm-cdp` captures every write,
`rkcdpd` ships them to `/replication/_kvmdr/rkcdp/<node>/`, and the peer's
`rkcdp-applier` folds them into `replica.raw` — an always-current, bootable
whole-disk image. There is no point-in-time history: the replica is the
node as of a few seconds ago.

## One manager down (normal case) — UI

Settings → Manager Recovery → row for the dead node shows **Stale** → Rebuild
→ pick Proxmox host (VMID/storage/bridge optional) → **Rebuild as STANDBY**.

Steps shown live: check → quiesce → copy → mark standby → create VM → boot.
The node boots with `/etc/kvmdr/rkcdp-rebuilt-standby` present;
`rkcdp-firstboot` runs `kvmdr-heal-standby` before the API starts, so it
rejoins as STANDBY of the current ACTIVE. Failback later if roles should swap.

Stop or remove the old VM first: the rebuilt one uses the same MAC and IP.

## Both managers down — command line

Replicas are on the replication share, not on the managers. From a Proxmox
host that has the share mounted (or any Debian box with NFS + ssh to the host):

    python3 rkcdp-rebuild.py --node km-src1 --host 192.168.1.180 --no-standby
    # -> creates VM, imports replica, boots. Then on the node: kvmdr-promote
    python3 rkcdp-rebuild.py --node km-tgt1 --host 192.168.1.190
    # -> second node comes up STANDBY and heals from the first

Options: `--vmid`, `--storage local-lvm`, `--bridge vmbr0`,
`--host-journal <path as the host sees the share>`, `--force` (node still
reporting alive), `--keep-copy`.

## Files

    /replication/_kvmdr/rkcdp/<node>/
      manifest.json   device, base_end_seq, gen
      node.json       cpus, mem, MACs, IPs, firmware   (hourly)
      status.json     CDP/BITMAP, seq, ring, overflows (every second)
      applied.json    last seq folded into the replica
      replica.raw     sparse whole-disk image
      cycles/         shipped, not-yet-applied writes (normally empty)
    /replication/_kvmdr/rkcdp/rebuild/   temporary copies during a rebuild

## States

- **CDP** — shipping normally.
- **Bitmap** — ring overflowed; rkcdpd re-reads dirty blocks (CBT), never a full resync.
- **Stale** — no heartbeat for 60 s; probably dead. Rebuild is offered.
- **Unprotected** — base copy never completed.
