======
dm-cdp
======

dm-cdp is a device-mapper target that copies every write to a block
device into a kernel ring buffer readable from userspace, while the
original I/O proceeds unchanged. Reads pass straight through. It is
the kernel half of an in-guest continuous data protection stack: a
userspace daemon drains the ring into a journal from which any point
in time can be reconstructed.

Table
=====

::

    <start> <len> cdp <dev_path> <dev_offset>

- ``dev_path``: backing device (partition, LV, whole disk).
- ``dev_offset``: sector offset into the backing device.

Example — wrap ``/dev/sdb`` in place::

    dmsetup create data --table "0 $(blockdev --getsz /dev/sdb) cdp /dev/sdb 0"

Live attach to an existing LV without reboot::

    dmsetup suspend vg-root
    dmsetup reload vg-root --table "0 <sectors> cdp <pv> <offset>"
    dmsetup resume vg-root

Module parameters
=================

- ``ring_mb`` (default 256): ring size per target in MiB.
- ``chunk_kb`` (default 64): dirty-bitmap granularity in KiB.

Character device
================

Each target creates ``/dev/cdpN``. ``read(2)`` returns whole records
(``struct dm_cdp_rec`` + payload, see ``include/uapi/linux/dm-cdp.h``)
in submission order, each only after the backing device acknowledged
the I/O. A read never returns a partial record; buffers must hold at
least the largest bio (8 MiB is safe). ``poll(2)`` is supported.
``read`` returns 0 once the target has been removed and the ring is
empty.

Record types: WRITE (with payload), FLUSH (barrier), DISCARD, ZERO.
FLUSH records mark a point at which everything earlier is durable.

ioctls: ``DM_CDP_IOC_STATUS``, ``DM_CDP_IOC_BITMAP`` (copy, optionally
read-and-clear), ``DM_CDP_IOC_BITMAP_CLEAR``, ``DM_CDP_IOC_RESET_STATS``.

Overflow and the dirty bitmap
=============================

Every write also sets a bit in an in-kernel dirty bitmap before it is
copied into the ring. If the ring is full the payload is dropped, the
bio still proceeds, and the next record delivered carries
``DM_CDP_F_GAP_BEFORE``. The consumer then reads the bitmap
(read-and-clear), re-reads those extents from the device and journals
them. Protection degrades to changed-block tracking, never to a full
resync. The bitmap lives in kernel memory; persisting it across reboots
is the consumer's job (snapshot it at every checkpoint).

Status
======

``dmsetup status`` reports::

    cdp<N> ok|OVERFLOWED ring <used>/<size> records <n> bytes <n>
    overflows <n> seq <next> bitmap <set>/<bits> live|dead

Messages: ``clear_bitmap``, ``reset_stats``.
