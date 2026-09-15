// SPDX-License-Identifier: GPL-2.0
/*
 * dm-cdp - continuous data protection device-mapper target
 *
 * Copyright (C) 2026 KVMDR AI Limited
 *
 * Sits in front of a block device and copies every write into a kernel
 * ring buffer that userspace drains through /dev/cdpN. Reads pass
 * through untouched. A persistent-style dirty bitmap is always
 * maintained so that if the ring overflows (consumer too slow or
 * absent) the consumer can fall back to changed-block tracking instead
 * of a full resync.
 *
 * Table line:
 *	<start> <len> cdp <dev_path> <dev_offset_sectors>
 *
 * Records are handed to userspace in submission order and only once the
 * backing device has acknowledged the I/O, so the stream is always
 * crash-consistent with what is on disk.
 */

#include <linux/module.h>
#include <linux/init.h>
#include <linux/version.h>
#include <linux/device-mapper.h>
#include <linux/bio.h>
#include <linux/bvec.h>
#include <linux/blkdev.h>
#include <linux/vmalloc.h>
#include <linux/slab.h>
#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/poll.h>
#include <linux/uaccess.h>
#include <linux/idr.h>
#include <linux/kref.h>
#include <linux/wait.h>
#include <linux/spinlock.h>
#include <linux/mutex.h>
#include <linux/bitmap.h>
#include <linux/ktime.h>
#include <uapi/linux/dm-cdp.h>

#define DM_MSG_PREFIX		"cdp"
#define CDP_TARGET_NAME		"cdp"
#define CDP_MAX_MINORS		64
#define CDP_REC_ALIGN		8
#define CDP_MAX_REC_DIV		4	/* one record may not exceed ring/4 */
#define CDP_MAX_IO_SECTORS	8192	/* split bios at 4 MiB: bounds record size */

static unsigned int ring_mb = 256;
module_param(ring_mb, uint, 0444);
MODULE_PARM_DESC(ring_mb, "Ring buffer size per target in MiB (default 256)");

static unsigned int chunk_kb = 64;
module_param(chunk_kb, uint, 0444);
MODULE_PARM_DESC(chunk_kb, "Dirty bitmap granularity in KiB (default 64)");

static dev_t cdp_devt;
static struct class *cdp_class;
static DEFINE_IDA(cdp_ida);

struct cdp_target {
	struct dm_target *ti;
	struct dm_dev *dev;
	sector_t start;

	/* ring buffer */
	void *ring;
	u64 ring_size;
	spinlock_t lock;		/* producer side: head, seq, stats */
	u64 head;			/* monotonic producer offset */
	u64 tail;			/* monotonic consumer offset */
	u64 seq_next;
	u64 overflows;
	atomic64_t records;		/* bumped from end_io (any context) */
	atomic64_t bytes_logged;
	bool gap_pending;
	bool overflowed;

	/* dirty bitmap */
	unsigned long *bitmap;
	u64 nbits;
	unsigned int chunk_sectors;

	/* character device */
	int minor;
	struct cdev cdev;
	struct device *device;
	wait_queue_head_t wq;
	struct mutex read_lock;
	struct kref kref;
	bool dead;
};

struct cdp_pb {
	u64 rec_off;
	bool logged;
};

/* ------------------------------------------------------------------ */
/* bitmap                                                             */

static void cdp_mark(struct cdp_target *t, sector_t sector, unsigned int bytes)
{
	u64 first, last, b;

	if (!bytes)
		return;
	first = sector / t->chunk_sectors;
	last = (sector + ((bytes + 511) >> 9) - 1) / t->chunk_sectors;
	if (last >= t->nbits)
		last = t->nbits - 1;
	for (b = first; b <= last; b++)
		set_bit(b, t->bitmap);
}

/* ------------------------------------------------------------------ */
/* ring producer                                                      */

static inline struct dm_cdp_rec *cdp_rec_at(struct cdp_target *t, u64 off)
{
	return t->ring + (off % t->ring_size);
}

/*
 * Reserve a record of @payload bytes. Returns NULL (and accounts an
 * overflow) if the ring is full. On success the header is fully
 * initialised in state RESERVED and t->head has been published.
 */
static struct dm_cdp_rec *cdp_reserve(struct cdp_target *t, u16 type,
				      u32 flags, sector_t sector, u32 len,
				      u32 payload, u64 *off_out)
{
	struct dm_cdp_rec *rec;
	u64 need = ALIGN(sizeof(*rec) + payload, CDP_REC_ALIGN);
	u64 hpos, pad = 0, free;

	if (need > t->ring_size / CDP_MAX_REC_DIV)
		goto overflow_nolock;

	spin_lock(&t->lock);
	hpos = t->head % t->ring_size;
	if (hpos + need > t->ring_size)
		pad = t->ring_size - hpos;
	free = t->ring_size - (t->head - READ_ONCE(t->tail));
	if (need + pad > free) {
		t->overflows++;
		t->overflowed = true;
		t->gap_pending = true;
		spin_unlock(&t->lock);
		return NULL;
	}

	if (pad) {
		if (pad >= sizeof(*rec)) {
			struct dm_cdp_rec *p = t->ring + hpos;

			memset(p, 0, sizeof(*p));
			p->magic = DM_CDP_REC_MAGIC;
			p->version = DM_CDP_ABI_VERSION;
			p->type = DM_CDP_REC_PAD;
			p->rec_len = pad;
			p->state = DM_CDP_ST_COMMITTED;
		}
		/* pad < header: consumer skips to boundary by itself */
		t->head += pad;
		hpos = 0;
	}

	rec = t->ring + hpos;
	rec->magic = DM_CDP_REC_MAGIC;
	rec->version = DM_CDP_ABI_VERSION;
	rec->type = type;
	rec->flags = flags;
	if (t->gap_pending) {
		rec->flags |= DM_CDP_F_GAP_BEFORE;
		t->gap_pending = false;
	}
	rec->state = DM_CDP_ST_RESERVED;
	rec->seq = t->seq_next++;
	rec->sector = sector;
	rec->len = len;
	rec->rec_len = need;
	rec->ts_ns = ktime_get_real_ns();
	rec->reserved = 0;

	*off_out = t->head;
	/* header must be visible before consumers see the new head */
	smp_store_release(&t->head, t->head + need);
	spin_unlock(&t->lock);
	return rec;

overflow_nolock:
	spin_lock(&t->lock);
	t->overflows++;
	t->overflowed = true;
	t->gap_pending = true;
	spin_unlock(&t->lock);
	return NULL;
}

static void cdp_copy_bio_data(struct dm_cdp_rec *rec, struct bio *bio)
{
	struct bio_vec bv;
	struct bvec_iter iter;
	char *dst = (char *)(rec + 1);

	bio_for_each_segment(bv, bio, iter) {
		memcpy_from_bvec(dst, &bv);
		dst += bv.bv_len;
	}
}

static void cdp_log(struct cdp_target *t, struct bio *bio, struct cdp_pb *pb,
		    u16 type, u32 flags, sector_t rel, u32 len, bool copy)
{
	struct dm_cdp_rec *rec;
	u64 off;

	cdp_mark(t, rel, len);

	rec = cdp_reserve(t, type, flags, rel, len, copy ? len : 0, &off);
	if (!rec)
		return;

	if (copy)
		cdp_copy_bio_data(rec, bio);

	smp_store_release(&rec->state, DM_CDP_ST_DATA);
	pb->rec_off = off;
	pb->logged = true;
}

/* ------------------------------------------------------------------ */
/* ring consumer                                                      */

/*
 * Find the next deliverable record at or after @pos. Skips pad records
 * and end-of-ring slack. Returns NULL if the consumer has caught up.
 * *pos is advanced past anything skipped.
 */
static struct dm_cdp_rec *cdp_next(struct cdp_target *t, u64 *pos)
{
	for (;;) {
		u64 head = smp_load_acquire(&t->head);
		u64 p = *pos, ppos;
		struct dm_cdp_rec *rec;

		if (p >= head)
			return NULL;
		ppos = p % t->ring_size;
		if (t->ring_size - ppos < sizeof(*rec)) {
			*pos = p + (t->ring_size - ppos);
			continue;
		}
		rec = t->ring + ppos;
		if (rec->type == DM_CDP_REC_PAD) {
			*pos = p + rec->rec_len;
			continue;
		}
		return rec;
	}
}

static bool cdp_readable(struct cdp_target *t)
{
	u64 pos = READ_ONCE(t->tail);
	struct dm_cdp_rec *rec = cdp_next(t, &pos);

	return rec && smp_load_acquire(&rec->state) == DM_CDP_ST_COMMITTED;
}

static ssize_t cdp_read(struct file *f, char __user *buf, size_t count,
			loff_t *ppos)
{
	struct cdp_target *t = f->private_data;
	size_t copied = 0;
	int ret = 0;

	if (mutex_lock_interruptible(&t->read_lock))
		return -ERESTARTSYS;

	for (;;) {
		struct dm_cdp_rec *rec;
		u64 pos = t->tail;

		rec = cdp_next(t, &pos);
		if (!rec || smp_load_acquire(&rec->state) != DM_CDP_ST_COMMITTED) {
			/* nothing deliverable */
			if (copied)
				break;
			if (READ_ONCE(t->dead)) {
				ret = 0;	/* EOF */
				break;
			}
			if (f->f_flags & O_NONBLOCK) {
				ret = -EAGAIN;
				break;
			}
			mutex_unlock(&t->read_lock);
			ret = wait_event_interruptible(t->wq, cdp_readable(t) ||
						       READ_ONCE(t->dead));
			if (ret)
				return ret;
			if (mutex_lock_interruptible(&t->read_lock))
				return -ERESTARTSYS;
			continue;
		}

		if (rec->rec_len > count - copied) {
			if (!copied)
				ret = -EMSGSIZE;
			break;
		}
		if (copy_to_user(buf + copied, rec, rec->rec_len)) {
			if (!copied)
				ret = -EFAULT;
			break;
		}
		copied += rec->rec_len;
		WRITE_ONCE(t->tail, pos + rec->rec_len);
	}

	mutex_unlock(&t->read_lock);
	return copied ? copied : ret;
}

static __poll_t cdp_poll(struct file *f, poll_table *wait)
{
	struct cdp_target *t = f->private_data;
	__poll_t mask = 0;

	poll_wait(f, &t->wq, wait);
	if (cdp_readable(t))
		mask |= EPOLLIN | EPOLLRDNORM;
	if (READ_ONCE(t->dead))
		mask |= EPOLLHUP;
	return mask;
}

static u64 cdp_bitmap_bytes(struct cdp_target *t)
{
	return BITS_TO_LONGS(t->nbits) * sizeof(unsigned long);
}

static long cdp_ioctl_status(struct cdp_target *t, void __user *arg)
{
	struct dm_cdp_status s;

	memset(&s, 0, sizeof(s));
	s.abi_version = DM_CDP_ABI_VERSION;
	s.dev_sectors = t->ti->len;
	s.chunk_sectors = t->chunk_sectors;
	s.minor = t->minor;
	s.bitmap_bits = t->nbits;
	s.bitmap_bytes = cdp_bitmap_bytes(t);
	s.bitmap_set = bitmap_weight(t->bitmap, t->nbits);
	s.ring_size = t->ring_size;

	spin_lock(&t->lock);
	s.ring_used = t->head - READ_ONCE(t->tail);
	s.seq_next = t->seq_next;
	s.records = atomic64_read(&t->records);
	s.bytes_logged = atomic64_read(&t->bytes_logged);
	s.overflows = t->overflows;
	if (t->overflowed)
		s.flags |= DM_CDP_S_OVERFLOWED;
	spin_unlock(&t->lock);
	if (READ_ONCE(t->dead))
		s.flags |= DM_CDP_S_DEAD;

	return copy_to_user(arg, &s, sizeof(s)) ? -EFAULT : 0;
}

static long cdp_ioctl_bitmap(struct cdp_target *t, void __user *arg)
{
	struct dm_cdp_bitmap req;
	unsigned long *tmp;
	u64 bytes = cdp_bitmap_bytes(t);
	unsigned long nlongs = BITS_TO_LONGS(t->nbits), i;
	long ret = 0;

	if (copy_from_user(&req, arg, sizeof(req)))
		return -EFAULT;
	if (req.len < bytes)
		return -ENOSPC;

	tmp = vmalloc(bytes);
	if (!tmp)
		return -ENOMEM;

	if (req.flags & DM_CDP_BM_CLEAR) {
		for (i = 0; i < nlongs; i++)
			tmp[i] = xchg(&t->bitmap[i], 0UL);
		spin_lock(&t->lock);
		t->overflowed = false;
		spin_unlock(&t->lock);
	} else {
		for (i = 0; i < nlongs; i++)
			tmp[i] = READ_ONCE(t->bitmap[i]);
	}

	if (copy_to_user(u64_to_user_ptr(req.buf), tmp, bytes))
		ret = -EFAULT;
	vfree(tmp);
	return ret;
}

static long cdp_ioctl(struct file *f, unsigned int cmd, unsigned long arg)
{
	struct cdp_target *t = f->private_data;
	void __user *uarg = (void __user *)arg;

	switch (cmd) {
	case DM_CDP_IOC_STATUS:
		return cdp_ioctl_status(t, uarg);
	case DM_CDP_IOC_BITMAP:
		return cdp_ioctl_bitmap(t, uarg);
	case DM_CDP_IOC_BITMAP_CLEAR:
		bitmap_zero(t->bitmap, t->nbits);
		spin_lock(&t->lock);
		t->overflowed = false;
		spin_unlock(&t->lock);
		return 0;
	case DM_CDP_IOC_RESET_STATS:
		atomic64_set(&t->records, 0);
		atomic64_set(&t->bytes_logged, 0);
		spin_lock(&t->lock);
		t->overflows = 0;
		spin_unlock(&t->lock);
		return 0;
	default:
		return -ENOTTY;
	}
}

static void cdp_free(struct kref *kref)
{
	struct cdp_target *t = container_of(kref, struct cdp_target, kref);

	vfree(t->ring);
	bitmap_free(t->bitmap);
	ida_free(&cdp_ida, t->minor);
	kfree(t);
}

static int cdp_open(struct inode *inode, struct file *f)
{
	struct cdp_target *t = container_of(inode->i_cdev, struct cdp_target, cdev);

	if (READ_ONCE(t->dead))
		return -ENODEV;
	kref_get(&t->kref);
	f->private_data = t;
	return stream_open(inode, f);
}

static int cdp_release(struct inode *inode, struct file *f)
{
	struct cdp_target *t = f->private_data;

	kref_put(&t->kref, cdp_free);
	return 0;
}

static const struct file_operations cdp_fops = {
	.owner		= THIS_MODULE,
	.open		= cdp_open,
	.release	= cdp_release,
	.read		= cdp_read,
	.poll		= cdp_poll,
	.unlocked_ioctl	= cdp_ioctl,
	.compat_ioctl	= compat_ptr_ioctl,
};

/* ------------------------------------------------------------------ */
/* device-mapper target                                               */

static int cdp_ctr(struct dm_target *ti, unsigned int argc, char **argv)
{
	struct cdp_target *t;
	unsigned long long offset;
	char dummy;
	int ret;

	if (argc != 2) {
		ti->error = "Invalid argument count: <dev_path> <offset>";
		return -EINVAL;
	}
	if (sscanf(argv[1], "%llu%c", &offset, &dummy) != 1 ||
	    offset != (sector_t)offset) {
		ti->error = "Invalid device offset";
		return -EINVAL;
	}
	if (!ring_mb || !chunk_kb || (chunk_kb & (chunk_kb - 1))) {
		ti->error = "Invalid module parameters";
		return -EINVAL;
	}

	t = kzalloc(sizeof(*t), GFP_KERNEL);
	if (!t) {
		ti->error = "Cannot allocate context";
		return -ENOMEM;
	}
	t->ti = ti;
	t->start = offset;
	spin_lock_init(&t->lock);
	mutex_init(&t->read_lock);
	init_waitqueue_head(&t->wq);
	kref_init(&t->kref);
	t->minor = -1;

	ret = dm_get_device(ti, argv[0], dm_table_get_mode(ti->table), &t->dev);
	if (ret) {
		ti->error = "Device lookup failed";
		pr_err("dm-cdp [FAILED] Device lookup failed\\n");
		goto err_free;
	}

	t->ring_size = (u64)ring_mb << 20;
	t->ring = vmalloc(t->ring_size);
	if (!t->ring) {
		ti->error = "Cannot allocate ring";
		pr_err("dm-cdp [FAILED] Cannot allocate ring\\n");
		ret = -ENOMEM;
		goto err_dev;
	}

	t->chunk_sectors = chunk_kb * 2;
	t->nbits = DIV_ROUND_UP((u64)ti->len, t->chunk_sectors);
	t->bitmap = bitmap_zalloc(t->nbits, GFP_KERNEL);
	if (!t->bitmap) {
		ti->error = "Cannot allocate bitmap";
		pr_err("dm-cdp [FAILED] Cannot allocate bitmap\\n");
		ret = -ENOMEM;
		goto err_ring;
	}

	t->minor = ida_alloc_max(&cdp_ida, CDP_MAX_MINORS - 1, GFP_KERNEL);
	if (t->minor < 0) {
		ti->error = "No free minor";
		pr_err("dm-cdp [FAILED] No free minor\\n");
		ret = t->minor;
		goto err_bitmap;
	}

	cdev_init(&t->cdev, &cdp_fops);
	t->cdev.owner = THIS_MODULE;
	ret = cdev_add(&t->cdev, MKDEV(MAJOR(cdp_devt), t->minor), 1);
	if (ret) {
		ti->error = "cdev_add failed";
		pr_err("dm-cdp [FAILED] cdev_add failed\\n");
		goto err_ida;
	}

	t->device = device_create(cdp_class, NULL,
				  MKDEV(MAJOR(cdp_devt), t->minor), t,
				  "cdp%d", t->minor);
	if (IS_ERR(t->device)) {
		ti->error = "device_create failed";
		pr_err("dm-cdp [FAILED] device_create failed\\n");
		ret = PTR_ERR(t->device);
		goto err_cdev;
	}

	ret = dm_set_target_max_io_len(ti, CDP_MAX_IO_SECTORS);
	if (ret) {
		ti->error = "dm_set_target_max_io_len failed";
		pr_err("dm-cdp [FAILED] dm_set_target_max_io_len failed\\n");
		goto err_device;
	}

	ti->num_flush_bios = 1;
	ti->num_discard_bios = 1;
	ti->num_secure_erase_bios = 1;
	ti->num_write_zeroes_bios = 1;
	ti->flush_supported = true;
	ti->per_io_data_size = sizeof(struct cdp_pb);
	ti->private = t;

	pr_info("dm-cdp attached to %s (%llu sectors) as /dev/cdp%d, ring %u MiB [SUCCESS]\n",
		t->dev->name, (unsigned long long)ti->len, t->minor, ring_mb);
	return 0;

err_device:
	device_destroy(cdp_class, MKDEV(MAJOR(cdp_devt), t->minor));
err_cdev:
	cdev_del(&t->cdev);
err_ida:
	ida_free(&cdp_ida, t->minor);
	t->minor = -1;
err_bitmap:
	bitmap_free(t->bitmap);
	t->bitmap = NULL;
err_ring:
	vfree(t->ring);
	t->ring = NULL;
err_dev:
	dm_put_device(ti, t->dev);
err_free:
	kfree(t);
	return ret;
}

static void cdp_dtr(struct dm_target *ti)
{
	struct cdp_target *t = ti->private;

	WRITE_ONCE(t->dead, true);
	wake_up_all(&t->wq);
	device_destroy(cdp_class, MKDEV(MAJOR(cdp_devt), t->minor));
	cdev_del(&t->cdev);
	dm_put_device(ti, t->dev);
	/* ring/bitmap live on until the last reader closes */
	kref_put(&t->kref, cdp_free);
}

static int cdp_map(struct dm_target *ti, struct bio *bio)
{
	struct cdp_target *t = ti->private;
	struct cdp_pb *pb = dm_per_bio_data(bio, sizeof(*pb));
	sector_t rel = dm_target_offset(ti, bio->bi_iter.bi_sector);
	unsigned int bytes = bio->bi_iter.bi_size;
	u32 flags = 0;

	pb->logged = false;

	bio_set_dev(bio, t->dev->bdev);
	bio->bi_iter.bi_sector = t->start + rel;

	if (bio->bi_opf & REQ_FUA)
		flags |= DM_CDP_F_FUA;
	if (bio->bi_opf & REQ_PREFLUSH)
		flags |= DM_CDP_F_PREFLUSH;

	switch (bio_op(bio)) {
	case REQ_OP_WRITE:
		if (!bytes) {
			/* empty flush bio */
			if (bio->bi_opf & REQ_PREFLUSH)
				cdp_log(t, bio, pb, DM_CDP_REC_FLUSH, flags,
					0, 0, false);
			break;
		}
		cdp_log(t, bio, pb, DM_CDP_REC_WRITE, flags, rel, bytes, true);
		break;
	case REQ_OP_DISCARD:
	case REQ_OP_SECURE_ERASE:
		cdp_log(t, bio, pb, DM_CDP_REC_DISCARD, flags, rel, bytes, false);
		break;
	case REQ_OP_WRITE_ZEROES:
		cdp_log(t, bio, pb, DM_CDP_REC_ZERO, flags, rel, bytes, false);
		break;
	default:
		/* reads and everything else pass straight through */
		break;
	}

	return DM_MAPIO_REMAPPED;
}

static int cdp_end_io(struct dm_target *ti, struct bio *bio, blk_status_t *error)
{
	struct cdp_target *t = ti->private;
	struct cdp_pb *pb = dm_per_bio_data(bio, sizeof(*pb));
	struct dm_cdp_rec *rec;

	if (!pb->logged)
		return DM_ENDIO_DONE;

	rec = cdp_rec_at(t, pb->rec_off);
	if (*error)
		rec->flags |= DM_CDP_F_ERROR;

	atomic64_inc(&t->records);
	if (rec->type == DM_CDP_REC_WRITE)
		atomic64_add(rec->len, &t->bytes_logged);

	/* data and flags must be visible before the state flips */
	smp_store_release(&rec->state, DM_CDP_ST_COMMITTED);
	wake_up_interruptible(&t->wq);
	return DM_ENDIO_DONE;
}

static void cdp_status(struct dm_target *ti, status_type_t type,
		       unsigned int status_flags, char *result,
		       unsigned int maxlen)
{
	struct cdp_target *t = ti->private;
	unsigned int sz = 0;
	u64 used, records, bytes, overflows, seq;
	bool ovf;

	switch (type) {
	case STATUSTYPE_INFO:
		spin_lock(&t->lock);
		used = t->head - READ_ONCE(t->tail);
		overflows = t->overflows;
		seq = t->seq_next;
		ovf = t->overflowed;
		spin_unlock(&t->lock);
		records = atomic64_read(&t->records);
		bytes = atomic64_read(&t->bytes_logged);
		DMEMIT("cdp%d %s ring %llu/%llu records %llu bytes %llu overflows %llu seq %llu bitmap %llu/%llu %s",
		       t->minor, ovf ? "OVERFLOWED" : "ok",
		       used, t->ring_size, records, bytes, overflows, seq,
		       (u64)bitmap_weight(t->bitmap, t->nbits), t->nbits,
		       READ_ONCE(t->dead) ? "dead" : "live");
		break;
	case STATUSTYPE_TABLE:
		DMEMIT("%s %llu", t->dev->name, (unsigned long long)t->start);
		break;
	case STATUSTYPE_IMA:
		*result = '\0';
		break;
	}
}

static int cdp_message(struct dm_target *ti, unsigned int argc, char **argv,
		       char *result, unsigned int maxlen)
{
	struct cdp_target *t = ti->private;

	if (argc != 1)
		return -EINVAL;

	if (!strcasecmp(argv[0], "clear_bitmap")) {
		bitmap_zero(t->bitmap, t->nbits);
		spin_lock(&t->lock);
		t->overflowed = false;
		spin_unlock(&t->lock);
		return 0;
	}
	if (!strcasecmp(argv[0], "reset_stats")) {
		atomic64_set(&t->records, 0);
		atomic64_set(&t->bytes_logged, 0);
		spin_lock(&t->lock);
		t->overflows = 0;
		spin_unlock(&t->lock);
		return 0;
	}
	return -EINVAL;
}

static int cdp_iterate_devices(struct dm_target *ti,
			       iterate_devices_callout_fn fn, void *data)
{
	struct cdp_target *t = ti->private;

	return fn(ti, t->dev, t->start, ti->len, data);
}

static struct target_type cdp_target_type = {
	.name		= CDP_TARGET_NAME,
	.version	= {0, 1, 0},
	.module		= THIS_MODULE,
	.ctr		= cdp_ctr,
	.dtr		= cdp_dtr,
	.map		= cdp_map,
	.end_io		= cdp_end_io,
	.status		= cdp_status,
	.message	= cdp_message,
	.iterate_devices = cdp_iterate_devices,
};

/* ------------------------------------------------------------------ */
/* module                                                             */

static int __init dm_cdp_init(void)
{
	int ret;

	ret = alloc_chrdev_region(&cdp_devt, 0, CDP_MAX_MINORS, "dm-cdp");
	if (ret)
		return ret;

#if LINUX_VERSION_CODE >= KERNEL_VERSION(6, 4, 0)
	cdp_class = class_create("dm-cdp");
#else
	cdp_class = class_create(THIS_MODULE, "dm-cdp");
#endif
	if (IS_ERR(cdp_class)) {
		ret = PTR_ERR(cdp_class);
		goto err_chrdev;
	}

	ret = dm_register_target(&cdp_target_type);
	if (ret) {
		pr_err("dm-cdp [FAILED] target registration: %d\n", ret);
		goto err_class;
	}
	pr_info("dm-cdp %d.%d.%d loaded [SUCCESS]\n", cdp_target_type.version[0],
		cdp_target_type.version[1], cdp_target_type.version[2]);
	return 0;

err_class:
	class_destroy(cdp_class);
err_chrdev:
	unregister_chrdev_region(cdp_devt, CDP_MAX_MINORS);
	return ret;
}

static void __exit dm_cdp_exit(void)
{
	dm_unregister_target(&cdp_target_type);
	class_destroy(cdp_class);
	unregister_chrdev_region(cdp_devt, CDP_MAX_MINORS);
	ida_destroy(&cdp_ida);
}

module_init(dm_cdp_init);
module_exit(dm_cdp_exit);

MODULE_DESCRIPTION(DM_NAME " continuous data protection target");
MODULE_AUTHOR("KVMDR AI Limited");
MODULE_LICENSE("GPL");
