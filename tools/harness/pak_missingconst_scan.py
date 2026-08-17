#!/usr/bin/env python3
"""Every openborconstant a PAK asks for that v7533 does NOT register.

Fixing these one at a time costs a build cycle each -- Lust Rush needed
SAMPLE_BEEP2, and once that was registered it moved straight on to
FRONTPANEL_Z. This enumerates the whole class in one pass so the fix can be
complete instead of iterative.

  asked-for  = every openborconstant("NAME") literal in every PAK's scripts
  registered = every name constants.c registers, via ICMPCONST / IICMPCONST
  MISSING    = asked-for minus registered

Prefix-matching macros (ICMPSCONSTA/B/C) are handled separately: they accept
NAME1..NAME9 style families, so a literal matching one of those prefixes is NOT
missing even though the exact string never appears.

Usage:  python3 pak_missingconst_scan.py <dir-of-paks> <path-to-constants.c>
Read-only. Standard library only.
"""
import os
import re
import struct
import sys

HEAD = struct.Struct("<III")
CALL_RE = re.compile(rb"""openborconstant\s*\(\s*['"]\s*([A-Za-z_][A-Za-z0-9_]*)\s*['"]""")
TEXT_EXT = (".c", ".txt", ".h")

REG_RE = re.compile(r"\bI?ICMPCONST\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)")
PREFIX_RE = re.compile(r"\bICMPSCONST[ABC]\s*\(\s*([A-Za-z_][A-Za-z0-9_]*)")


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
        yield name.decode("latin-1").replace("\\", "/").lower(), off, size
        p += elen


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    root, cpath = sys.argv[1], sys.argv[2]

    with open(cpath, encoding="utf-8", errors="replace") as f:
        cdata = f.read()
    registered = {m.upper() for m in REG_RE.findall(cdata)}
    prefixes = {m.upper() for m in PREFIX_RE.findall(cdata)}
    if not registered:
        print("ERROR: parsed 0 registered constants from %s -- wrong file?" % cpath)
        return 2

    paks = sorted(f for f in os.listdir(root) if f.lower().endswith(".pak"))
    asked = {}          # NAME -> set(paks)
    errors = 0

    for nm in paks:
        p = os.path.join(root, nm)
        try:
            members = [(n, o, s) for n, o, s in entries(p)
                       if n.endswith(TEXT_EXT) and 0 < s <= 4 * 1024 * 1024]
            with open(p, "rb") as f:
                for _n, off, size in members:
                    f.seek(off)
                    for m in CALL_RE.findall(f.read(size)):
                        asked.setdefault(m.decode("latin-1").upper(), set()).add(nm)
        except Exception as e:
            errors += 1
            print("  ERROR %s: %s" % (nm, e))

    def covered(name):
        if name in registered:
            return True
        # NAME1..NAME9 families
        return any(name.startswith(p) and name[len(p):].isdigit() for p in prefixes)

    missing = {k: v for k, v in asked.items() if not covered(k)}

    print()
    print("=== openborconstants the library asks for, vs what v7533 registers ===")
    print("PAKs scanned          : %d  (unreadable %d)" % (len(paks), errors))
    print("registered by engine  : %d   (+%d prefix families)" % (len(registered), len(prefixes)))
    print("distinct names asked  : %d" % len(asked))
    print("MISSING               : %d" % len(missing))
    print()
    if missing:
        print("%-26s %5s  %s" % ("CONSTANT", "PAKs", "example PAK"))
        for name in sorted(missing, key=lambda k: (-len(missing[k]), k)):
            ex = sorted(missing[name])[0]
            print("  %-24s %4d  %s" % (name, len(missing[name]), ex[:44]))
        affected = set()
        for v in missing.values():
            affected |= v
        print()
        print("PAKs blocked by at least one missing constant: %d" % len(affected))
        for a in sorted(affected):
            need = sorted(k for k, v in missing.items() if a in v)
            print("  %-46s %s" % (a[:46], ",".join(need)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
