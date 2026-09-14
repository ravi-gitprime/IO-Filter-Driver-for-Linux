// SPDX-License-Identifier: GPL-2.0
/*
 * cdp-drain - reference consumer for dm-cdp
 *
 *   cdp-drain [-o stream.bin] [-q] [-n N] /dev/cdpN   drain records
 *   cdp-drain -S /dev/cdpN                            print status
 *   cdp-drain -B [-c] /dev/cdpN                       dump bitmap extents (-c: read-and-clear)
 *
 * Exit on SIGINT/SIGTERM or after N records (-n). Records are written to
 * the -o file verbatim (header + payload) so cdp-apply can replay them.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <linux/dm-cdp.h>

#define BUFSZ (16u << 20)

static volatile sig_atomic_t stop;
static void on_sig(int s) { (void)s; stop = 1; }

static const char *tname(unsigned t)
{
	switch (t) {
	case DM_CDP_REC_WRITE: return "WRITE";
	case DM_CDP_REC_FLUSH: return "FLUSH";
	case DM_CDP_REC_DISCARD: return "DISCARD";
	case DM_CDP_REC_ZERO: return "ZERO";
	case DM_CDP_REC_PAD: return "PAD";
	default: return "?";
	}
}

static int do_status(int fd)
{
	struct dm_cdp_status s;

	if (ioctl(fd, DM_CDP_IOC_STATUS, &s) < 0) {
		perror("DM_CDP_IOC_STATUS");
		return 1;
	}
	printf("abi %u minor %u flags %s%s\n", s.abi_version, s.minor,
	       (s.flags & DM_CDP_S_OVERFLOWED) ? "OVERFLOWED " : "",
	       (s.flags & DM_CDP_S_DEAD) ? "DEAD" : "");
	printf("dev_sectors %llu chunk_sectors %u\n",
	       (unsigned long long)s.dev_sectors, s.chunk_sectors);
	printf("ring %llu/%llu used\n", (unsigned long long)s.ring_used,
	       (unsigned long long)s.ring_size);
	printf("seq_next %llu records %llu bytes_logged %llu overflows %llu\n",
	       (unsigned long long)s.seq_next, (unsigned long long)s.records,
	       (unsigned long long)s.bytes_logged,
	       (unsigned long long)s.overflows);
	printf("bitmap %llu/%llu bits set (%llu bytes)\n",
	       (unsigned long long)s.bitmap_set,
	       (unsigned long long)s.bitmap_bits,
	       (unsigned long long)s.bitmap_bytes);
	return 0;
}

static int do_bitmap(int fd, int clear)
{
	struct dm_cdp_status s;
	struct dm_cdp_bitmap req;
	unsigned long *bm;
	uint64_t i, start = 0, set = 0;
	int in = 0;

	if (ioctl(fd, DM_CDP_IOC_STATUS, &s) < 0) {
		perror("DM_CDP_IOC_STATUS");
		return 1;
	}
	bm = calloc(1, s.bitmap_bytes);
	if (!bm)
		return 1;
	req.buf = (uint64_t)(uintptr_t)bm;
	req.len = s.bitmap_bytes;
	req.flags = clear ? DM_CDP_BM_CLEAR : 0;
	req.reserved = 0;
	if (ioctl(fd, DM_CDP_IOC_BITMAP, &req) < 0) {
		perror("DM_CDP_IOC_BITMAP");
		free(bm);
		return 1;
	}
	for (i = 0; i <= s.bitmap_bits; i++) {
		int bit = i < s.bitmap_bits &&
			  (bm[i / (8 * sizeof(long))] >> (i % (8 * sizeof(long)))) & 1;
		if (bit && !in) {
			start = i;
			in = 1;
		} else if (!bit && in) {
			printf("dirty sector %llu len %llu\n",
			       (unsigned long long)(start * s.chunk_sectors),
			       (unsigned long long)((i - start) * s.chunk_sectors));
			in = 0;
		}
		set += bit;
	}
	fprintf(stderr, "%llu chunks dirty%s\n", (unsigned long long)set,
		clear ? " (cleared)" : "");
	free(bm);
	return 0;
}

int main(int argc, char **argv)
{
	const char *out = NULL, *dev;
	int opt, quiet = 0, status = 0, bitmap = 0, clear = 0;
	long limit = -1, count = 0;
	int fd, ofd = -1;
	char *buf;
	uint64_t total = 0;

	while ((opt = getopt(argc, argv, "o:qn:SBc")) != -1) {
		switch (opt) {
		case 'o': out = optarg; break;
		case 'q': quiet = 1; break;
		case 'n': limit = atol(optarg); break;
		case 'S': status = 1; break;
		case 'B': bitmap = 1; break;
		case 'c': clear = 1; break;
		default:
			fprintf(stderr, "usage: %s [-o file] [-q] [-n N] [-S] [-B [-c]] /dev/cdpN\n", argv[0]);
			return 2;
		}
	}
	if (optind >= argc) {
		fprintf(stderr, "missing device\n");
		return 2;
	}
	dev = argv[optind];

	fd = open(dev, O_RDONLY);
	if (fd < 0) {
		perror(dev);
		return 1;
	}
	if (status)
		return do_status(fd);
	if (bitmap)
		return do_bitmap(fd, clear);

	if (out) {
		ofd = open(out, O_WRONLY | O_CREAT | O_APPEND, 0600);
		if (ofd < 0) {
			perror(out);
			return 1;
		}
	}

	size_t bufsz = BUFSZ;

	buf = malloc(bufsz);
	if (!buf)
		return 1;

	setvbuf(stdout, NULL, _IOLBF, 0);
	{
		struct sigaction sa = { .sa_handler = on_sig };

		/* no SA_RESTART: a blocked read() must return EINTR */
		sigaction(SIGINT, &sa, NULL);
		sigaction(SIGTERM, &sa, NULL);
	}

	while (!stop && (limit < 0 || count < limit)) {
		ssize_t n = read(fd, buf, bufsz);
		size_t off = 0;

		if (n < 0) {
			if (errno == EINTR) {
				if (stop)
					break;
				continue;
			}
			if (errno == EMSGSIZE) {
				char *nb = realloc(buf, bufsz * 2);

				if (!nb) {
					perror("realloc");
					break;
				}
				buf = nb;
				bufsz *= 2;
				fprintf(stderr, "buffer grown to %zu MiB\n", bufsz >> 20);
				continue;
			}
			perror("read");
			break;
		}
		if (n == 0)
			break;	/* target removed */

		if (ofd >= 0 && write(ofd, buf, n) != n) {
			perror("write");
			break;
		}

		while (off + sizeof(struct dm_cdp_rec) <= (size_t)n) {
			struct dm_cdp_rec *r = (void *)(buf + off);

			if (r->magic != DM_CDP_REC_MAGIC) {
				fprintf(stderr, "bad magic at %zu\n", off);
				stop = 1;
				break;
			}
			if (!quiet)
				printf("%llu %s sector %llu len %u%s%s%s%s\n",
				       (unsigned long long)r->seq, tname(r->type),
				       (unsigned long long)r->sector, r->len,
				       (r->flags & DM_CDP_F_FUA) ? " FUA" : "",
				       (r->flags & DM_CDP_F_PREFLUSH) ? " PREFLUSH" : "",
				       (r->flags & DM_CDP_F_ERROR) ? " ERROR" : "",
				       (r->flags & DM_CDP_F_GAP_BEFORE) ? " GAP_BEFORE" : "");
			if (r->type == DM_CDP_REC_WRITE)
				total += r->len;
			count++;
			off += r->rec_len;
			if (limit >= 0 && count >= limit)
				break;
		}
	}

	fprintf(stderr, "%ld records, %llu payload bytes\n", count,
		(unsigned long long)total);
	free(buf);
	if (ofd >= 0)
		close(ofd);
	close(fd);
	return 0;
}
