#!/usr/bin/env python3
"""Register item 16 (E2): does data/videopc.txt change the resolution census?

pak_videoscan.py reads ONLY data/video.txt. The engine does not:

    char *filename = "data/video.txt";
    ...
    #define tryfile(X) if((tmp=openpackfile(X,packfile))!=-1) \
                       { closepackfile(tmp); filename=X; goto readfile; }
    #if WIN || LINUX
        tryfile("data/videopc.txt");

-- videopc.txt WINS OUTRIGHT when present: it replaces `filename` and jumps
straight to the read, so video.txt is never opened. (openbor.c v7533:48548-48568.)

And it applies to us: our Makefile's BUILD_MISTER block sets
`TARGET_PLATFORM = LINUX` (apply_patches.py:217), so `LINUX` is defined and that
branch is live on the MiSTer core.

So any PAK shipping videopc.txt may be counted at the wrong resolution, which
feeds 8.2's R table, 9.6's variant counts and 14.4.5's per-resolution bandwidth.

This scans the library for both files and reports only the PAKs where they
DISAGREE -- those are the census corrections.

Usage:  python3 pak_videopc_scan.py <dir-of-paks>
Standard library only. Read-only: never writes into the PAK library.
"""
import os
import re
import struct
import sys

# packfile directory entry, matching engine/source/gamelib/packfile.h
HEAD = struct.Struct("<III")


def entries(path):
    """Yield (name, offset, size) from the packfile directory at the tail."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        if end < 8:
            return
        f.seek(-4, os.SEEK_END)
        dir_off = struct.unpack("<I", f.read(4))[0]
        if dir_off <= 0 or dir_off >= end:
            return
        f.seek(dir_off)
        blob = f.read(end - dir_off - 4)
    p = 0
    while p + 12 <= len(blob):
        try:
            elen, off, size = HEAD.unpack_from(blob, p)
        except struct.error:
            return
        if elen < 13 or p + elen > len(blob):
            return
        raw = blob[p + 12:p + elen]
        name = raw.split(b"\0", 1)[0].decode("latin-1").replace("\\", "/").lower()
        yield name, off, size
        p += elen


def read_member(path, off, size):
    if size <= 0 or size > 4 * 1024 * 1024:
        return None
    with open(path, "rb") as f:
        f.seek(off)
        return f.read(size)


# The engine takes the LAST `video` line that parses (openbor.c:48609-48806).
VIDEO_RE = re.compile(rb"^\s*video\s+(\d+)\s+(\d+)", re.I | re.M)


def resolution(blob):
    if blob is None:
        return None
    hits = VIDEO_RE.findall(blob)
    if not hits:
        return None
    w, h = hits[-1]
    return (int(w), int(h))


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = sys.argv[1]
    paks = sorted(f for f in os.listdir(root) if f.lower().endswith(".pak"))
    if not paks:
        print("no .pak files in %s" % root)
        return 2

    n_pc = 0
    identical = []
    disagree = []
    only_pc = []
    errors = 0

    for name in paks:
        p = os.path.join(root, name)
        try:
            idx = {n: (o, s) for n, o, s in entries(p)}
        except Exception as e:            # a corrupt PAK must not stop the census
            errors += 1
            print("  ERROR %s: %s" % (name, e))
            continue

        def res_of(member):
            hit = idx.get(member)
            if not hit:
                return None
            return resolution(read_member(p, hit[0], hit[1]))

        r_std = res_of("data/video.txt")
        r_pc = res_of("data/videopc.txt")

        if "data/videopc.txt" in idx:
            n_pc += 1
            # STRONGEST test first, and it needs no parser: if the two members
            # are byte-identical, no parse can make them disagree. This matters
            # because video.txt has TWO forms -- `video <w> <h>` and the
            # mode-number `video <n>` -- and a regex written for one reports the
            # other as "no video line", which looks like a defect when it is a
            # no-op. Super Fightin' Spirit is exactly that case.
            a = idx["data/video.txt"] if "data/video.txt" in idx else None
            b = idx["data/videopc.txt"]
            if a is not None:
                ba = read_member(p, a[0], a[1])
                bb = read_member(p, b[0], b[1])
                if ba is not None and ba == bb:
                    identical.append(name)
                    continue
            if r_pc is None:
                # present but unparseable -- the engine still jumps to it, so the
                # fallthrough is the engine's default, NOT video.txt
                disagree.append((name, r_std, "videopc present but no video line"))
            elif r_std is None:
                only_pc.append((name, r_pc))
            elif r_pc != r_std:
                disagree.append((name, r_std, r_pc))

    print()
    print("=== register item 16 (E2): videopc.txt vs video.txt ===")
    print("PAKs scanned                     : %d" % len(paks))
    print("unreadable                       : %d" % errors)
    print("shipping data/videopc.txt        : %d" % n_pc)
    print("of those, DISAGREEING with video.txt : %d" % len(disagree))
    print("of those, videopc-only (no video.txt): %d" % len(only_pc))
    print("of those, BYTE-IDENTICAL to video.txt : %d" % len(identical))
    print()
    if disagree:
        print("--- census CORRECTIONS (scanner said left, engine uses right) ---")
        for nm, a, b in disagree:
            print("  %-52s %s -> %s" % (nm[:52], a, b))
    if only_pc:
        print("--- videopc-only ---")
        for nm, b in only_pc:
            print("  %-52s        %s" % (nm[:52], b))
    if identical:
        print("--- byte-identical, so no possible divergence ---")
        for nm in identical:
            print("  %s" % nm)
        print()
    if not disagree and not only_pc:
        print("NO CORRECTIONS: every PAK's videopc.txt agrees with its video.txt,")
        print("so 8.2's R table, 9.6's variant counts and 14.4.5 are UNAFFECTED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
