#!/usr/bin/env python3
"""test_snap_script.py -- drive the REAL script-save validator, natively.

WHAT THIS TESTS AND WHY IT CAN BE TRUSTED
-----------------------------------------
mrec_snap_script_ok() decides whether a PROGRAM written by a stranger is allowed
to reach .scratch/savestates, where loadScriptFile() will compile and EXECUTE it
as root. Round 14 found that path reachable from a shared .inp -- the take could
call savefilestream, whose path argument is unsanitised, and write
/media/fat/linux/user-startup.sh. This function is the control that makes
carrying unlocks safe again, so it is a security boundary and belongs under
machine check forever rather than being hand-tested once.

So this does NOT reimplement the grammar and test the reimplementation. It:

  1. runs apply_patches.py into a pristine v7533 clone,
  2. CUTS mrec_snap_script_ok() and its helpers out of the EMITTED
     sdl/sdlport.c by text markers,
  3. compiles them natively (x86, no ARM, no QEMU, no SDL) with a test main(),
  4. feeds that binary accept and refuse cases on stdin.

Point 2 is what makes it meaningful: a test against a copy of the logic would
pass while the shipped code rotted. [[parser-tests-cut-real-source]]

🛑 EXIT CODES ARE DISTINCT ON PURPOSE. The driver returns 2 for its own errors,
the validator's verdict is 0 (accept) or 1 (refuse), and every refusal case
below asserts rc == 1 -- never rc != 0. The sibling suite shipped once with
eleven refusal checks that passed while executing nothing, because a driver
usage error exited non-zero and `rc != 0` accepted it as a refusal.

    python3 test_snap_script.py [--keep]
"""

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))

UPSTREAM = "https://github.com/DCurrent/openbor.git"
# EXACTLY the files build_mister_arm.sh copies -- a glob here would be more
# permissive than CI, which is how a missing native_sha1.h once dry-ran clean
# and then failed the build.
NATIVE_COPIES = ["native_video_writer.c", "native_video_writer.h",
                 "native_audio_writer.c", "native_audio_writer.h",
                 "native_sha1.h"]

PROLOGUE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* The cut fragment must carry its OWN includes -- it gets them transitively in
 * the shipped file, and a probe that inherits nothing fails to compile. */
"""

TEST_MAIN = r"""
/* Reads the candidate script from stdin so a case may contain ANY byte --
 * embedded NUL, a high-bit byte, a lone CR. Passing it as argv would have made
 * exactly the hostile cases unrepresentable, and they are the point.
 *
 * len is the byte count READ, not strlen(), so an embedded NUL reaches the
 * validator as the shipped code sees it (a payload entry carries an explicit
 * length; the validator is what decides a NUL is disqualifying).
 */
int main(void)
{
    static char buf[1 << 20];
    size_t n = fread(buf, 1, sizeof(buf) - 1, stdin);
    if (ferror(stdin)) { fprintf(stderr, "driver: read error\n"); return 2; }
    if (n >= sizeof(buf) - 1) { fprintf(stderr, "driver: case too large\n"); return 2; }
    buf[n] = 0;
    return mrec_snap_script_ok(buf, (long)n) ? 0 : 1;
}
"""

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("  %-62s %s%s" % (name, "PASS" if cond else "FAIL",
                            ("  -- " + detail) if detail and not cond else ""))


def build_tree(work):
    tree = os.path.join(work, "ob")
    subprocess.run(["git", "clone", "--depth", "1", "--branch", "v7533",
                    "--filter=blob:none", "--quiet", UPSTREAM, tree], check=True)
    eng = os.path.join(tree, "engine")
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


def cut_validator(eng):
    """Lift the validator group out of the emitted sdlport.c.

    🛑 The anchor is the #define that opens the group, not the function this
    test is nominally about -- the helpers (mrec_sv_ws/_str/_num/_arg/_lit) sit
    between them and a cut starting at mrec_snap_script_ok would compile with
    implicit declarations and then fail to LINK. That is precisely how the
    sibling suite's anchor went wrong once.
    """
    src = open(os.path.join(eng, "sdl", "sdlport.c"),
               encoding="utf-8", errors="replace").read()
    start = src.find("#define MREC_SCRIPT_MAX_BYTES")
    if start < 0:
        raise SystemExit("MREC_SCRIPT_MAX_BYTES not in the emitted C -- did the "
                         "sdlport_patch.c splice marker break again, or was the "
                         "validator removed?")
    end = src.find("\n}", src.find("int mrec_snap_script_ok(const char *text, long len)\n{"))
    if end < 0:
        raise SystemExit("could not find the end of mrec_snap_script_ok")
    body = src[start:end + 2]
    # A cut that lands the PROTOTYPE but not the DEFINITION would compile and
    # then fail to link with a message about a missing symbol rather than about
    # a broken cut. Say which it was.
    if "return *p == 0;" not in body:
        raise SystemExit("the cut is missing the body of mrec_snap_script_ok")
    return body


def compile_probe(work, body):
    csrc = os.path.join(work, "probe.c")
    open(csrc, "w", encoding="utf-8", newline="\n").write(PROLOGUE + body + TEST_MAIN)
    exe = os.path.join(work, "probe")
    cc = os.environ.get("CC", "gcc")
    r = subprocess.run([cc, "-O1", "-g", "-o", exe, csrc], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-4000:])
        raise SystemExit("the extracted validator did not compile")
    return exe


def verdict(exe, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    r = subprocess.run([exe], input=data, capture_output=True)
    return r.returncode


def main():
    keep = "--keep" in sys.argv
    work = tempfile.mkdtemp(prefix="snapscript_")
    print("workdir:", work)
    try:
        eng = build_tree(work)
        exe = compile_probe(work, cut_validator(eng))
        print("validator compiled natively from the emitted C\n")

        # ---- ACCEPT: the shapes the corpus actually contains -----------------
        #
        # 🛑 SYNTHESISED, not copied from the card. The 15 surveyed files are a
        # user's own save data and do not belong in a public repo; what matters
        # is the four LINE SHAPES the survey found, which are reproduced here
        # exactly (docs/dev/script_save_validator.md records the survey command
        # so the grammar can be re-measured, not re-guessed).
        accepts = [
            ("minimal, no statements",
             "void main() {\n}\n"),
            ("setglobalvar with a string name",
             'void main() {\n    setglobalvar("unlocked_raph", 1);\n}\n'),
            ("setglobalvar with a numeric name",
             "void main() {\n    setglobalvar(3, 1);\n}\n"),
            ("changemodelproperty",
             'void main() {\n    changemodelproperty("Raph", 12, 1);\n}\n'),
            ("negative and fractional numbers",
             'void main() {\n    setglobalvar("x", -1);\n'
             '    changemodelproperty("M", 4, -2.5);\n}\n'),
            ("many statements, mixed",
             "void main() {\n" + "".join(
                 '    setglobalvar("v%d", %d);\n    changemodelproperty("m%d", %d, 1);\n'
                 % (i, i, i, i) for i in range(60)) + "}\n"),
            ("CRLF line endings",
             'void main() {\r\n    setglobalvar("a", 1);\r\n}\r\n'),
            ("no trailing newline",
             'void main() {\n    setglobalvar("a", 1);\n}'),
            ("tabs and irregular spacing",
             'void\tmain\t(\t)\t{\t\n\tsetglobalvar\t(\t"a"\t,\t1\t)\t;\n}\n'),
            ("all on one line",
             'void main() { setglobalvar("a", 1); changemodelproperty("b", 2, 3); }'),
            ("empty string argument",
             'void main() {\n    setglobalvar("", 0);\n}\n'),
            ("at the byte cap", None),        # filled below -- needs the caps
            ("at the statement cap", None),
        ]
        cap = 65536
        stmts_cap = 2048
        head, tail = "void main() {\n", "}\n"

        # 🛑 THE BYTE-CAP CASE MUST STAY UNDER THE STATEMENT CAP, or the accept
        # fails and the refusal passes for the WRONG REASON -- which is exactly
        # what the first version of this did: 22-byte statements needed 2978 of
        # them to reach 64 KiB, so the statement cap refused at 65536 and the
        # "byte cap is exactly N" check reported at=1 over=1, i.e. agreement
        # that proved nothing about the byte cap at all. A LONG statement gets
        # there in ~1000, well inside 2048, so only the byte bound can fire.
        stmt = '    setglobalvar("' + "a" * 40 + '", 1);\n'
        n = (cap - len(head) - len(tail)) // len(stmt)
        assert n < stmts_cap, "the byte-cap case must stay under the statement cap"
        big = head + stmt * n + tail
        big = big + " " * (cap - len(big))          # pad to exactly the cap
        accepts[-2] = ("at the byte cap (%d bytes, %d statements)" % (len(big), n), big)

        # 🛑 The statement cap has to be exercised UNDER the byte cap, or the
        # refusal is attributable to the wrong bound and the check proves
        # nothing. "setglobalvar(0,0);\n" is the shortest statement the grammar
        # admits, so 2049 of them is ~39 KB -- comfortably inside 64 KiB, which
        # is what makes the statement cap the only thing that can refuse it.
        short = "setglobalvar(0,0);\n"
        at_stmts = head + short * stmts_cap + tail
        over_stmts = head + short * (stmts_cap + 1) + tail
        assert len(over_stmts) < cap, "the statement-cap case must fit under the byte cap"
        accepts[-1] = ("at the statement cap (%d statements, %d bytes)"
                       % (stmts_cap, len(at_stmts)), at_stmts)

        for label, data in accepts:
            check("accept: " + label, verdict(exe, data) == 0)

        # ---- REFUSE: everything else ----------------------------------------
        #
        # The first three are the round-14 chain itself. If any of them ever
        # returns 0, a shared take can run code as root again.
        refuses = [
            ("savefilestream (the round-14 primitive)",
             'void main() {\n    savefilestream("/media/fat/linux/user-startup.sh", 0);\n}\n'),
            ("openfilestream",
             'void main() {\n    openfilestream("/etc/passwd", 0);\n}\n'),
            ("system()",
             'void main() {\n    system("sh");\n}\n'),
            ("a permitted call plus a forbidden one",
             'void main() {\n    setglobalvar("a", 1);\n    savefilestream("x", 0);\n}\n'),
            ("a second function definition",
             'void main() {\n}\nvoid evil() {\n    savefilestream("x", 0);\n}\n'),
            ("a nested call as an argument",
             'void main() {\n    setglobalvar(getglobalvar("a"), 1);\n}\n'),
            ("a backslash escape inside a string",
             'void main() {\n    setglobalvar("a\\", 1); savefilestream("x", 0); //", 1);\n}\n'),
            ("an embedded NUL",
             b'void main() {\n    setglobalvar("a\x00b", 1);\n}\n'),
            ("a high-bit byte",
             b'void main() {\n    setglobalvar("\xc3\xa9", 1);\n}\n'),
            ("unbalanced braces (no close)",
             'void main() {\n    setglobalvar("a", 1);\n'),
            ("trailing content after main()",
             'void main() {\n}\nsavefilestream("x", 0);\n'),
            ("a C comment (not in the grammar)",
             'void main() {\n    /* hi */ setglobalvar("a", 1);\n}\n'),
            ("a preprocessor line",
             'void main() {\n#include "evil.c"\n}\n'),
            ("a longer identifier that starts with a permitted one",
             'void main() {\n    setglobalvarEVIL("a", 1);\n}\n'),
            ("a string where a number must be",
             'void main() {\n    setglobalvar("a", "b");\n}\n'),
            ("a bare identifier argument",
             'void main() {\n    setglobalvar(a, 1);\n}\n'),
            ("a missing semicolon",
             'void main() {\n    setglobalvar("a", 1)\n}\n'),
            ("an unterminated string",
             'void main() {\n    setglobalvar("a, 1);\n}\n'),
            ("wrong entry point name",
             'void notmain() {\n    setglobalvar("a", 1);\n}\n'),
            ("arguments to main()",
             'void main(int x) {\n    setglobalvar("a", 1);\n}\n'),
            ("empty input",
             ""),
            ("whitespace only",
             "   \n\t\n"),
            ("one byte over the byte cap",
             big + " "),
            ("one statement over the statement cap (under the byte cap)",
             over_stmts),
            ("too few arguments",
             'void main() {\n    setglobalvar("a");\n}\n'),
            ("too many arguments",
             'void main() {\n    changemodelproperty("a", 1, 2, 3);\n}\n'),
        ]
        for label, data in refuses:
            rc = verdict(exe, data)
            check("refuse: " + label, rc == 1, "rc=%d (2 means the DRIVER failed)" % rc)

        # 🛑 BOTH CAPS ARE PINNED, NOT MERELY "SOMEWHERE". Each is exercised at
        # the boundary and one step past it, so an off-by-one in either
        # direction is visible -- and the statement pair is built to fit under
        # the byte cap, so it cannot pass because the OTHER bound fired.
        check("the byte cap is exactly %d" % cap,
              verdict(exe, big) == 0 and verdict(exe, big + " ") == 1,
              "at=%d over=%d" % (verdict(exe, big), verdict(exe, big + " ")))
        check("the statement cap is exactly %d, and it can fire" % stmts_cap,
              verdict(exe, at_stmts) == 0 and verdict(exe, over_stmts) == 1
              and len(over_stmts) < cap,
              "at=%d over=%d bytes=%d" % (verdict(exe, at_stmts),
                                          verdict(exe, over_stmts), len(over_stmts)))

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
