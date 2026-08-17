#!/usr/bin/env python3
"""Which openborconstant SAMPLE_* names does the PAK library actually use?

Lust Rush dies at load with:

    Loading scripts.............. Can't find openbor constant 'SAMPLE_BEEP2'
    Script compile error in 'update': openborconstant line 145, column 31

v7533 restructured the hard-coded sounds into `s_global_sample
global_sample_list` (openbor.c:342) and registers **no SAMPLE_* constants at
all** in source/openborscript/constants.c -- so every PAK built against an
engine that exposed them fails to compile its scripts and shuts down.

Rather than guess the legacy names from the struct members (`one_up` ->
SAMPLE_1UP? SAMPLE_ONEUP?), this reads the names the PAKs THEMSELVES reference.
That is the set the fix has to cover, and it is measured rather than inferred.

Usage:  python3 pak_sampleconst_scan.py <dir-of-paks>
Read-only. Standard library only.
"""
import os
import re
import struct
import sys

HEAD = struct.Struct("<III")
# openborconstant("SAMPLE_X") / openborconstant('SAMPLE_X')
CONST_RE = re.compile(rb"""openborconstant\s*\(\s*['"]\s*(SAMPLE_[A-Za-z0-9_]+)\s*['"]""")
# any SAMPLE_ token, to catch indirect uses the call-site regex would miss
LOOSE_RE = re.compile(rb"\bSAMPLE_[A-Za-z0-9_]+\b")

TEXT_EXT = (".c", ".txt", ".h")


def entries(path):
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
        name = blob[p + 12:p + elen].split(b"\0", 1)[0]
        name = name.decode("latin-1").replace("\\", "/").lower()
        yield name, off, size
        p += elen


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = sys.argv[1]
    paks = sorted(f for f in os.listdir(root) if f.lower().endswith(".pak"))
    if not paks:
        print("no .pak files in %s" % root)
        return 2

    per_name = {}      # SAMPLE_X -> set of paks
    per_pak = {}       # pak -> set of SAMPLE_X
    errors = 0

    for nm in paks:
        p = os.path.join(root, nm)
        found = set()
        try:
            members = [(n, o, s) for n, o, s in entries(p)
                       if n.endswith(TEXT_EXT) and 0 < s <= 4 * 1024 * 1024]
        except Exception as e:
            errors += 1
            print("  ERROR %s: %s" % (nm, e))
            continue
        try:
            with open(p, "rb") as f:
                for _n, off, size in members:
                    f.seek(off)
                    blob = f.read(size)
                    for m in CONST_RE.findall(blob):
                        found.add(m.decode("latin-1"))
                    for m in LOOSE_RE.findall(blob):
                        found.add(m.decode("latin-1"))
        except Exception as e:
            errors += 1
            print("  ERROR reading %s: %s" % (nm, e))
            continue
        if found:
            per_pak[nm] = found
            for f_ in found:
                per_name.setdefault(f_, set()).add(nm)

    print()
    print("=== SAMPLE_* openborconstants referenced by the PAK library ===")
    print("PAKs scanned      : %d" % len(paks))
    print("unreadable        : %d" % errors)
    print("PAKs referencing  : %d" % len(per_pak))
    print()
    if per_name:
        print("%-22s %s" % ("CONSTANT", "PAKs"))
        for name in sorted(per_name, key=lambda k: (-len(per_name[k]), k)):
            print("  %-20s %d" % (name, len(per_name[name])))
        print()
        print("--- which PAKs (first 15) ---")
        for nm in sorted(per_pak)[:15]:
            print("  %-48s %s" % (nm[:48], ",".join(sorted(per_pak[nm]))))
    else:
        print("none found")
    return 0


if __name__ == "__main__":
    sys.exit(main())
