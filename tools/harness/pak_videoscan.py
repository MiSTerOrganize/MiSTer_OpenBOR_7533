#!/usr/bin/env python3
"""
pak_videoscan.py -- engine-exact native-resolution census of an OpenBOR PAK library.

Reads ONLY the packfile directory (at the file tail) and each PAK's data/video.txt.
Bounded: a few hundred KB per PAK regardless of PAK size. Run it LOCALLY, never on the
MiSTer (see the never-scan-the-PAK-library-on-device rule).

Replicates v7533 exactly:
  - packfile directory  : engine/source/gamelib/packfile.h  pnamestruct
                          {u32 pns_len, u32 filestart, u32 filesize, char namebuf[]}
                          last 4 bytes of the file = directory start offset
  - video.txt parser    : engine/openbor.c:48609-48806, line by line, LAST `video` wins
  - arg tokenizer       : engine/openbor.c ParseArgs -- '#', '\\r', '\\n', '\\0' end the
                          line; space/tab separate; quotes protect
  - `video WxH`         : strchr(value, 'x') -- LOWERCASE x ONLY -> hRes/vRes, mode 255
  - `video N`           : atoi -> preset table below
  - no/unreadable file  : mode 0 -> 320x240
  - mode 7+             : the engine SHUTS DOWN; reported here as INVALID

Usage:  python pak_videoscan.py <paks_dir> [-o out.tsv]
"""

import os, sys, struct, argparse

# engine/openbor.c:48692 switch (videoMode)
PRESETS = {
    0: (320, 240),
    1: (480, 272),
    2: (640, 480),
    3: (720, 480),
    4: (800, 480),
    5: (800, 600),
    6: (960, 540),
}
DEFAULT = PRESETS[0]


def atoi(s):
    """C atoi: leading whitespace, optional sign, leading digits, else 0."""
    i, n = 0, len(s)
    while i < n and s[i] in " \t\n\r\f\v":
        i += 1
    sign = 1
    if i < n and s[i] in "+-":
        sign = -1 if s[i] == "-" else 1
        i += 1
    j = i
    while j < n and s[j].isdigit():
        j += 1
    return sign * int(s[i:j]) if j > i else 0


def parse_args(line):
    """ParseArgs: split on space/tab; '#', CR, LF, NUL terminate; quotes protect."""
    args, cur, dq, sq = [], [], False, False
    for ch in line:
        if ch == '"' and not sq:
            dq = not dq
            cur.append(ch)
        elif ch == "'" and not dq:
            sq = not sq
            cur.append(ch)
        elif ch in ("#", "\r", "\n", "\0") and not dq and not sq:
            break
        elif ch in (" ", "\t") and not dq and not sq:
            if cur:
                args.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        args.append("".join(cur))
    return args


def parse_video_txt(text):
    """Engine-exact: walk every line, LAST `video` directive wins."""
    mode, w, h = 0, None, None
    for line in text.splitlines():
        a = parse_args(line)
        if not a or not a[0]:
            continue
        if a[0].lower() != "video":
            continue
        value = a[1] if len(a) > 1 else ""
        if "x" in value:                      # LOWERCASE x only, as strchr()
            w = atoi(value)
            h = atoi(value[value.index("x") + 1:])
            mode = 255
        else:
            mode = atoi(value)
    if mode == 255:
        return w, h, 255
    if mode in PRESETS:
        return PRESETS[mode][0], PRESETS[mode][1], mode
    return None, None, mode                   # 7+ -> engine shutdown


def read_video_txt(path):
    """Return (text, note). Reads only the directory tail + the one file range."""
    size = os.path.getsize(path)
    if size < 8:
        return None, "too small"
    with open(path, "rb") as f:
        f.seek(-4, os.SEEK_END)
        dir_off = struct.unpack("<I", f.read(4))[0]
        if dir_off >= size:
            return None, "bad directory offset"
        f.seek(dir_off)
        directory = f.read(size - dir_off - 4)

        pos, hit = 0, None
        while pos + 12 <= len(directory):
            pns_len, filestart, filesize = struct.unpack_from("<III", directory, pos)
            if pns_len < 12 or pos + pns_len > len(directory):
                break
            raw = directory[pos + 12: pos + pns_len]
            name = raw.split(b"\0", 1)[0].decode("latin-1").replace("\\", "/").lower()
            if name.endswith("data/video.txt") or name == "video.txt":
                hit = (filestart, filesize)
                break                          # engine takes the first match
            pos += pns_len
        if hit is None:
            return None, "no video.txt"
        start, length = hit
        if start + length > size or length <= 0:
            return None, "bad file range"
        f.seek(start)
        return f.read(length).decode("latin-1"), "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paks_dir")
    ap.add_argument("-o", "--out", default="pak_videoscan.tsv")
    a = ap.parse_args()

    paks = sorted(p for p in os.listdir(a.paks_dir) if p.lower().endswith(".pak"))
    rows, fails = [], 0
    for i, pak in enumerate(paks, 1):
        full = os.path.join(a.paks_dir, pak)
        try:
            text, note = read_video_txt(full)
        except Exception as e:
            text, note = None, "error: %s" % e
        if text is None:
            w, h, mode = DEFAULT[0], DEFAULT[1], 0     # engine default
            src = note
        else:
            w, h, mode = parse_video_txt(text)
            src = "mode %s" % mode
            if w is None:
                fails += 1
                src = "INVALID mode %s (engine would SHUT DOWN)" % mode
        rows.append((pak[:-4], w, h, mode, src))
        if i % 50 == 0:
            print("  ...%d/%d" % (i, len(paks)), file=sys.stderr)

    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("width\theight\tmode\tsource\tpak\n")
        for name, w, h, mode, src in rows:
            f.write("%s\t%s\t%s\t%s\t%s\n" % (w, h, mode, src, name))

    counts = {}
    for _, w, h, _, _ in rows:
        counts[(w, h)] = counts.get((w, h), 0) + 1
    print("\n%d PAKs scanned -> %s" % (len(rows), a.out))
    print("%d distinct resolutions%s\n" % (len(counts),
          ", %d INVALID" % fails if fails else ""))
    print("%12s %6s" % ("W x H", "PAKs"))
    for (w, h), n in sorted(counts.items(), key=lambda kv: (-(kv[0][0] or 0) * (kv[0][1] or 0))):
        print("%12s %6d" % ("%sx%s" % (w, h), n))


if __name__ == "__main__":
    main()
