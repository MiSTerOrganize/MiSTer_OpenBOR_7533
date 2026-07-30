#!/usr/bin/env python3
"""Decode s_savedata (engine/source/savedata.h @ v7533) from a device .cfg and
report the three fields that select getVideoSurface's bscreen branch."""
import glob, os, struct, io

# arm32, all members 4-byte -> no padding
OFF = {
    'compatibleversion': 0, 'gamma': 4, 'brightness': 8, 'soundvol': 12,
    'usemusic': 16, 'musicvol': 20, 'effectvol': 24, 'usejoy': 28,
    'mode': 32, 'windowpos': 36,
    # keys[4][13] = 208 B @40 ; joyrumble[4] = 16 B @248
    'showtitles': 264, 'videoNTSC': 268,
    'swfilter': 272, 'logo': 276, 'uselog': 280, 'debuginfo': 284,
    'fullscreen': 288, 'stretch': 292,
    # screen[1][2] = 8 B @296
    'vsync': 304, 'fpslimit': 308,
    'usegl': 312, 'hwscale': 316, 'hwfilter': 320,
}
SIZE = 324

for p in sorted(glob.glob(os.path.join(os.path.dirname(__file__), 'cfg', '*.cfg'))):
    d = io.open(p, 'rb').read()
    name = os.path.basename(p)
    print("== %s  (%d bytes)" % (name, len(d)))
    if len(d) != SIZE:
        print("   size != %d -> different build era, offsets not valid; SKIPPED\n" % SIZE)
        continue
    g = lambda k: struct.unpack_from('<i', d, OFF[k])[0]
    hw = struct.unpack_from('<f', d, OFF['hwscale'])[0]
    print("   compatibleversion = %d" % struct.unpack_from('<I', d, 0)[0])
    print("   swfilter   = %d   (default 0)" % g('swfilter'))
    print("   fullscreen = %d   (default 0)" % g('fullscreen'))
    print("   hwscale    = %.4f (default 1.0)" % hw)
    print("   usegl=%d hwfilter=%d vsync=%d videoNTSC=%d" %
          (g('usegl'), g('hwfilter'), g('vsync'), g('videoNTSC')))
    bscreen_live = bool(g('swfilter')) and (hw >= 2.0 or bool(g('fullscreen')))
    print("   -> swfilter && (hwscale>=2.0 || fullscreen) = %s" % bscreen_live)
    print("   -> getVideoSurface returns %s\n" %
          ("screen->pixels  (bscreen LIVE -- D2 chain BROKEN)" if bscreen_live
           else "vscreen->data   (bscreen NULL -- D2 chain holds)"))
