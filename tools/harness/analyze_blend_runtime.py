#!/usr/bin/env python3
"""Analyse blend_runtime.tsv from pak_blend_runtime_scan.sh (Tier-B Phase 1b layer 2).

Input rows:  <pak> \t <exitcode> \t [TBB] fast=N slow=N m0..m7=N p0..p7=N tint=N chan=N remap=N frames=N

Buckets (mirroring getblendfunction16, pixelformat.c:629):
  m0 = alpha <= 0  -> opaque (fp NULL)      m1..m6 = SCREEN/MULTIPLY/OVERLAY/HARDLIGHT/DODGE/HALF
  m7 = alpha > 6   -> indexes past blendfunctions16[6]
"""
import io, re, sys, collections

NAME = {0: "opaque", 1: "SCREEN", 2: "MULTIPLY", 3: "OVERLAY",
        4: "HARDLIGHT", 5: "DODGE", 6: "HALF", 7: "alpha>6"}
path = sys.argv[1] if len(sys.argv) > 1 else "blend_runtime.tsv"
rows = [l.split("\t") for l in io.open(path, encoding="utf-8", errors="replace").read().splitlines() if l.strip()]

kv = re.compile(r"(\w+)=(\d+)")
ok, notbb, byec = [], [], collections.Counter()
for r in rows:
    pak, ec, tbb = (r + ["", ""])[:3]
    byec[ec] += 1
    if not tbb.startswith("[TBB]"):
        notbb.append((pak, ec))
        continue
    d = {k: int(v) for k, v in kv.findall(tbb)}
    ok.append((pak, ec, d))

print("=" * 78)
print("TIER-B PHASE 1b LAYER 2 -- runtime blend census, %d PAKs" % len(rows))
print("=" * 78)
print("\nexit codes:", ", ".join("%s:%d" % (e, n) for e, n in byec.most_common()))
print("rows with a usable [TBB] line : %d" % len(ok))
print("rows without one              : %d" % len(notbb))

drew = [x for x in ok if x[2].get("fast", 0) + x[2].get("slow", 0) > 0]
print("PAKs that actually DREW a sprite: %d  (the rest never left a static screen)" % len(drew))

tot_c = collections.Counter()
tot_p = collections.Counter()
paks_c = collections.Counter()
paks_blend = 0
tint = chan = 0
fast = slow = 0
for _pak, _ec, d in drew:
    fast += d.get("fast", 0); slow += d.get("slow", 0)
    if d.get("tint", 0): tint += 1
    if d.get("chan", 0): chan += 1
    any_blend = False
    for i in range(8):
        c = d.get("m%d" % i, 0); p = d.get("p%d" % i, 0)
        tot_c[i] += c; tot_p[i] += p
        if c:
            paks_c[i] += 1
            if 1 <= i <= 6:
                any_blend = True
    if any_blend:
        paks_blend += 1

sc, sp = sum(tot_c.values()) or 1, sum(tot_p.values()) or 1
print("\nblits: fast(offloadable)=%d  slow(CPU fallback)=%d  -> fast %.1f%%"
      % (fast, slow, 100.0 * fast / max(fast + slow, 1)))
print("\n%-10s %6s %10s %7s %14s %7s" % ("mode", "PAKs", "calls", "%calls", "pixels", "%px"))
print("-" * 62)
for i in range(8):
    if not tot_c[i] and not tot_p[i]:
        continue
    print("%-10s %6d %10d %6.1f%% %14d %6.1f%%"
          % (NAME[i], paks_c[i], tot_c[i], 100.0 * tot_c[i] / sc, tot_p[i], 100.0 * tot_p[i] / sp))
print("-" * 62)
print("PAKs drawing >=1 real blend (1..6): %d of %d that drew" % (paks_blend, len(drew)))
print("PAKs firing the tint override     : %d   (tint COMPOSES two modes)" % tint)
print("PAKs firing rgbchannel (mode 6)   : %d" % chan)

print("\n--- per-mode: which PAKs use it most (by pixels) ---")
for i in range(1, 8):
    top = sorted(((d.get("p%d" % i, 0), p) for p, _e, d in drew if d.get("m%d" % i, 0)),
                 reverse=True)[:5]
    if not top:
        print("  %-10s (none)" % NAME[i]); continue
    print("  %-10s %s" % (NAME[i], ", ".join("%s (%.0fMpx)" % (n[:26], px / 1e6) for px, n in top)))

if notbb:
    print("\n--- no [TBB] line (%d) ---" % len(notbb))
    for pak, ec in notbb[:25]:
        print("   ec=%-4s %s" % (ec, pak))
    if len(notbb) > 25:
        print("   ... +%d more" % (len(notbb) - 25))
