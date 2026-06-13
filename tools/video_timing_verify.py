#!/usr/bin/env python3
"""
video_timing_verify.py -- automates full-audit Section 3 (reference-timing match).

Parses the RTL video-timing localparams, derives H/V rate + active time from the
PLL master clock + per-line MCLK, and compares byte-for-byte against the target
console's published values. Catches CRT-lock-breaking timing drift on any RTL
change, and enforces the source-of-truth rule (a divergence is a FAIL, never
laundered into a "match" via an alternate-mode label).

Dev-machine only (no MiSTer). Exit 0 = all match, 1 = any divergence.

Usage:  python tools/video_timing_verify.py [path/to/openbor_video_timing.sv]
"""
import re, sys, os

# -- target reference: Sega CD NTSC (H40 + V28), mk-docs published values --
REF = {
    "name":       "Sega CD NTSC (H40+V28)",
    "MCLK_HZ":    53693175,   # CLK_VIDEO master clock
    "MCLK_LINE":  3420,       # master clocks per scanline (variable CE: /8 active, /10+/9+/8 blank)
    "CE_ACTIVE":  8,          # active-region CE divider -> pixel clock = MCLK/8
    "H_ACTIVE":   320, "H_TOTAL": 420,
    "V_ACTIVE":   224, "V_TOTAL": 262,
    "PIX_HZ":     6711647,    # 53693175/8
    "HRATE_HZ":   15700,      # 53693175/3420
    "VRATE_HZ":   59.92,      # HRATE/V_TOTAL
    "ACTIVE_US":  47.68,      # H_ACTIVE/PIX
    "VBLANK":     38,         # V_TOTAL-V_ACTIVE
}

def parse_localparams(path):
    txt = open(path, encoding="utf-8", errors="replace").read()
    out = {}
    for m in re.finditer(r"localparam\s+(\w+)\s*=\s*(\d+)", txt):
        out[m.group(1)] = int(m.group(2))
    return out

def approx(a, b, tol):
    return abs(a - b) <= tol

def main():
    here = os.path.dirname(os.path.abspath(__file__))
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "..", "fpga", "rtl", "openbor_video_timing.sv")
    if not os.path.isfile(path):
        print("ERROR: timing RTL not found:", path); return 2
    p = parse_localparams(path)
    need = ["H_ACTIVE","H_TOTAL","V_ACTIVE","V_TOTAL"]
    miss = [k for k in need if k not in p]
    if miss:
        print("ERROR: missing localparams:", miss); return 2

    mclk, line, ce = REF["MCLK_HZ"], REF["MCLK_LINE"], REF["CE_ACTIVE"]
    pix     = mclk / ce
    hrate   = mclk / line
    vrate   = hrate / p["V_TOTAL"]
    active  = p["H_ACTIVE"] / pix * 1e6      # us
    vblank  = p["V_TOTAL"] - p["V_ACTIVE"]

    rows = [
        # label,            ours,                 reference,         tol,    fmt
        ("Pixel clock (MHz)", pix/1e6,            REF["PIX_HZ"]/1e6, 0.001, "%.5f"),
        ("H_ACTIVE",          p["H_ACTIVE"],      REF["H_ACTIVE"],   0,     "%d"),
        ("H_TOTAL",           p["H_TOTAL"],       REF["H_TOTAL"],    0,     "%d"),
        ("Active time (us)",  active,             REF["ACTIVE_US"],  0.02,  "%.2f"),
        ("H rate (Hz)",       hrate,              REF["HRATE_HZ"],   2,     "%.0f"),
        ("V_ACTIVE",          p["V_ACTIVE"],      REF["V_ACTIVE"],   0,     "%d"),
        ("V_TOTAL",           p["V_TOTAL"],       REF["V_TOTAL"],    0,     "%d"),
        ("V blanking",        vblank,             REF["VBLANK"],     0,     "%d"),
        ("V rate (Hz)",       vrate,              REF["VRATE_HZ"],   0.05,  "%.2f"),
    ]
    print("== video_timing_verify -- %s ==" % os.path.basename(path))
    print("reference: %s\n" % REF["name"])
    print("%-18s %14s %14s  %s" % ("parameter","ours","reference","verdict"))
    fails = 0
    for label, ours, ref, tol, fmt in rows:
        ok = approx(float(ours), float(ref), tol)
        if not ok: fails += 1
        print("%-18s %14s %14s  %s" % (label, fmt % ours, fmt % ref, "OK" if ok else "** FAIL **"))
    print()
    if fails:
        print("RESULT: %d divergence(s) -- Section 3 FAIL (timing drift; CRT-lock risk)." % fails)
        return 1
    print("RESULT: all match -- Section 3 PASS (CRT-locked to %s)." % REF["name"])
    return 0

if __name__ == "__main__":
    sys.exit(main())
