/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * dm-cdp: userspace ABI for the "cdp" device-mapper target.
 *
 * Every record read from /dev/cdpN starts with struct dm_cdp_rec and is
 * followed by rec_len - sizeof(struct dm_cdp_rec) bytes of payload (only
 * DM_CDP_REC_WRITE carries payload; the rest carry none). rec_len is a
 * multiple of 8. Records are delivered in submission order and only after
 * the underlying device has acknowledged the I/O.
 */
#ifndef _UAPI_LINUX_DM_CDP_H
#define _UAPI_LINUX_DM_CDP_H

#include <linux/types.h>
#include <linux/ioctl.h>

#define DM_CDP_ABI_VERSION	1
#define DM_CDP_REC_MAGIC	0x52504443	/* "CDPR" little-endian */

/* record types */
#define DM_CDP_REC_WRITE	1	/* payload = data written at sector */
#define DM_CDP_REC_FLUSH	2	/* barrier: everything before is durable */
#define DM_CDP_REC_DISCARD	3	/* len bytes at sector discarded */
#define DM_CDP_REC_ZERO		4	/* len bytes at sector zeroed */
#define DM_CDP_REC_PAD		5	/* ring wrap filler; never delivered */

/* record flags */
#define DM_CDP_F_FUA		(1u << 0)	/* write carried REQ_FUA */
#define DM_CDP_F_PREFLUSH	(1u << 1)	/* write carried REQ_PREFLUSH */
#define DM_CDP_F_ERROR		(1u << 2)	/* device returned an error */
#define DM_CDP_F_GAP_BEFORE	(1u << 3)	/* records were lost before this one
						 * (ring overflow); consult bitmap */

/* record state (kernel-internal, exposed for debugging only) */
#define DM_CDP_ST_RESERVED	0
#define DM_CDP_ST_DATA		1
#define DM_CDP_ST_COMMITTED	2

struct dm_cdp_rec {
	__u32 magic;		/* DM_CDP_REC_MAGIC */
	__u16 version;		/* DM_CDP_ABI_VERSION */
	__u16 type;		/* DM_CDP_REC_* */
	__u32 flags;		/* DM_CDP_F_* */
	__u32 state;		/* DM_CDP_ST_* */
	__u64 seq;		/* monotonic per target, no gaps unless F_GAP_BEFORE */
	__u64 sector;		/* target-relative, 512-byte units */
	__u32 len;		/* bytes affected (payload bytes for WRITE) */
	__u32 rec_len;		/* total record bytes incl. header, 8-aligned */
	__u64 ts_ns;		/* CLOCK_REALTIME at submission */
	__u64 reserved;
};

/* status flags */
#define DM_CDP_S_OVERFLOWED	(1u << 0)	/* overflow since last clear */
#define DM_CDP_S_DEAD		(1u << 1)	/* target removed, drain and close */

struct dm_cdp_status {
	__u32 abi_version;
	__u32 flags;
	__u64 dev_sectors;
	__u32 chunk_sectors;	/* bitmap granularity */
	__u32 minor;		/* /dev/cdp<minor> */
	__u64 bitmap_bits;
	__u64 bitmap_bytes;	/* bytes needed for DM_CDP_IOC_BITMAP */
	__u64 bitmap_set;	/* bits currently set */
	__u64 ring_size;
	__u64 ring_used;
	__u64 seq_next;		/* next seq the producer will assign */
	__u64 records;		/* committed since load/reset */
	__u64 bytes_logged;	/* payload bytes since load/reset */
	__u64 overflows;	/* dropped bios since load/reset */
	__u64 reserved[4];
};

#define DM_CDP_BM_CLEAR		(1u << 0)	/* read-and-clear atomically per word */

struct dm_cdp_bitmap {
	__u64 buf;		/* userspace pointer */
	__u64 len;		/* buffer length in bytes */
	__u32 flags;		/* DM_CDP_BM_* */
	__u32 reserved;
};

#define DM_CDP_IOC_MAGIC	0xCD
#define DM_CDP_IOC_STATUS	_IOR(DM_CDP_IOC_MAGIC, 1, struct dm_cdp_status)
#define DM_CDP_IOC_BITMAP	_IOWR(DM_CDP_IOC_MAGIC, 2, struct dm_cdp_bitmap)
#define DM_CDP_IOC_BITMAP_CLEAR	_IO(DM_CDP_IOC_MAGIC, 3)
#define DM_CDP_IOC_RESET_STATS	_IO(DM_CDP_IOC_MAGIC, 4)

#endif /* _UAPI_LINUX_DM_CDP_H */
