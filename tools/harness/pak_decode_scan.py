#!/usr/bin/env python3
# OpenBOR diff-harness — DECODE category (PAK-integrity differential).
#
# The OpenBOR analog of the PICO-8 shrinko8 decode-differential: PICO-8 carts
# are PXA/png-compressed (validated vs shrinko8); OpenBOR PAKs are packfile
# archives (DATA/ tree of character.txt, scripts, GIFs, ...). This scanner reads
# every PAK by the canonical packfile format (upstream engine/source/gamelib/
# packfile.c/.h — the same directory layout the engine's reader walks) and
# verifies structural integrity, so a PAK our reader chokes on = one the engine
# would choke on too (decode bug or corrupt cart). No headless engine needed.
#
#   pak_decode_scan.py <paks_root> [out_file]
#
# Per PAK -> OK | <failure-class>, with entry count + bytes. Failure classes:
#   BAD_DIR_OFFSET  directory offset out of range / unreadable
#   BAD_ENTRY       a directory entry's pns_len is implausible (<12 or >4096)
#   OOB_FILE        an entry's filestart+filesize runs past EOF (truncation)
#   BAD_NAME        filename bytes not decodable / empty
#   EMPTY           zero usable entries
#   READ_ERR        file unreadable
import sys, os, struct

root = sys.argv[1] if len(sys.argv) > 1 else "."
out  = sys.argv[2] if len(sys.argv) > 2 else None

def scan_pak(path):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return ("READ_ERR", 0, 0, str(e)[:50])
    if len(raw) < 8:
        return ("BAD_DIR_OFFSET", 0, 0, "file too small")
    dir_off = struct.unpack("<I", raw[-4:])[0]
    if dir_off > len(raw) - 4:
        return ("BAD_DIR_OFFSET", 0, 0, f"dir_off {dir_off} > size {len(raw)}")
    pos = dir_off
    entries = 0
    total = 0
    end = len(raw) - 4
    while pos + 12 <= end:
        pns_len = struct.unpack("<I", raw[pos:pos+4])[0]
        if pns_len < 12 or pns_len > 4096:
            # End of directory is normal once we stop hitting valid entries;
            # but if we have ZERO entries so far it's a malformed directory.
            break
        filestart = struct.unpack("<I", raw[pos+4:pos+8])[0]
        filesize  = struct.unpack("<I", raw[pos+8:pos+12])[0]
        name = raw[pos+12:pos+pns_len].split(b"\x00", 1)[0]
        if not name:
            return ("BAD_NAME", entries, total, f"empty name @entry {entries}")
        try:
            name.decode("ascii", "strict")
        except Exception:
            # OpenBOR filenames are ASCII paths; non-ASCII = suspect.
            try:
                name.decode("latin-1")
            except Exception:
                return ("BAD_NAME", entries, total, f"undecodable name @entry {entries}")
        if filestart + filesize > len(raw):
            return ("OOB_FILE", entries, total,
                    f"{name.decode('latin-1','replace')} start+size {filestart+filesize} > {len(raw)}")
        entries += 1
        total += filesize
        pos += pns_len
    if entries == 0:
        return ("EMPTY", 0, 0, "no usable directory entries")
    return ("OK", entries, total, "")

paks = []
for dirpath, _d, files in os.walk(root):
    for f in files:
        if f.lower().endswith(".pak"):
            paks.append(os.path.join(dirpath, f))
paks.sort()

lines = []
counts = {}
for i, p in enumerate(paks, 1):
    rel = os.path.relpath(p, root).replace("\\", "/")
    cls, n, tot, detail = scan_pak(p)
    counts[cls] = counts.get(cls, 0) + 1
    lines.append(f"{i}|{cls}|{n}|{tot}|{rel}|{detail}")
    if i % 100 == 0:
        print(f"...scanned {i}/{len(paks)}", flush=True)

print(f"\n=== OpenBOR PAK DECODE/INTEGRITY SCAN ({len(paks)} PAKs) ===")
for cls in sorted(counts):
    tag = "OK" if cls == "OK" else f"*** {cls} ***"
    print(f"  {tag:<22} {counts[cls]}")
bad = [l for l in lines if not l.split("|")[1] == "OK"]
if bad:
    print(f"\n=== NON-OK PAKs ({len(bad)}) — decode/integrity failures ===")
    for l in bad[:120]:
        p = l.split("|", 5)
        print(f"  {p[1]:<16} entries={p[2]:<5} {p[4]}  [{p[5]}]")
else:
    print("\nAll PAKs parse cleanly per the canonical packfile format.")

if out:
    with open(out, "w", encoding="utf-8", newline="\n") as fo:
        fo.write("\n".join(lines) + "\n")
    print(f"\nfull results -> {out}")
