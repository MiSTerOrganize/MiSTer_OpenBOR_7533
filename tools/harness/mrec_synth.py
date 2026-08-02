#!/usr/bin/env python3
"""mrec_synth.py -- write and inspect .inp takes (MREC container v2).

WHY THIS EXISTS
---------------
Two separate problems, one tool.

1. INPUT INJECTION. Driving the real binary hands-free needs a way to feed it a
   chosen button sequence. Keyboard input is not wired through the SDL dummy
   driver on either OpenBOR build (see the feature matrix), so a keyboard-based
   automation tool can reach MiSTer's OSD but NOT in-core input. A synthesised
   .inp goes in through the recorder's own playback path, which means the real
   binary, the real engine tick, and the real video path.

2. HOSTILE INPUT. The payload reader turns bytes written by a stranger into
   filenames on a root filesystem. That control has to be testable with
   deliberately malformed takes -- ../ members, a .p8/.sh entry, a truncated
   record, a header claiming two million frames -- and hand-crafting those with
   dd is exactly how the container offsets went stale in the first place.

FORMAT (must match apply_patches.py; see MREC_OFF_* there)
    "MREC"(4) u32 container=2  u32 engine_ver  char[12] build  char[256] pak
    u64 seed  u32 frame_count  u32 crc32
    identity: u16 entry_count, [u16 name_len, name, u8 sha1[20]],
              u16 stem_len, stem
    frames:   frame_count * 9 * u64  ([0]=interval, [1..4]=keyflags,
                                      [5..8]=newkeyflags)
    payload:  u32 file_count, [u32 name_len, name, u32 data_len, data]

The frame CRC32 is CRC-32/ISO-HDLC (poly 0xEDB88320), matching mrec_crc32.

Every field is written little-endian and fixed-width on purpose: the format
forbids `long`, which is 4 bytes on armhf and 8 on a 64-bit producer -- a
desktop tool emitting 8 would leave the ARM reader 4 bytes out of phase with
every validation still passing.

USAGE
    mrec_synth.py write  out.inp --pak NAME [--frames N] [--hold BTN@S:E] ...
    mrec_synth.py inspect take.inp
"""

import argparse
import binascii
import hashlib
import struct
import sys

CONTAINER = 2
ENGINE_VER = 1
FRAME_WORDS = 9          # [0]=interval, [1..4]=keyflags, [5..8]=newkeyflags
MAX_FRAMES = 2000000


def crc32(b):
    """CRC-32/ISO-HDLC -- same polynomial and conventions as mrec_crc32()."""
    return binascii.crc32(b) & 0xFFFFFFFF


def build_frames(n, holds):
    """n frames of 9 u64. interval=1 each: the engine forces the recorded
    interval back on playback, which is what makes a replay independent of
    frame cost. holds is [(bitmask, player, start, end)]."""
    out = bytearray()
    for f in range(n):
        w = [0] * FRAME_WORDS
        w[0] = 1
        for mask, player, start, end in holds:
            if start <= f < end:
                w[1 + player] |= mask            # keyflags: held
                if f == start:
                    w[5 + player] |= mask        # newkeyflags: the press edge
        out += struct.pack("<9Q", *w)
    return bytes(out)


def build_identity(pak_display, content_hash, stem):
    """entry_count is 0 or 1. Zero means 'this take does not vouch for its
    content' -- which is what the writer emits when the PAK cannot be hashed,
    deliberately, so two unhashable PAKs never compare equal."""
    if content_hash is None:
        b = struct.pack("<H", 0)
    else:
        nm = pak_display.encode("utf-8")
        b = struct.pack("<H", 1) + struct.pack("<H", len(nm)) + nm + content_hash
    s = stem.encode("utf-8")
    return b + struct.pack("<H", len(s)) + s


def build_payload(files):
    """files is [(name, bytes)]. Length-prefixed records, never tar: BusyBox
    tar -x has varied in how it treats ../ members and it follows symlink
    members, which on a root filesystem is a persistence primitive."""
    b = struct.pack("<I", len(files))
    for name, data in files:
        nb = name.encode("utf-8")
        b += struct.pack("<I", len(nb)) + nb + struct.pack("<I", len(data)) + data
    return b


def write_take(path, pak="TestPak", frames=60, holds=(), seed=1,
               content_hash=b"\x00" * 20, stem=None, payload=(),
               container=CONTAINER, engine_ver=ENGINE_VER,
               build="Jan  1 2026", truncate_after=None,
               claim_frames=None, bad_magic=False, corrupt_crc=False):
    if stem is None:
        stem = pak
    fr = build_frames(frames, holds)
    n_claim = claim_frames if claim_frames is not None else frames
    crc = crc32(fr)
    if corrupt_crc:
        crc ^= 0xFFFFFFFF

    hdr = b"XXXX" if bad_magic else b"MREC"
    hdr += struct.pack("<I", container)
    hdr += struct.pack("<I", engine_ver)
    hdr += build.encode("utf-8")[:12].ljust(12, b"\0")
    hdr += pak.encode("utf-8")[:256].ljust(256, b"\0")
    hdr += struct.pack("<Q", seed)
    hdr += struct.pack("<I", n_claim)
    hdr += struct.pack("<I", crc)

    blob = hdr + build_identity(pak, content_hash, stem) + fr + build_payload(list(payload))
    if truncate_after is not None:
        blob = blob[:truncate_after]
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


def inspect(path):
    d = open(path, "rb").read()
    if len(d) < 296:
        print("too short to be a v2 take: %d bytes" % len(d))
        return 1
    magic = d[0:4]
    cont, ever = struct.unpack("<II", d[4:12])
    build = d[12:24].split(b"\0")[0].decode("utf-8", "replace")
    pak = d[24:280].split(b"\0")[0].decode("utf-8", "replace")
    seed, = struct.unpack("<Q", d[280:288])
    n, crc = struct.unpack("<II", d[288:296])
    print("magic       %r" % magic)
    print("container   %d" % cont)
    print("engine_ver  %d" % ever)
    print("build       %s" % build)
    print("pak         %s" % pak)
    print("seed        %d" % seed)
    print("frames      %d" % n)
    print("crc32       %08x" % crc)

    i = 296
    cnt, = struct.unpack("<H", d[i:i + 2]); i += 2
    print("identity    %d entr%s" % (cnt, "y" if cnt == 1 else "ies"))
    if cnt == 1:
        nl, = struct.unpack("<H", d[i:i + 2]); i += 2
        nm = d[i:i + nl].decode("utf-8", "replace"); i += nl
        h = d[i:i + 20]; i += 20
        print("  name      %s" % nm)
        print("  sha1      %s" % h.hex())
    sl, = struct.unpack("<H", d[i:i + 2]); i += 2
    print("stem        %s" % d[i:i + sl].decode("utf-8", "replace")); i += sl

    fstart = i
    body = d[fstart:fstart + n * FRAME_WORDS * 8]
    print("frame block %d bytes at %d (crc %s)"
          % (len(body), fstart,
             "OK" if crc32(body) == crc else "MISMATCH"))
    i = fstart + n * FRAME_WORDS * 8
    if i + 4 <= len(d):
        fc, = struct.unpack("<I", d[i:i + 4]); i += 4
        print("payload     %d file(s)" % fc)
        for _ in range(fc):
            if i + 4 > len(d):
                print("  <truncated>"); break
            nl, = struct.unpack("<I", d[i:i + 4]); i += 4
            nm = d[i:i + nl].decode("utf-8", "replace"); i += nl
            dl, = struct.unpack("<I", d[i:i + 4]); i += 4
            print("  %-40s %d bytes" % (nm, dl))
            i += dl
    else:
        print("payload     <absent>")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("write")
    w.add_argument("out")
    w.add_argument("--pak", default="TestPak")
    w.add_argument("--frames", type=int, default=60)
    w.add_argument("--seed", type=int, default=1)
    w.add_argument("--stem")
    w.add_argument("--hash", help="40-char hex content hash, or 'none' for an "
                                  "unverifiable take")
    w.add_argument("--hold", action="append", default=[],
                   metavar="MASK@P:START:END",
                   help="hold bitmask MASK for player P over [START,END)")

    i = sub.add_parser("inspect")
    i.add_argument("take")

    a = ap.parse_args()
    if a.cmd == "inspect":
        return inspect(a.take)

    holds = []
    for h in a.hold:
        mask, rest = h.split("@", 1)
        player, start, end = rest.split(":")
        holds.append((int(mask, 0), int(player), int(start), int(end)))
    if a.hash == "none":
        ch = None
    elif a.hash:
        ch = bytes.fromhex(a.hash)
    else:
        ch = hashlib.sha1(a.pak.encode()).digest()
    n = write_take(a.out, pak=a.pak, frames=a.frames, holds=holds,
                   seed=a.seed, content_hash=ch, stem=a.stem)
    print("wrote %s (%d bytes)" % (a.out, n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
