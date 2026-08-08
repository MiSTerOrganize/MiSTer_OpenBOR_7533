"""How big is a PAK's TITLE-screen text once we squeeze it into 320x224?

The pause-menu check (a9_font_check.py) asks "does OUR text clip at display
width". This asks a different question, about text we do NOT author: the PAK
draws its own title menu with its own font at its own native resolution, and
WriteFrame then downscales the whole frame. So the text cannot clip -- it fits
the PAK's own canvas by construction -- but it CAN end up too few pixels tall
to read.

Reuses a9_font_check's PAK reader. Font cell = sheet/16 in both axes (font.c:
font->width = screen->width / 16).

🛑 The title menu uses font index 0 (data/sprites/font), not the pause menu's
pauseoffset set. Reporting font2/font4 here would measure the wrong glyphs.

Usage:  python title_text_measure.py <pakdir> [name-substring ...]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from a9_font_check import cell_size, extract, native_res   # noqa: E402

DISP_W, DISP_H = 320, 224

WANT = ("data/video.txt",
        "data/sprites/font.gif", "data/sprites/font.png", "data/sprites/font/00.gif")


def measure(pak):
    blobs = extract(pak, WANT)
    res = native_res(blobs.get("data/video.txt")) or (320, 240)
    sheet = (blobs.get("data/sprites/font.gif")
             or blobs.get("data/sprites/font.png")
             or blobs.get("data/sprites/font/00.gif"))
    cell = cell_size(sheet)
    if not cell:
        return None
    cw, ch = cell
    nw, nh = res
    # WriteFrame maps the whole frame onto 320x224, each axis independently.
    fx, fy = DISP_W / nw, DISP_H / nh
    return {
        "native": (nw, nh),
        "cell": (cw, ch),
        "scale": (fx, fy),
        "shown": (cw * fx, ch * fy),
        # >1 means glyphs are squeezed harder horizontally than vertically,
        # which is what reads as "squished" rather than merely "small".
        "aspect_err": fy / fx if fx else 0.0,
    }


def main():
    pakdir = sys.argv[1]
    picks = [a.lower() for a in sys.argv[2:]]
    rows = []
    for f in sorted(os.listdir(pakdir)):
        if not f.lower().endswith(".pak"):
            continue
        if picks and not any(p in f.lower() for p in picks):
            continue
        try:
            m = measure(os.path.join(pakdir, f))
        except Exception as e:                      # a corrupt PAK is data, not a crash
            print("%-52s  UNREADABLE (%s)" % (f[:52], e))
            continue
        if not m:
            print("%-52s  no font sheet" % f[:52])
            continue
        rows.append((f, m))

    print("%-44s %10s %8s %14s %8s" %
          ("PAK", "native", "cell", "shown (px)", "squish"))
    print("-" * 92)
    for f, m in rows:
        print("%-44s %5dx%-4d %3dx%-3d  %5.1fx%-5.1f  %5.2fx" % (
            f[:44].replace(".pak", ""),
            m["native"][0], m["native"][1],
            m["cell"][0], m["cell"][1],
            m["shown"][0], m["shown"][1],
            m["aspect_err"]))

    if len(rows) > 1:
        # The WORST axis decides legibility. Counting height alone excluded
        # He-Man (6.5 tall, 4.7 wide) -- the PAK this tool was built for.
        worst = lambda m: min(m["shown"])
        distort = lambda m: max(m["aspect_err"], 1.0 / m["aspect_err"]) if m["aspect_err"] else 99

        under5 = [r for r in rows if worst(r[1]) < 5]
        under6 = [r for r in rows if worst(r[1]) < 6]
        sq20 = [r for r in rows if distort(r[1]) > 1.2]

        print("-" * 92)
        print("%d measured" % len(rows))
        print("  glyphs under 5px on their narrow axis: %3d  (%.0f%%)  -- hard to read"
              % (len(under5), 100.0 * len(under5) / len(rows)))
        print("  glyphs under 6px on their narrow axis: %3d  (%.0f%%)"
              % (len(under6), 100.0 * len(under6) / len(rows)))
        print("  aspect distorted by more than 20%%:     %3d  (%.0f%%)"
              % (len(sq20), 100.0 * len(sq20) / len(rows)))

        print("\n  worst 10 by narrow axis:")
        for f, m in sorted(rows, key=lambda r: worst(r[1]))[:10]:
            print("    %-44s %5.1fx%-5.1f  native %dx%d"
                  % (f[:44].replace(".pak", ""), m["shown"][0], m["shown"][1],
                     m["native"][0], m["native"][1]))


if __name__ == "__main__":
    main()
