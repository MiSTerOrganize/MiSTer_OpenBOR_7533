#!/usr/bin/env python3
"""test_snap_extract.py -- exercise the REAL --extract-snap parser, natively.

WHAT THIS TESTS AND WHY IT CAN BE TRUSTED
-----------------------------------------
mrec_extract_snap() turns bytes written by a stranger into filenames on the
recipient's root filesystem, and one allowed kind (.sNN) is re-executable engine
script that loadScriptFile COMPILES AND EXECUTES. TWO controls make that safe --
the extension whitelist decides what may land, and mrec_snap_script_ok decides
whether a program may run -- and neither substitutes for the other. Controls like
that should be machine-checked forever, not hand-tested once.

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
 * Exit codes are DISTINCT on purpose:
 *
 *   0  accepted          2  THIS DRIVER's usage error
 *   1  payload refused   3  not a container-v2 take (no payload can exist)
 *
 * The first version used `argc < 4`, so every no-stem call bailed with 2 before
 * reaching the parser -- and because the refusal checks only asserted "rc != 0",
 * eleven of them passed while testing nothing at all. Every check now demands a
 * SPECIFIC code, so neither a driver error nor the wrong refusal can pass as
 * the right one. 3 exists because "there cannot be a payload" and "the payload
 * was refused" are different answers, and the handler tells the user which.
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

# A script-save is the one payload entry whose CONTENT is parsed (it is a
# PROGRAM the engine compiles and executes), so every .sNN used here has to be
# real script or the extension cases below start failing for the wrong reason.
# The grammar itself is exercised by test_snap_script.py; this file only cares
# that the extractor CONSULTS it. The byte cap is asserted against the emitted
# #define at run time (see check_cap): a re-typed constant that drifts would
# leave the over-cap case UNDER the real cap, silently testing nothing.
SCRIPT_OK = b'void main() {\n    setglobalvar("unlocked_raph", 1);\n}\n'
SCRIPT_BAD = b'void main() {\n    savefilestream("/media/fat/linux/user-startup.sh", 0);\n}\n'
SCRIPT_MAX_BYTES = 65536

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


def check_cap(eng):
    """The reader's byte cap, read from the source rather than trusted."""
    src = open(os.path.join(eng, "sdl", "sdlport.c"),
               encoding="utf-8", errors="replace").read()
    m = re.search(r"#define\s+MREC_SCRIPT_MAX_BYTES\s+(\d+)", src)
    if not m:
        raise SystemExit("MREC_SCRIPT_MAX_BYTES not in the emitted C")
    if int(m.group(1)) != SCRIPT_MAX_BYTES:
        raise SystemExit("this suite's SCRIPT_MAX_BYTES (%d) no longer matches the "
                         "shipped cap (%s) -- the over-cap case would be UNDER it "
                         "and test nothing" % (SCRIPT_MAX_BYTES, m.group(1)))


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
    """destdir is the scratch ROOT. The extractor routes .sav/.hi into
    <dest>/saves and script-saves into <dest>/savestates, and deliberately does
    NOT create either -- _handler.sh:106 makes them on every launch, so the
    running core always has them.

    🛑 The driver has to reproduce that precondition. Without it every accept
    check failed with "cannot write .../saves/TestPak.sav" while the refusal
    checks sailed through, so the suite still ran and still reported 18/24 --
    a shipped-code verdict from an environment production never has.

    Files are collected by WALKING, for the same reason: listing only the top
    of dest saw an empty directory once routing landed on 2026-08-07, and the
    assertions below match on basenames.
    """
    for leaf in ("saves", "savestates"):
        os.makedirs(os.path.join(dest, leaf), exist_ok=True)
    args = [exe, inp, dest] + ([stem] if stem else [])
    r = subprocess.run(args, capture_output=True, text=True)
    landed = sorted(f for _, _, fs in os.walk(dest) for f in fs)
    return r.returncode, r.stdout + r.stderr, landed


def leaf_of(dest, basename):
    """Which scratch leaf a file actually landed in ('' if it is missing)."""
    for leaf in ("saves", "savestates"):
        if os.path.exists(os.path.join(dest, leaf, basename)):
            return leaf
    return ""


def main():
    keep = "--keep" in sys.argv
    work = tempfile.mkdtemp(prefix="snapx_")
    print("workdir:", work)
    try:
        eng = build_tree(work)
        check_cap(eng)
        exe = compile_probe(work, cut_functions(eng))
        print("extractor compiled natively from the emitted C\n")

        def take(name, **kw):
            p = os.path.join(work, name)
            mrec_synth.write_take(p, **kw)
            return p

        # ---- the allowed set -------------------------------------------------
        good = take("good.inp", pak="TestPak", frames=4, payload=[
            ("TestPak.sav", b"SAVE"), ("TestPak.hi", b"HI"),
            ("TestPak.s00", SCRIPT_OK), ("TestPak.s07", SCRIPT_OK),
        ])
        d_good = os.path.join(work, "d_good")
        rc, out, landed = run(exe, good, d_good)
        check("valid take: accepted", rc == 0, out.strip())
        check("valid take: all 4 allowed extensions land", len(landed) == 4, str(landed))

        # 🛑 ROUTING. The engine reads .sav/.hi from .scratch/saves and
        # script-saves from .scratch/savestates. Sending every .sNN to saves/
        # left savestates/ empty, so a replay booted without the unlocks the
        # take was recorded against -- a guaranteed desync on any multi-set PAK.
        # That bug was found by reading the source in Aug 2026; nothing here
        # would have caught it, because the driver only ever looked at the top
        # of destdir. It does now.
        check("routing: .sav lands in saves/",
              leaf_of(d_good, "TestPak.sav") == "saves", leaf_of(d_good, "TestPak.sav"))
        check("routing: .hi lands in saves/",
              leaf_of(d_good, "TestPak.hi") == "saves", leaf_of(d_good, "TestPak.hi"))
        check("routing: .s00 lands in savestates/",
              leaf_of(d_good, "TestPak.s00") == "savestates", leaf_of(d_good, "TestPak.s00"))
        check("routing: .s07 lands in savestates/",
              leaf_of(d_good, "TestPak.s07") == "savestates", leaf_of(d_good, "TestPak.s07"))
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

        # ---- the CONTENT gate on script-saves --------------------------------
        #
        # 🛑 A .sNN is admitted by NAME and then judged by CONTENT. The name
        # cases above cannot see that second control at all, so a build that
        # accepted .sNN and never parsed it would pass every one of them --
        # which is exactly the round-14 RCE (take -> .scratch/savestates ->
        # loadScriptFile -> Script_Compile -> Script_Execute, as root).
        #
        # These assert the extractor CONSULTS the validator. The grammar itself
        # is exercised call-by-call in test_snap_script.py, which cuts and runs
        # mrec_snap_script_ok directly; duplicating that here would be a second
        # copy to keep in step.
        for label, blob in [
            ("hostile script (savefilestream)", SCRIPT_BAD),
            ("not script at all", b"SET0"),
            ("a NUL inside otherwise-valid script",
             b'void main() {\n    setglobalvar("a\x00b", 1);\n}\n'),
            ("one byte over the script byte cap",
             b"void main() {\n" + b'    setglobalvar("a", 1);\n' * 2900
             + b"}\n" + b" " * SCRIPT_MAX_BYTES),
        ]:
            p = take("badscript.inp", pak="TestPak", frames=2,
                     payload=[("TestPak.s00", blob)])
            d = os.path.join(work, "d_badscript")
            shutil.rmtree(d, ignore_errors=True)
            rc, out, landed = run(exe, p, d)
            check("refuse: %s" % label, rc == 1 and not landed,
                  "rc=%d landed=%s out=%s" % (rc, landed, out.strip()))

        # 🛑 AND IT COSTS THE WHOLE PAYLOAD, which is why the WRITER validates
        # too (see test_writer_reader_agree.py). If a bad script only skipped
        # its own entry the take would quietly lose its unlocks; refusing
        # everything is the safer half of that trade, and it is what makes
        # shipping a take this build's own reader rejects unacceptable.
        p = take("mixedscript.inp", pak="TestPak", frames=2, payload=[
            ("TestPak.sav", b"GOOD"), ("TestPak.s00", SCRIPT_BAD)])
        d = os.path.join(work, "d_mixedscript")
        shutil.rmtree(d, ignore_errors=True)
        rc, out, landed = run(exe, p, d)
        check("refuse: a hostile script rejects the ENTIRE payload",
              rc == 1 and not landed, "landed=%s" % landed)

        # ...and the mirror: a VALID script must not be collateral damage.
        p = take("okscript.inp", pak="TestPak", frames=2, payload=[
            ("TestPak.sav", b"GOOD"), ("TestPak.s00", SCRIPT_OK)])
        d = os.path.join(work, "d_okscript")
        shutil.rmtree(d, ignore_errors=True)
        rc, out, landed = run(exe, p, d)
        check("accept: a valid script lands alongside the save",
              rc == 0 and len(landed) == 2
              and leaf_of(d, "TestPak.s00") == "savestates",
              "rc=%d landed=%s" % (rc, landed))

        # ---- container guards ------------------------------------------------
        #
        # 🛑 These assert rc == 3, not rc == 1, and the distinction is the point.
        # 1 means "a payload was found and REFUSED"; 3 means "this file cannot
        # contain one". Collapsed together, a take that predates the payload
        # feature was reported to the user as having had its payload refused --
        # a decision nobody made about data that never existed.
        #
        # 3 rather than 2 because the driver above returns 2 for its own usage
        # errors. Asserting the SPECIFIC code is what keeps these honest: a
        # driver mistake cannot masquerade as either verdict.
        p = take("newer.inp", pak="TestPak", frames=2, container=3)
        rc, out, _ = run(exe, p, os.path.join(work, "d_new"))
        check("refuse: newer container (rc=3, not a v2 take)", rc == 3, out.strip())

        p = take("older.inp", pak="TestPak", frames=2, container=1)
        rc, out, _ = run(exe, p, os.path.join(work, "d_old"))
        check("refuse: older container (rc=3, not a v2 take)", rc == 3, out.strip())

        p = take("magic.inp", pak="TestPak", frames=2, bad_magic=True)
        rc, out, _ = run(exe, p, os.path.join(work, "d_magic"))
        check("refuse: bad magic (rc=3, not a v2 take)", rc == 3, out.strip())

        # ...and a REFUSAL still reports 1, so the two cannot drift together.
        p = take("refused.inp", pak="TestPak", frames=2,
                 payload=[("../evil.sav", b"X")])
        rc, out, _ = run(exe, p, os.path.join(work, "d_ref"))
        check("a refused payload is still rc=1, distinct from rc=3", rc == 1,
              "rc=%d" % rc)

        # A ~290-byte header claiming two million frames allocated 144 MB before
        # the read failed -- on a 1 GB board shared with the FPGA.
        p = take("huge.inp", pak="TestPak", frames=1, claim_frames=mrec_synth.MAX_FRAMES)
        rc, out, _ = run(exe, p, os.path.join(work, "d_huge"))
        check("refuse: frame count larger than the file", rc == 1, out.strip())

        p = take("trunc.inp", pak="TestPak", frames=8,
                 payload=[("TestPak.sav", b"X" * 64)], truncate_after=340)
        rc, out, _ = run(exe, p, os.path.join(work, "d_trunc"))
        check("refuse: truncated mid-file", rc == 1, out.strip())

        # 🛑 The LAST record specifically. A mid-payload over-seek is caught for
        # free by the NEXT record's fread returning nothing -- which is why the
        # case above passed long before pass 1 could detect truncation at all.
        # The final entry has no successor, so a payload whose last entry
        # declares more data than the file holds validated CLEAN and only failed
        # later in the write pass, surfacing as "write failed after N file(s)":
        # the write blamed for a source that was short. Chop the tail off the
        # last entry's data and nothing else.
        p = take("lasttrunc.inp", pak="TestPak", frames=4,
                 payload=[("TestPak.sav", b"A" * 32), ("TestPak.hi", b"B" * 256)])
        with open(p, "r+b") as fh:
            fh.seek(0, 2)
            fh.truncate(fh.tell() - 200)   # last entry claims 256 bytes, has 56
        rc, out, landed = run(exe, p, os.path.join(work, "d_lasttrunc"))
        # 🛑 Assert the REASON, not just rc. Verified with snap_eof_negtest.py:
        # with the guard REMOVED this still returns rc=1 and lands nothing,
        # because the write pass fails and the cleanup wipes both leaves. So an
        # rc-only assertion passes either way and tests nothing. What the guard
        # actually changes is that VALIDATION refuses -- before any mkdir -- and
        # names truncation, instead of the write blaming itself for a short
        # source. The invariant was already held; it was being held by the error
        # path rather than by the pass that claims to hold it.
        check("refuse: truncated LAST payload entry",
              rc == 1 and not landed and "ends past the file" in out,
              "rc=%d landed=%s out=%s" % (rc, landed, out.strip()))

        # ---- absent payload is not corruption --------------------------------
        p = take("nopay.inp", pak="TestPak", frames=4, payload=[])
        rc, out, landed = run(exe, p, os.path.join(work, "d_nopay"))
        check("empty payload: accepted, nothing written",
              rc == 0 and not landed, "rc=%d landed=%s" % (rc, landed))

        # ---- identity: a take that declines to vouch may not restore files ----
        #
        # 🛑 THIS CHECK USED TO ASSERT THE OPPOSITE, and the old behaviour was
        # the finding. ic > 1 is refused and ic == 1 is hash-verified, so ic == 0
        # skipped the content guard entirely -- one hostile take worked against
        # ANY PAK, which is what made the round-14 payload hole universal rather
        # than targeted. A take may still decline to vouch (the writer emits 0
        # for an unhashable PAK) and still PLAY; what it may not do is write a
        # stranger's files into the scratch on nobody's authority.
        p = take("noid.inp", pak="TestPak", frames=4, content_hash=None,
                 payload=[("TestPak.sav", b"S")])
        rc, out, landed = run(exe, p, os.path.join(work, "d_noid"))
        check("refuse: unverifiable take (0 identity entries) with a payload",
              rc == 1 and not landed, "rc=%d landed=%s" % (rc, landed))

        # ...but an EMPTY payload has nothing to be wrong about, so an
        # unverifiable take is not turned into an error by this rule alone.
        p = take("noid_empty.inp", pak="TestPak", frames=4, content_hash=None,
                 payload=[])
        rc, out, landed = run(exe, p, os.path.join(work, "d_noid_empty"))
        check("accept: unverifiable take with NO payload",
              rc == 0 and not landed, "rc=%d landed=%s" % (rc, landed))

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
