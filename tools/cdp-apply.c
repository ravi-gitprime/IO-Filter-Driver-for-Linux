// SPDX-License-Identifier: GPL-2.0
/*
 * cdp-apply - replay a cdp-drain stream onto an image or block device
 *
 *   cdp-apply [-f FROM_SEQ] [-s STOP_SEQ] stream.bin target
 *
 * Applies WRITE/ZERO/DISCARD records in order (DISCARD is treated as
 * zero). Stops after STOP_SEQ if given. Exits 3 on a GAP_BEFORE record
 * (printing its seq) so the caller can re-read the dirty bitmap extents
 * from the device and then resume with -f <that seq>. Exits 1 on an
 * ERROR record or a broken stream.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <linux/dm-cdp.h>

static int write_all(int fd, const void *p, size_t n, off_t off)
{
	const char *c = p;

	while (n) {
		ssize_t w = pwrite(fd, c, n, off);

		if (w < 0) {
			if (errno == EINTR)
				continue;
			return -1;
		}
		c += w;
		n -= w;
		off += w;
	}
	return 0;
}

static int read_all(FILE *f, void *p, size_t n)
{
	return fread(p, 1, n, f) == n ? 0 : -1;
}

int main(int argc, char **argv)
{
	int opt, tfd, rc = 0;
	long long stop_seq = -1, from_seq = -1;
	FILE *in;
	char *zero = NULL;
	size_t zero_len = 0;
	uint64_t applied = 0, bytes = 0, expect_seq = 0;
	int have_seq = 0;

	while ((opt = getopt(argc, argv, "f:s:")) != -1) {
		if (opt == 's')
			stop_seq = atoll(optarg);
		else if (opt == 'f')
			from_seq = atoll(optarg);
		else
			return 2;
	}
	if (argc - optind != 2) {
		fprintf(stderr, "usage: %s [-f FROM_SEQ] [-s STOP_SEQ] stream.bin target\n", argv[0]);
		return 2;
	}

	in = fopen(argv[optind], "rb");
	if (!in) {
		perror(argv[optind]);
		return 1;
	}
	tfd = open(argv[optind + 1], O_WRONLY);
	if (tfd < 0) {
		perror(argv[optind + 1]);
		return 1;
	}

	for (;;) {
		struct dm_cdp_rec r;
		size_t payload;
		char *buf;

		if (read_all(in, &r, sizeof(r)))
			break;
		if (r.magic != DM_CDP_REC_MAGIC) {
			fprintf(stderr, "bad magic\n");
			rc = 1;
			break;
		}
		payload = r.rec_len - sizeof(r);
		if ((long long)r.seq < from_seq) {
			/* skip records before the resume point */
			if (payload && fseek(in, payload, SEEK_CUR)) {
				rc = 1;
				break;
			}
			continue;
		}
		if (have_seq && r.seq != expect_seq) {
			fprintf(stderr, "seq gap: expected %llu got %llu\n",
				(unsigned long long)expect_seq,
				(unsigned long long)r.seq);
			rc = 1;
			break;
		}
		if ((r.flags & DM_CDP_F_GAP_BEFORE) && (long long)r.seq != from_seq) {
			fprintf(stderr, "GAP_BEFORE at seq %llu\n",
				(unsigned long long)r.seq);
			rc = 3;
			break;
		}
		if (r.flags & DM_CDP_F_ERROR) {
			fprintf(stderr, "ERROR record at seq %llu\n",
				(unsigned long long)r.seq);
			rc = 1;
			break;
		}
		have_seq = 1;
		expect_seq = r.seq + 1;

		buf = NULL;
		if (payload) {
			buf = malloc(payload);
			if (!buf || read_all(in, buf, payload)) {
				fprintf(stderr, "truncated at seq %llu\n",
					(unsigned long long)r.seq);
				rc = 1;
				free(buf);
				break;
			}
		}

		switch (r.type) {
		case DM_CDP_REC_WRITE:
			if (write_all(tfd, buf, r.len, (off_t)r.sector * 512)) {
				perror("pwrite");
				rc = 1;
			}
			bytes += r.len;
			applied++;
			break;
		case DM_CDP_REC_ZERO:
		case DM_CDP_REC_DISCARD:
			if (r.len > zero_len) {
				free(zero);
				zero = calloc(1, r.len);
				zero_len = zero ? r.len : 0;
			}
			if (!zero || write_all(tfd, zero, r.len, (off_t)r.sector * 512)) {
				perror("pwrite zero");
				rc = 1;
			}
			applied++;
			break;
		default:
			break;	/* FLUSH: nothing to do on replay */
		}
		free(buf);
		if (rc)
			break;
		if (stop_seq >= 0 && (long long)r.seq >= stop_seq)
			break;
	}

	fsync(tfd);
	close(tfd);
	fclose(in);
	free(zero);
	fprintf(stderr, "applied %llu records, %llu bytes, last seq %llu\n",
		(unsigned long long)applied, (unsigned long long)bytes,
		have_seq ? (unsigned long long)(expect_seq - 1) : 0ULL);
	return rc;
}
