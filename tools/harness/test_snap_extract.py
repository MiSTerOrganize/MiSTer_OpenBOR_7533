#!/usr/bin/env python3
"""test_snap_extract.py -- exercise the REAL --extract-snap parser, natively.

WHAT THIS TESTS AND WHY IT CAN BE TRUSTED
-----------------------------------------
mrec_extract_snap() turns bytes written by a stranger into filenames on the
recipient's root filesystem. Design note O1 accepts that a shared take can carry
re-executable engine script (.sNN), which makes the extension whitelist the
load-bearing control -- not a nicety. A control like that should be machine-
checked forever, not hand-tested once.

So this does NOT reimplement the parser and test the reimplementation. It:

  1. runs apply_patches.py into a pristine v7533 clone,
  2. CUTS the whole contiguous extractor group -- mrec_snap_is_script_save(),
     mrec_snap_ext_ok(), mrec_extract_snap() -- out of the EMITTED
     sdl/sdlport.c by text markers,
  3. compiles them natively (x86, no ARM, no QEMU, no SDL) with a test main(),
  4. drives that binary with takes built by mrec_synth.py.

Point 2 is what makes it meaningful: a test against a copy of the logic would
pass while the shipped code rotted. Those two functions are self-contained --
stdio, string, sys/stat -- which is what makes the cut possible.

This closes a real gap. The extractor has never run against a real take, and
§12's round trip (record, rename the content, replay) needs no hardware at all:
it is entirely a question of what the parser accepts and refuses.

    python3 test_snap_extract.py [--keep]
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, HERE)
import mrec_synth  # noqa: E402

UPSTREAM = "https://github.com/DCurrent/openbor.git"
NATIVE_COPIES = ["native_video_writer.c", "native_video_writer.h",
                 "native_audio_writer.c", "native_audio_writer.h",
                 "native_sha1.h"]

TEST_MAIN = r"""
/* Test driver: calls the extractor exactly as main() does.
 *
 * Exit codes are DISTINCT on purpose. This driver returns 2 for a usage error,
 * while the extractor returns 0 (accepted) or 1 (refused). The first version
 * used `argc < 4`, so every no-stem call bailed with 2 before reaching the
 * parser -- and because the refusal checks only asserted "rc != 0", eleven of
 * them passed while testing nothing at all. Refusal checks now demand rc == 1
 * specifically, so a driver error can never again read as a refusal.
 */
int main(int argc, char **argv)
{
    if (argc < 3) { fprintf(stderr, "usage: %s <inp> <destdir> [stem]\n", argv[0]); return 2; }
    return mrec_extract_snap(argv[1], argv[2], argc >= 4 ? argv[3] : NULL);
}
"""

PROLOGUE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <sys/types.h>
/* dirent.h is needed because the cut function walks a directory (DIR /
 * struct dirent / opendir). It is absent from the shipped file's own include
 * list only because that file gets it transitively; the probe does not, so
 * omitting it here fails the compile with "unknown type name 'DIR'" and takes
 * the whole gate red -- which is exactly what happened. Same class as the
 * missing-include build failures the guide records: new C in a cut or injected
 * fragment must carry its OWN includes, never inherit them. */
#include <dirent.h>
"""

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  %-58s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  -- " + detail) if detail and not cond else ""))


def build_tree(work):
    tree = os.path.join(work, "ob")
    subprocess.run(["git", "clone", "--depth", "1", "--branch", "v7533",
                    "--filter=blob:none", "--quiet", UPSTREAM, tree], check=True)
    eng = os.path.join(tree, "engine")
    # EXACTLY the files build_mister_arm.sh copies -- a glob here would be more
    # permissive than CI, which is how a missing native_sha1.h once dry-ran clean
    # and then failed the build.
    for fn in NATIVE_COPIES:
        shutil.copy(os.path.join(REPO, "src", fn), eng)
    hdr = os.path.join(eng, "openbor.h")
    s = open(hdr, encoding="utf-8", errors="replace").read().replace("stricmp", "strcasecmp")
    open(hdr, "w", encoding="utf-8", newline="\n").write(s)
    r = subprocess.run([sys.executable,
                        os.path.join(REPO, ".github", "scripts", "apply_patches.py"),
                        eng, os.path.join(REPO, "patches")],
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    if r.returncode != 0:
        print(r.stdout[-3000:]); print(r.stderr[-3000:])
        raise SystemExit("apply_patches.py failed")
    return eng


def cut_functions(eng):
    """Lift the WHOLE contiguous extractor group out of the emitted sdlport.c.

    🛑 The anchor is the FIRST function in the group, not the one the test is
    nominally about. It used to start at mrec_snap_ext_ok, which silently
    excluded mrec_snap_is_script_save sitting immediately above it -- the cut
    compiled and then failed to LINK with an implicit-declaration warning as
    the only clue. Anyone adding another helper above this anchor must move it
    up, or the same link error returns.
    """
    src = open(os.path.join(eng, "sdl", "sdlport.c"),
               encoding="utf-8", errors="replace").read()
    start = src.find("static int mrec_snap_is_script_save")
    if start < 0:
        raise SystemExit("mrec_snap_is_script_save not in the emitted C -- did "
                         "the sdlport_patch.c splice marker break again, or did "
                         "a new helper move above the cut anchor?")
    end = src.find("\n#endif", src.find("static int mrec_extract_snap"))
    if end < 0:
        raise SystemExit("could not find the end of mrec_extract_snap")
    return src[start:end]


def compile_probe(work, body):
    csrc = os.path.join(work, "probe.c")
    open(csrc, "w", encoding="utf-8", newline="\n").write(PROLOGUE + body + TEST_MAIN)
    exe = os.path.join(work, "probe")
    r = subprocess.run(["gcc", "-O1", "-g", "-o", exe, csrc],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-4000:])
        raise SystemExit("the extracted extractor did not compile")
    return exe


def run(exe, inp, dest, stem=None):
    os.makedirs(dest, exist_ok=True)
    args = [exe, inp, dest] + ([stem] if stem else [])
    r = subprocess.run(args, capture_output=True, text=True)
    landed = sorted(os.listdir(dest)) if os.path.isdir(dest) else []
    return r.returncode, r.stdout + r.stderr, landed


def main():
    keep = "--keep" in sys.argv
    work = tempfile.mkdtemp(prefix="snapx_")
    print("workdir:", work)
    try:
        eng = build_tree(work)
        exe = compile_probe(work, cut_functions(eng))
        print("extractor compiled natively from the emitted C\n")

        def take(name, **kw):
            p = os.path.join(work, name)
            mrec_synth.write_take(p, **kw)
            return p

        # ---- the allowed set -------------------------------------------------
        good = take("good.inp", pak="TestPak", frames=4, payload=[
            ("TestPak.sav", b"SAVE"), ("TestPak.hi", b"HI"),
            ("TestPak.s00", b"SET0"), ("TestPak.s07", b"SET7"),
        ])
        rc, out, landed = run(exe, good, os.path.join(work, "d_good"))
        check("valid take: accepted", rc == 0, out.strip())
        check("valid take: all 4 allowed extensions land", len(landed) == 4, str(landed))
        # .sNN must accept ANY two digits: saveScriptFile writes one file per
        # level set, so matching only .s00 silently drops every later set.
        check("valid take: .s07 kept, not just .s00",
              any(x.endswith(".s07") for x in landed), str(landed))

        # ---- stem remapping (the point of carrying the recorder's stem) -------
        rc, out, landed = run(exe, good, os.path.join(work, "d_stem"), "LocalPak")
        check("remap: every entry renamed to the local stem",
              landed and all(x.startswith("LocalPak.") for x in landed), str(landed))
        check("remap: extension preserved exactly",
              sorted(x.split(".", 1)[1] for x in landed) == ["hi", "s00", "s07", "sav"],
              str(landed))

        # ---- the whitelist: this is the security control ---------------------
        for label, entry in [
            ("path traversal ../", "../escape.sav"),
            ("absolute path", "/etc/passwd.sav"),
            ("backslash separator", "..\\escape.sav"),
            ("dot", "."),
            ("dotdot", ".."),
            ("ROM overlay .p8", "TestPak.p8"),
            ("shell script .sh", "TestPak.sh"),
            ("no extension", "TestPak"),
            ("engine script .scr", "TestPak.scr"),
            ("three-digit .s000", "TestPak.s000"),
            ("non-digit .sXX", "TestPak.sXX"),
        ]:
            p = take("bad.inp", pak="TestPak", frames=2, payload=[(entry, b"X")])
            d = os.path.join(work, "d_bad")
            shutil.rmtree(d, ignore_errors=True)
            rc, out, landed = run(exe, p, d)
            check("refuse: %s" % label, rc == 1 and not landed,
                  "rc=%d landed=%s" % (rc, landed))

        # A rejected entry must refuse the WHOLE payload, not skip and continue:
        # a partial restore is a desync dressed as a warning.
        p = take("mixed.inp", pak="TestPak", frames=2, payload=[
            ("TestPak.sav", b"GOOD"), ("../evil.sav", b"BAD")])
        d = os.path.join(work, "d_mixed")
        shutil.rmtree(d, ignore_errors=True)
        rc, out, landed = run(exe, p, d)
        check("refuse: one bad entry rejects the ENTIRE payload",
              rc == 1 and not landed, "landed=%s" % landed)

        # ---- container guards ------------------------------------------------
        p = take("newer.inp", pak="TestPak", frames=2, container=3)
        rc, out, _ = run(exe, p, os.path.join(work, "d_new"))
        check("refuse: newer container", rc == 1, out.strip())

        p = take("older.inp", pak="TestPak", frames=2, container=1)
        rc, out, _ = run(exe, p, os.path.join(work, "d_old"))
        check("refuse: older container", rc == 1, out.strip())

        p = take("magic.inp", pak="TestPak", frames=2, bad_magic=True)
        rc, out, _ = run(exe, p, os.path.join(work, "d_magic"))
        check("refuse: bad magic", rc == 1, out.strip())

        # A ~290-byte header claiming two million frames allocated 144 MB before
        # the read failed -- on a 1 GB board shared with the FPGA.
        p = take("huge.inp", pak="TestPak", frames=1, claim_frames=mrec_synth.MAX_FRAMES)
        rc, out, _ = run(exe, p, os.path.join(work, "d_huge"))
        check("refuse: frame count larger than the file", rc == 1, out.strip())

        p = take("trunc.inp", pak="TestPak", frames=8,
                 payload=[("TestPak.sav", b"X" * 64)], truncate_after=340)
        rc, out, _ = run(exe, p, os.path.join(work, "d_trunc"))
        check("refuse: truncated mid-file", rc == 1, out.strip())

        # ---- absent payload is not corruption --------------------------------
        p = take("nopay.inp", pak="TestPak", frames=4, payload=[])
        rc, out, landed = run(exe, p, os.path.join(work, "d_nopay"))
        check("empty payload: accepted, nothing written",
              rc == 0 and not landed, "rc=%d landed=%s" % (rc, landed))

        # ---- identity is carried, and a take may decline to vouch ------------
        p = take("noid.inp", pak="TestPak", frames=4, content_hash=None,
                 payload=[("TestPak.sav", b"S")])
        rc, out, landed = run(exe, p, os.path.join(work, "d_noid"))
        check("unverifiable take (0 identity entries) still extracts",
              rc == 0 and len(landed) == 1, "rc=%d landed=%s" % (rc, landed))

    finally:
        if keep:
            print("\nkept:", work)
        else:
            shutil.rmtree(work, ignore_errors=True)

    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    if bad:
        print("FAILED:")
        for n in bad:
            print("  -", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
