#!/usr/bin/env python3
"""
pak_spritesize_scan.py -- STATIC upper bound on each PAK's sprite memory.

Answers the Tier-B sprite-arena sizing question for ALL 450 PAKs, including the
ones the runtime scan cannot reach (Lust Rush and the other ec=1 PAKs, and any
PAK whose deep content a 900-frame bot never loads).

WHY A STATIC BOUND IS NEEDED: OpenBOR loads models on demand
(`load_cached_model()` -> `model_cache[]`, each entry with an `unload` flag), so
a runtime working-set sample is only ever a LOWER bound. Measured: Ultimate
Double Dragon is a 1.31 GB PAK whose observed working set was 20.4 MB = 1.5%.

METHOD: walk each packfile directory, and for every image member read ONLY its
header to get width x height. Sum. Bounded: directory + ~32 bytes per image.

  GIF87a/89a : width = LE u16 @ 6,  height = LE u16 @ 8
  PNG        : IHDR, width = BE u32 @ 16, height = BE u32 @ 20

INTERPRETATION -- this is a deliberate OVER-estimate, and by two known factors:
  1. Sprites are stored RLE (`encodesprite`), roughly 1 byte per OPAQUE pixel
     plus run overhead, so fully-transparent regions cost nothing. w*h is the
     no-transparency worst case.
  2. Not every image becomes a sprite -- backgrounds, panels, fonts and menu art
     load through other paths. Counting every image over-counts.
Both push the same way, so the result is a genuine CEILING: if a PAK fits this,
it fits. Pair with pak_blend_runtime_*.tsv (the measured lower bound) to bracket
the real figure.

Usage:  python pak_spritesize_scan.py <paks_dir> [-o out.tsv]
Run LOCALLY, never on the MiSTer.
"""

import os, sys, struct, argparse

IMG_EXT = (".gif", ".png")


def image_wh(f, start, length):
    """Header-only read. Returns (w, h) or None if not a recognised image."""
    if length < 26:
        return None
    f.seek(start)
    hdr = f.read(26)
    if len(hdr) < 26:
        return None
    if hdr[:3] == b"GIF":
        w, h = struct.unpack_from("<HH", hdr, 6)
        return (w, h) if w and h else None
    if hdr[:8] == b"\x89PNG\r\n\x1a\n" and hdr[12:16] == b"IHDR":
        w, h = struct.unpack_from(">II", hdr, 16)
        return (w, h) if w and h and w < 65536 and h < 65536 else None
    return None


def scan_pak(path):
    size = os.path.getsize(path)
    if size < 8:
        return 0, 0, 0
    px = files = skipped = 0
    with open(path, "rb") as f:
        f.seek(-4, os.SEEK_END)
        dir_off = struct.unpack("<I", f.read(4))[0]
        if dir_off >= size:
            return 0, 0, 0
        f.seek(dir_off)
        directory = f.read(size - dir_off - 4)
        pos = 0
        while pos + 12 <= len(directory):
            pns_len, start, length = struct.unpack_from("<III", directory, pos)
            if pns_len < 12 or pos + pns_len > len(directory):
                break
            raw = directory[pos + 12: pos + pns_len]
            name = raw.split(b"\0", 1)[0].decode("latin-1").replace("\\", "/").lower()
            pos += pns_len
            if not name.endswith(IMG_EXT):
                continue
            if start + length > size:
                skipped += 1
                continue
            wh = image_wh(f, start, length)
            if wh is None:
                skipped += 1
                continue
            px += wh[0] * wh[1]
            files += 1
    return px, files, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paks_dir")
    ap.add_argument("-o", "--out", default="pak_spritesize.tsv")
    a = ap.parse_args()

    paks = sorted(p for p in os.listdir(a.paks_dir) if p.lower().endswith(".pak"))
    rows = []
    for i, pak in enumerate(paks, 1):
        try:
            px, n, sk = scan_pak(os.path.join(a.paks_dir, pak))
        except Exception as e:
            print("  ERROR %s: %s" % (pak, e), file=sys.stderr)
            px, n, sk = 0, 0, -1
        rows.append((pak[:-4], px, n, sk))
        if i % 25 == 0:
            print("  ...%d/%d" % (i, len(paks)), file=sys.stderr)

    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("pak\tsprite_px_upper\timages\tskipped\n")
        for n, px, c, sk in rows:
            f.write("%s\t%d\t%d\t%d\n" % (n, px, c, sk))

    ranked = sorted(rows, key=lambda r: -r[1])
    mb = lambda p: p / 1048576.0            # 1 byte/px upper bound
    print("\n%d PAKs -> %s\n" % (len(rows), a.out))
    print("STATIC UPPER BOUND on sprite bytes (1 byte per pixel, no transparency)")
    print("  largest %.0f MB (%s)   median %.1f MB   total %.1f GB"
          % (mb(ranked[0][1]), ranked[0][0], mb(ranked[len(ranked) // 2][1]),
             sum(r[1] for r in rows) / 1073741824.0))
    print("\ntop 20:")
    for n, px, c, sk in ranked[:20]:
        print("   %8.0f MB  %7d images  %s" % (mb(px), c, n))
    print("\narena sizing against this CEILING:")
    for cap in (64, 128, 160, 256, 512):
        over = [r for r in ranked if mb(r[1]) > cap]
        print("   %4d MB -> %3d PAK(s) exceed the ceiling%s"
              % (cap, len(over), (": " + ", ".join(r[0][:22] for r in over[:3])) if over else ""))


if __name__ == "__main__":
    main()
