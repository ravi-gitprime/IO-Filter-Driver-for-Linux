# SPDX-License-Identifier: GPL-2.0
"""dm-cdp userspace ABI for Python (mirrors include/uapi/linux/dm-cdp.h)."""
import fcntl
import struct
import ctypes

REC_MAGIC = 0x52504443
ABI_VERSION = 1

REC_WRITE, REC_FLUSH, REC_DISCARD, REC_ZERO, REC_PAD = 1, 2, 3, 4, 5
F_FUA, F_PREFLUSH, F_ERROR, F_GAP_BEFORE = 1, 2, 4, 8
S_OVERFLOWED, S_DEAD = 1, 2
BM_CLEAR = 1

# struct dm_cdp_rec: magic u32, version u16, type u16, flags u32, state u32,
#                    seq u64, sector u64, len u32, rec_len u32, ts_ns u64, reserved u64
REC_FMT = "<IHHIIQQIIQQ"
REC_SIZE = struct.calcsize(REC_FMT)           # 56
assert REC_SIZE == 56

# struct dm_cdp_status (128 bytes)
STATUS_FMT = "<IIQIIQQQQQQQQQ4Q"
STATUS_SIZE = struct.calcsize(STATUS_FMT)
assert STATUS_SIZE == 128
STATUS_FIELDS = ("abi_version", "flags", "dev_sectors", "chunk_sectors", "minor",
                 "bitmap_bits", "bitmap_bytes", "bitmap_set", "ring_size", "ring_used",
                 "seq_next", "records", "bytes_logged", "overflows")

IOC_STATUS = 0x8080CD01
IOC_BITMAP = 0xC018CD02
IOC_BITMAP_CLEAR = 0xCD03
IOC_RESET_STATS = 0xCD04


class Rec:
    __slots__ = ("magic", "version", "type", "flags", "state", "seq", "sector",
                 "len", "rec_len", "ts_ns", "reserved")

    def __init__(self, buf, off=0):
        (self.magic, self.version, self.type, self.flags, self.state, self.seq,
         self.sector, self.len, self.rec_len, self.ts_ns, self.reserved) = \
            struct.unpack_from(REC_FMT, buf, off)
        if self.magic != REC_MAGIC:
            raise ValueError("bad record magic at %d" % off)

    @property
    def payload_len(self):
        return self.rec_len - REC_SIZE


def pack_rec(rtype, flags, seq, sector, length, ts_ns, payload=b""):
    rec_len = (REC_SIZE + len(payload) + 7) & ~7
    hdr = struct.pack(REC_FMT, REC_MAGIC, ABI_VERSION, rtype, flags, 2, seq,
                      sector, length, rec_len, ts_ns, 0)
    return hdr + payload + b"\0" * (rec_len - REC_SIZE - len(payload))


def iter_records(buf):
    """Yield (Rec, payload_memoryview) for every complete record in buf.
    Returns the number of bytes consumed via StopIteration.value."""
    off, n = 0, len(buf)
    mv = memoryview(buf)
    while off + REC_SIZE <= n:
        r = Rec(mv, off)
        if off + r.rec_len > n:
            break
        yield r, mv[off + REC_SIZE: off + REC_SIZE + (r.len if r.type == REC_WRITE else 0)]
        off += r.rec_len
    return off


def status(fd):
    buf = bytearray(STATUS_SIZE)
    fcntl.ioctl(fd, IOC_STATUS, buf, True)
    vals = struct.unpack(STATUS_FMT, buf)
    return dict(zip(STATUS_FIELDS, vals))


def read_bitmap(fd, clear=False):
    """Return (bitmap_bytes, chunk_sectors, nbits). Optionally read-and-clear."""
    st = status(fd)
    nbytes = st["bitmap_bytes"]
    bm = ctypes.create_string_buffer(nbytes)
    req = struct.pack("<QQII", ctypes.addressof(bm), nbytes, BM_CLEAR if clear else 0, 0)
    fcntl.ioctl(fd, IOC_BITMAP, req)
    return bytes(bm.raw), st["chunk_sectors"], st["bitmap_bits"]


def bitmap_extents(bm, chunk_sectors, nbits):
    """Yield (sector, nsectors) runs of set bits. Bitmap is native little-endian longs."""
    start = None
    for i in range(nbits + 1):
        bit = i < nbits and (bm[i >> 3] >> (i & 7)) & 1
        if bit and start is None:
            start = i
        elif not bit and start is not None:
            yield start * chunk_sectors, (i - start) * chunk_sectors
            start = None
