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

# Font sheets are .gif OR .png -- the engine's font_load() takes an extensionless
# path and loadscreen resolves the extension. Looking only for .gif left 57 of
# 450 PAKs unmeasured and mislabelled as "no font sheet", when they simply used
# PNG. Check both, or the coverage number is a fiction.
#
# A few PAKs use the multi-part DIRECTORY form instead -- data/sprites/font/00.gif
# -- which is the MBS font path (font_load takes fontmbs[i] as a flag). Those are
# the last 4 of 450, so they are included rather than written off.
WANT = ("data/video.txt",
        "data/sprites/font.gif", "data/sprites/font.png", "data/sprites/font/00.gif",
        "data/sprites/font2.gif", "data/sprites/font2.png", "data/sprites/font2/00.gif",
        "data/sprites/font4.gif", "data/sprites/font4.png", "data/sprites/font4/00.gif")


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


def cell_size(blob):
    """Glyph cell of a font sheet: the image is a 16x16 grid (font.c:
    font->width = screen->width / 16). GIF stores logical width at bytes 6-7
    little-endian; PNG stores it in IHDR at bytes 16-19 big-endian."""
    if not blob:
        return None
    if blob[:3] == b"GIF":
        w, h = struct.unpack("<HH", blob[6:10])
        return w // 16, h // 16
    if blob[:8] == b"\x89PNG\r\n\x1a\n" and blob[12:16] == b"IHDR":
        w, h = struct.unpack(">II", blob[16:24])
        return w // 16, h // 16
    return None


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
strings = {m for m in re.findall(r'Tr\("([^"]+)"\)', src)}

# 🛑 Tr() is not all of the menu text. The Options rows and the slot picker are
# built at RUNTIME with snprintf, so a Tr()-only scan misses them -- and it
# missed the ones that matter: after shortening the two 24-char Tr status lines,
# the longest string left is the slot row at 19 chars, which still overflows.
# Worst-case expansions, taken from the format strings and their real bounds
# (MREC_SLOTS = 8, musicvol <= 100, effectvol <= 120).
RUNTIME = {
    "Slot 8/8 empty": '"Slot %d/%d %s"',
    "Music: 100":     '"Music: %ld"',
    "SFX: 120":       '"SFX: %ld"',
    "FPS: Off":       '"FPS: %s"',
}
# 🛑 The worst-case expansions above are hand-derived, so they go stale the
# moment someone edits a format string -- and a stale expansion measures text
# that is not on screen, which is worse than not measuring it. Assert each
# format is still present in the source; if one changes, this fails loudly
# instead of quietly reporting a pass against the old wording.
missing = [f for f in RUNTIME.values() if f not in src]
if missing:
    sys.exit("STALE: these format strings are no longer in the menu source, so "
             "their worst-case expansions cannot be trusted:\n  "
             + "\n  ".join(missing))
strings |= set(RUNTIME)

strings = sorted(strings, key=len, reverse=True)
print(f"menu strings: {len(strings)} ({len(RUNTIME)} runtime-formatted)")
print("  longest five:")
for s in strings[:5]:
    tag = "  <- " + RUNTIME[s] if s in RUNTIME else ""
    print(f"    {len(s):>3}  {s!r}{tag}")
print()

rows = []
skipped = {}          # reason -> [pak names]
paks = sorted(glob.glob(os.path.join(PAKDIR, "*.pak")))

for pak in paks:
    name = os.path.basename(pak)[:-4]
    try:
        got = extract(pak, WANT)
    except Exception as e:
        skipped.setdefault(f"unreadable index ({type(e).__name__})", []).append(name)
        continue
    res = native_res(got.get("data/video.txt"))
    cells = {}
    for idx, stem in ((0, "font"), (1, "font2"), (3, "font4")):
        for suffix in (".gif", ".png", "/00.gif"):
            c = cell_size(got.get("data/sprites/" + stem + suffix))
            if c:
                cells[idx] = c
                break
    # 🛑 A skip must be REPORTED, never silently dropped. Counting only the
    # PAKs that happened to parse would let "393 measured" read as "all of
    # them", which is exactly the false all-clear this check exists to avoid.
    item_w = cells.get(1, cells.get(0))
    if not item_w:
        has_any_font = any(k.startswith("data/sprites/font") for k in got)
        skipped.setdefault(
            "no pause-menu font sheet in the PAK (engine default fonts)"
            if not has_any_font else "font sheet present but not a readable GIF",
            []).append(name)
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
n_skip = sum(len(v) for v in skipped.values())
print(f"{len(paks)} PAKs found | {len(rows)} measured | {n_skip} not measured\n")
print(f"  of the {len(rows)} measured: {len(fine)} fit, {len(regress)} REGRESSED "
      f"by A9, {len(pre_existing)} already broken before A9\n")
if skipped:
    print("  NOT MEASURED -- these are unproven, not passing:")
    for reason, names in sorted(skipped.items(), key=lambda kv: -len(kv[1])):
        print(f"    {len(names):>4}  {reason}")
        for n in names[:3]:
            print(f"            e.g. {n}")
    print()

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
