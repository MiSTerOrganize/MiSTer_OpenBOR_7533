"""Task 14: will A9's display-space pause menu overflow 320px?

Before A9 the menu was drawn at the PAK's native width and downscaled with the
game. Now it is drawn straight onto a 320-wide surface, so a font sized for a
960-wide PAK is suddenly 3x too big RELATIVE to the frame. _strmidx centres as
(320 - width)/2, which goes NEGATIVE when a string is wider than the surface --
clipping it at BOTH ends.

Measured, not guessed: pull each PAK's video.txt and its pause-menu fonts, and
compute the real string widths.

PAK format (source/gamelib/packfile.c): last 4 bytes = index offset; index is a
run of {u32 pns_len, u32 filestart, u32 filesize, char namebuf[]}, next entry at
+pns_len. Font cell width is gif_width/16 (font.c: font->width = width / 16).
Pause menu fonts come from pauseoffset[] = {0, 1, 0, 0, 3, ...}: font index 0 ->
data/sprites/font, 1 -> font2, 3 -> font4.
"""
import glob
import io
import os
import re
import struct
import sys

PAKDIR = sys.argv[1]
MENU_SRC = sys.argv[2]
SURFACE = 320

WANT = ("data/video.txt", "data/sprites/font.gif",
        "data/sprites/font2.gif", "data/sprites/font4.gif")


def pak_index(path):
    """Yield (name, start, size) for every entry in the PAK index."""
    with open(path, "rb") as f:
        f.seek(-4, os.SEEK_END)
        off = struct.unpack("<I", f.read(4))[0]
        size = f.seek(0, os.SEEK_END)
        while 0 < off < size:
            f.seek(off)
            head = f.read(12)
            if len(head) < 12:
                return
            pns_len, start, fsize = struct.unpack("<III", head)
            if pns_len <= 12 or off + pns_len > size:
                return
            raw = f.read(pns_len - 12)
            name = raw.split(b"\0")[0].decode("latin-1").replace("\\", "/")
            yield name, start, fsize
            off += pns_len


def extract(path, wanted):
    out = {}
    lower = {w.lower() for w in wanted}
    with open(path, "rb") as f:
        for name, start, fsize in pak_index(path):
            if name.lower() in lower:
                f.seek(start)
                out[name.lower()] = f.read(fsize)
    return out


def gif_cell_width(blob):
    """GIF logical width is bytes 6-7 LE; a font sheet is a 16x16 glyph grid."""
    if not blob or blob[:3] != b"GIF":
        return None
    w, h = struct.unpack("<HH", blob[6:10])
    return w // 16, h // 16


def native_res(blob):
    if not blob:
        return None
    txt = blob.decode("latin-1", "replace")
    # Format is "video 960x480" -- NOT space separated. Getting this wrong made
    # every PAK read as the 320x240 default, which hid the only distinction that
    # matters here: whether a PAK's menu already overflowed before A9.
    m = re.search(r"^\s*video\s+(\d+)\s*x\s*(\d+)", txt, re.M | re.I)
    return (int(m.group(1)), int(m.group(2))) if m else None


# The real menu strings, taken from the patch rather than retyped.
src = io.open(MENU_SRC, encoding="utf-8", errors="replace").read()
src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
strings = sorted({m for m in re.findall(r'Tr\("([^"]+)"\)', src)}, key=len, reverse=True)
print(f"menu strings found: {len(strings)}; longest: {strings[0]!r} ({len(strings[0])} chars)\n")

rows = []
for pak in sorted(glob.glob(os.path.join(PAKDIR, "*.pak"))):
    name = os.path.basename(pak)[:-4]
    try:
        got = extract(pak, WANT)
    except Exception as e:
        print(f"  !! {name}: {e}")
        continue
    res = native_res(got.get("data/video.txt"))
    cells = {}
    for idx, fn in ((0, "font.gif"), (1, "font2.gif"), (3, "font4.gif")):
        c = gif_cell_width(got.get("data/sprites/" + fn))
        if c:
            cells[idx] = c
    if not cells:
        continue
    item_w = cells.get(1, cells.get(0))          # selected item font
    if not item_w:
        continue
    cw = item_w[0]
    worst = max(len(s) for s in strings) * cw
    rows.append((name, res, cw, worst, [s for s in strings if len(s) * cw > SURFACE]))

# The distinction that decides whether A9 broke anything:
#   was it ALREADY overflowing at its native width?  -> pre-existing, not us
#   does it only overflow now that we draw at 320?   -> REGRESSION from A9
regress, pre_existing, fine = [], [], []
for name, res, cw, worst, over in rows:
    native_w = res[0] if res else 320
    if not over:
        fine.append(name)
    elif worst > native_w:
        pre_existing.append((name, res, cw, worst, over))
    else:
        regress.append((name, res, cw, worst, over))

regress.sort(key=lambda r: -(r[3] - SURFACE))
print(f"{len(rows)} PAKs measured: {len(fine)} fit, "
      f"{len(regress)} REGRESSED by A9, {len(pre_existing)} already broken before A9\n")

if regress:
    print(f"REGRESSIONS -- fit at native width, clip at {SURFACE}:")
    print(f"  {'PAK':<42}{'native':>10}{'cell':>6}{'widest':>8}{'over by':>9}")
    for name, res, cw, worst, over in regress:
        r = f"{res[0]}x{res[1]}" if res else "?"
        print(f"  {name[:40]:<42}{r:>10}{cw:>6}{worst:>8}{worst - SURFACE:>8}px")
    print()
    longest = max((s for _, _, _, _, ov in regress for s in ov), key=len)
    print(f"  driven by the longest strings, e.g. {longest!r} ({len(longest)} chars)")

if pre_existing:
    print(f"\nALREADY overflowing before A9 (their own native width is too small "
          f"for their font) -- not caused by this change:")
    for name, res, cw, worst, over in pre_existing[:6]:
        r = f"{res[0]}x{res[1]}" if res else "320x240 default"
        print(f"  {name[:40]:<42}{r:>16} cell {cw:>3}  widest {worst}px")
