#!/usr/bin/env python3
"""
pak_blendscan.py -- static blend-mode census of an OpenBOR PAK library.

Answers "which blend modes does the RTL have to support so that no PAK silently
falls back?" across ALL PAKs, with no device and no engine run. This is Tier-B
Phase-1b LAYER 1; see the limitations at the bottom -- it is a LOWER BOUND.

drawmethod->alpha is a blend MODE INDEX, not an alpha level (openbor.c:48692 /
pixelformat.c:629):
    0 = opaque (no blend fp)   1 = SCREEN   2 = MULTIPLY
    3 = OVERLAY                4 = HARDLIGHT 5 = DODGE     6 = HALF
Two globals override it: tintmode > 0 -> blend_tint16 (composes two modes);
usechannel (any of channelr/g/b < 255) turns mode 6 into blend_rgbchannel16.

Cart-authored sources of the mode, all parsed here (openbor.c):
  1. `alpha N`                                 CMD_MODEL_ALPHA        :14556
  2. `drawmethod alpha N`                      named  sub-directive   :15737
  3. `drawmethod sx sy fx fy shx ALPHA rm ...` positional, arg 6      :15645
  4. `changedrawmethod(ent, "alpha", N)`       script bridge          :14400
  5. `drawmethod tintmode N` / `tintcolor`     tint override
  6. `channelr/g/b N`                          usechannel override

Reads ONLY each PAK's packfile directory plus its .txt/.c entries -- bounded,
a few MB per PAK. Run LOCALLY, never on the MiSTer.

Usage:  python pak_blendscan.py <paks_dir> [-o out.tsv]
"""

import os, re, sys, struct, argparse, collections

MODE_NAME = {0: "opaque", 1: "SCREEN", 2: "MULTIPLY", 3: "OVERLAY",
             4: "HARDLIGHT", 5: "DODGE", 6: "HALF"}

# `alpha N` as a standalone directive (arg 0 == "alpha"). '#' starts a comment.
RE_MODEL_ALPHA = re.compile(r'^[ \t]*alpha[ \t]+(-?\d+)', re.I | re.M)
# `drawmethod alpha N`
RE_DM_NAMED = re.compile(r'^[ \t]*drawmethod[ \t]+alpha[ \t]+(-?\d+)', re.I | re.M)
# positional `drawmethod` -- alpha is the 6th argument after the keyword
RE_DM_POS = re.compile(r'^[ \t]*drawmethod[ \t]+(-?\d+(?:[ \t]+-?\d+){5,})', re.I | re.M)
# script: changedrawmethod(<ent>, "alpha", <value>)
RE_SCRIPT = re.compile(r'changedrawmethod\s*\([^,]+,\s*"alpha"\s*,\s*([^)\s,]+)', re.I)
RE_TINT = re.compile(r'^[ \t]*(?:drawmethod[ \t]+)?tintmode[ \t]+(-?\d+)', re.I | re.M)
RE_CHAN = re.compile(r'^[ \t]*(?:drawmethod[ \t]+)?channel[rgb][ \t]+(-?\d+)', re.I | re.M)


def strip_comments(text):
    """ParseArgs ends a line at '#'. Mirror that before matching."""
    return "\n".join(ln.split("#", 1)[0] for ln in text.splitlines())


def iter_text_members(path, exts=(".txt", ".c")):
    """Yield (name, bytes) for each .txt/.c member. Directory-only + ranged reads."""
    size = os.path.getsize(path)
    if size < 8:
        return
    with open(path, "rb") as f:
        f.seek(-4, os.SEEK_END)
        dir_off = struct.unpack("<I", f.read(4))[0]
        if dir_off >= size:
            return
        f.seek(dir_off)
        directory = f.read(size - dir_off - 4)
        pos = 0
        while pos + 12 <= len(directory):
            pns_len, start, length = struct.unpack_from("<III", directory, pos)
            if pns_len < 12 or pos + pns_len > len(directory):
                break
            raw = directory[pos + 12: pos + pns_len]
            name = raw.split(b"\0", 1)[0].decode("latin-1").replace("\\", "/")
            pos += pns_len
            if not name.lower().endswith(exts):
                continue
            if length <= 0 or start + length > size or length > 8 * 1024 * 1024:
                continue
            f.seek(start)
            yield name, f.read(length)


def scan_pak(path):
    modes = collections.Counter()
    dynamic = 0          # script-set with a non-literal value
    tint = 0
    chan = 0
    for _name, blob in iter_text_members(path):
        text = strip_comments(blob.decode("latin-1"))
        for m in RE_MODEL_ALPHA.finditer(text):
            modes[int(m.group(1))] += 1
        for m in RE_DM_NAMED.finditer(text):
            modes[int(m.group(1))] += 1
        for m in RE_DM_POS.finditer(text):
            args = m.group(1).split()
            if len(args) >= 6:
                modes[int(args[5])] += 1
        for m in RE_SCRIPT.finditer(text):
            v = m.group(1).strip()
            if re.fullmatch(r"-?\d+", v):
                modes[int(v)] += 1
            else:
                dynamic += 1
        tint += sum(1 for m in RE_TINT.finditer(text) if int(m.group(1)) > 0)
        chan += sum(1 for m in RE_CHAN.finditer(text) if int(m.group(1)) < 255)
    return modes, dynamic, tint, chan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paks_dir")
    ap.add_argument("-o", "--out", default="pak_blendscan.tsv")
    a = ap.parse_args()

    paks = sorted(p for p in os.listdir(a.paks_dir) if p.lower().endswith(".pak"))
    rows = []
    lib_modes = collections.Counter()     # total occurrences
    lib_paks = collections.Counter()      # how many PAKs use each mode
    dyn_paks = tint_paks = chan_paks = 0

    for i, pak in enumerate(paks, 1):
        try:
            modes, dyn, tint, chan = scan_pak(os.path.join(a.paks_dir, pak))
        except Exception as e:
            rows.append((pak[:-4], "ERROR", str(e), 0, 0, 0))
            continue
        blended = {k: v for k, v in modes.items() if k != 0}
        for k, v in modes.items():
            lib_modes[k] += v
            lib_paks[k] += 1
        if dyn:
            dyn_paks += 1
        if tint:
            tint_paks += 1
        if chan:
            chan_paks += 1
        rows.append((pak[:-4],
                     ",".join("%d:%d" % (k, blended[k]) for k in sorted(blended)) or "-",
                     modes.get(0, 0), dyn, tint, chan))
        if i % 50 == 0:
            print("  ...%d/%d" % (i, len(paks)), file=sys.stderr)

    with open(a.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("pak\tblend_modes(mode:count)\topaque_decls\tscript_dynamic\ttint\tchannel\n")
        for r in rows:
            f.write("%s\t%s\t%s\t%s\t%s\t%s\n" % r)

    print("\n%d PAKs scanned -> %s\n" % (len(rows), a.out))
    print("%-10s %-6s %10s %10s" % ("mode", "idx", "PAKs", "decls"))
    for k in sorted(lib_modes):
        print("%-10s %-6d %10d %10d" % (MODE_NAME.get(k, "OUT-OF-RANGE"), k,
                                        lib_paks[k], lib_modes[k]))
    print("\nPAKs with a script-DYNAMIC alpha (value not a literal): %d" % dyn_paks)
    print("PAKs declaring tintmode > 0                            : %d" % tint_paks)
    print("PAKs declaring a channel r/g/b < 255                   : %d" % chan_paks)
    print("\nLIMITATION: this is a LOWER BOUND. Script-computed values cannot be")
    print("resolved statically, and a declared mode is not proof it is ever drawn.")
    print("Pair with the runtime [TBB] histogram for weighting.")


if __name__ == "__main__":
    main()
