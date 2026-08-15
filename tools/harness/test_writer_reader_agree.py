"""The writer must not emit takes its own reader refuses.

WHY THIS EXISTS
---------------
test_snap_extract.py cuts the READER out of the emitted source and drives it
with synthesised takes. That is the right shape for a security boundary, and it
has caught real refusals. But it can only ever observe the reader -- so a WRITER
that produces something illegal has no case that can see it, and on 2026-08-13
that blind spot was found to contain two live bugs at once:

  * the snapshot embed loop took every non-dot file in the scratch, including
    the <pak>.scr the handler seeds there. mrec_snap_ext_ok refuses .scr, and
    ONE bad entry refuses the WHOLE payload -- so a PAK carrying a .scr wrote a
    take that this same build then refused, permanently, with nothing anywhere
    pointing at the cause.

  * the identity writer emitted strlen(name) as a u16 with no bound, while both
    readers refuse nl >= 512 into a char[512]. The name is the PAK path relative
    to Paks/ with separators flattened, so a deep enough subfolder did the same
    thing again.

Both were fixed on the WRITER. 🛑 Never on the reader: the first attempt at the
.scr one widened mrec_snap_ext_ok to accept .scr, which would have turned a
usability bug into arbitrary script execution -- saveScriptFile() emits
re-executable OpenBOR script and loadScriptFile() runs it on load, and takes are
shared between strangers. The existing suite caught that, which is the whole
argument for having it.

WHAT THIS CHECKS
----------------
Not a round trip -- the writer is emitted C spread across a 700-line function
and is not separable the way the two reader functions are. Instead it asserts
the two sides AGREE, by extracting each side's constraint from the emitted
source and comparing them:

  A. extension whitelist: the embed loop's filter vs mrec_snap_ext_ok
  B. identity name bound: the writer's guard vs both readers' array bound

That is narrower than a round trip and it is exactly the divergence class that
produced both bugs, which is the property worth having. If the writer ever
grows a third constraint, this file is where its twin belongs.

    python3 test_writer_reader_agree.py
"""

import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
UPSTREAM = "https://github.com/DCurrent/openbor.git"

fails = []
checks = 0


def check(ok, label, detail=""):
    global checks
    checks += 1
    print(("PASS  " if ok else "FAIL  ") + label + (("  -- " + detail) if detail else ""))
    if not ok:
        fails.append(label)


def emit(dst):
    """Run the real patcher over pristine upstream and return the emitted tree."""
    src = os.path.join(dst, "ob")
    subprocess.run(["git", "clone", "--depth", "1", "--branch", "v7533",
                    "--filter=blob:none", "-q", UPSTREAM, src], check=True)
    eng = os.path.join(src, "engine")
    for f in os.listdir(os.path.join(REPO, "src")):
        if f.startswith("native_") and (f.endswith(".c") or f.endswith(".h")):
            with open(os.path.join(REPO, "src", f), "rb") as a, \
                 open(os.path.join(eng, f), "wb") as b:
                b.write(a.read())
    hdr = os.path.join(eng, "openbor.h")
    with open(hdr, encoding="utf-8", errors="replace") as f:
        t = f.read()
    with open(hdr, "w", encoding="utf-8") as f:
        f.write(t.replace("stricmp", "strcasecmp"))
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable,
                        os.path.join(REPO, ".github", "scripts", "apply_patches.py"),
                        eng, os.path.join(REPO, "patches")],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.exit("apply_patches failed:\n" + r.stdout[-3000:] + r.stderr[-3000:])
    return eng


def read(p):
    with open(p, encoding="utf-8", errors="replace") as f:
        return f.read()


with tempfile.TemporaryDirectory() as td:
    eng = emit(td)
    ob = read(os.path.join(eng, "openbor.c"))
    sp = read(os.path.join(eng, "sdl", "sdlport.c"))

    # ---- A. extension whitelist --------------------------------------------
    # reader: mrec_snap_ext_ok, in the emitted sdlport.c
    m = re.search(r"mrec_snap_ext_ok\s*\([^)]*\)\s*\{(.*?)\n\}", sp, re.S)
    check(m is not None, "found mrec_snap_ext_ok in the emitted sdlport.c")

    # 🛑 COMPILE BOTH PREDICATES AND COMPARE VERDICTS. An earlier version of this
    # matched on the SHAPE of each filter -- which extension literals and
    # character tests appeared in the text -- and its own negative test walked
    # straight through it: flipping the .sNN branch from `_mr_okx = 1` to
    # `_mr_okx = 0` leaves every token in place, so a writer that had stopped
    # accepting the family still looked identical to one that did.
    #
    # Same doctrine as test_snap_extract.py, and for the same reason: cut the
    # REAL code out and RUN it. A predicate is defined by what it answers, not
    # by what it is spelled with.
    CORPUS = [
        "game.sav", "game.hi", "game.s00", "game.s01", "game.s99",
        "game.scr", "game.SCR", "game.SAV", "game.HI", "game.S00",
        "game.s0", "game.s000", "game.sa0", "game.s0a", "game.s-1",
        "game.txt", "game.pak", "game.inp", "game", ".sav", "game.",
        "a.b.sav", "a.b.scr", "game.s00.scr", "game.scr.s00",
    ]

    def cut_reader(sp_src):
        out = []
        for fn in ("mrec_snap_is_script_save", "mrec_snap_ext_ok"):
            m2 = re.search(r"(static\s+int\s+" + fn + r"\s*\([^)]*\)\s*\n\{.*?\n\})",
                           sp_src, re.S)
            check(m2 is not None, "cut %s() out of the emitted sdlport.c" % fn)
            if m2:
                out.append(m2.group(1))
        return "\n".join(out)

    def cut_writer(ob_src):
        m2 = re.search(r"(const char \*_mr_dot = strrchr\(.*?if\(!_mr_okx\) continue;)",
                       ob_src, re.S)
        check(m2 is not None, "cut the embed-loop filter out of the emitted openbor.c")
        if not m2:
            return None
        body = m2.group(1)
        body = body.replace("_mr_e->d_name", "name")
        body = body.replace("if(!_mr_okx) continue;", "return _mr_okx;")
        return "static int writer_ok(const char *name)\n{\n" + body + "\n}\n"

    rdsrc, wrsrc = cut_reader(sp), cut_writer(ob)
    if rdsrc and wrsrc:
        prog = ('#include <stdio.h>\n#include <string.h>\n#include <strings.h>\n'
                + rdsrc + "\n" + wrsrc + "\n"
                + 'int main(void){ const char *c[] = {'
                + ",".join('"%s"' % x for x in CORPUS) + '};\n'
                  '  int i, n = (int)(sizeof(c)/sizeof(c[0]));\n'
                  '  for(i=0;i<n;i++) printf("%s %d %d\\n", c[i],'
                  '        writer_ok(c[i])?1:0, mrec_snap_ext_ok(c[i])?1:0);\n'
                  '  return 0; }\n')
        cdir = os.path.join(td, "agree")
        os.makedirs(cdir, exist_ok=True)
        with open(os.path.join(cdir, "agree.c"), "w", encoding="utf-8") as f:
            f.write(prog)
        cc = os.environ.get("CC", "cc")
        b = subprocess.run([cc, "-O0", "-w", "-o", os.path.join(cdir, "agree"),
                            os.path.join(cdir, "agree.c")],
                           capture_output=True, text=True)
        check(b.returncode == 0, "both predicates COMPILE as cut",
              (b.stderr or b.stdout)[-400:])
        if b.returncode == 0:
            r = subprocess.run([os.path.join(cdir, "agree")],
                               capture_output=True, text=True)
            rows = [L.split() for L in r.stdout.split("\n") if L.strip()]
            check(len(rows) == len(CORPUS), "every corpus name was evaluated",
                  "%d of %d -- a short run would make disagreements invisible"
                  % (len(rows), len(CORPUS)))
            bad = [(n, w, d) for n, w, d in rows if w != d]
            check(not bad, "writer and reader agree on EVERY corpus name",
                  "; ".join("%s writer=%s reader=%s" % t for t in bad[:6]))
            scr = dict((n, (w, d)) for n, w, d in rows)
            check(scr.get("game.scr") == ("0", "0")
                  and scr.get("game.SCR") == ("0", "0"),
                  ".scr refused by BOTH, case-insensitively "
                  "(re-executable script from a stranger)",
                  "got %s / %s" % (scr.get("game.scr"), scr.get("game.SCR")))
            check(scr.get("game.s00") == ("1", "1"),
                  ".sNN accepted by BOTH (the family the handler actually seeds)",
                  "got %s" % (scr.get("game.s00"),))

    # ---- B. identity name bound -------------------------------------------
    # readers: the identity-name buffers, refusing nl >= sizeof.
    #
    # 🛑 SCOPED, not a bare `char nm[` sweep. openbor.c is 47k lines and `nm` is
    # an ordinary local -- an unrelated one at ~47006 is char nm[4096], which a
    # loose regex folded in and reported as "the two readers disagree, 512 vs
    # 4096". They do not; it is a different function entirely. Both real sites
    # declare the length as an `unsigned short` on the SAME line, which is what
    # makes them identity readers rather than any buffer that happens to be
    # spelled nm.
    bounds = set(
        int(x) for x in
        re.findall(r"unsigned short[^;\n]*\bnl\s*=[^;\n]*;\s*char\s+nm\s*\[\s*(\d+)\s*\]", ob)
        + re.findall(r"char\s+_mr_idn\s*\[\s*(\d+)\s*\]", ob))
    check(len(bounds) >= 1, "found the identity-name readers at all",
          "an empty set would pass a ==1 test by accident")
    check(len(bounds) == 1,
          "both readers use ONE identity-name array size", "sizes=%s" % sorted(bounds))
    if len(bounds) == 1:
        n = bounds.pop()
        wb = re.search(r"strlen\(_mr_nm\)\s*<\s*(\d+)", ob)
        check(wb is not None, "the identity writer has a length bound at all")
        if wb:
            check(int(wb.group(1)) == n,
                  "writer bound == reader array size",
                  "writer<%s reader[%d]" % (wb.group(1), n))
        # and the readers must actually enforce it, not just declare the array
        check(len(re.findall(r"(?:nl|_mr_nl)\s*>=\s*\(?\s*unsigned short\s*\)?\s*sizeof", ob)) >= 1
              or len(re.findall(r"(?:nl|_mr_nl)\s*>=\s*sizeof", ob)) >= 1,
              "readers refuse nl >= sizeof(array), rather than only declaring it")

    # ---- C. the script-save content gate -----------------------------------
    #
    # The third constraint this file's docstring anticipated. A .sNN is a
    # PROGRAM, so the writer and reader must agree on TWO further things or the
    # writer ships takes its own reader refuses -- and one refused entry costs
    # the WHOLE payload, .sav and .hi included. That is the .scr divergence
    # again with a bigger blast radius.
    #
    # C1. ONE parser, not two. The writer must CALL the reader's function, not
    #     carry a copy: two copies drift, and the copy that drifts is the one
    #     that decides what a stranger may run as root.
    # 🛑 A DEFINITION, not a declaration -- the file deliberately carries a
    # prototype above the body (so -Wmissing-prototypes stays quiet if it is
    # ever switched on), and a pattern that cannot tell the two apart reports
    # "2 definitions" for correct code. Require the opening brace.
    _defs = re.findall(r"^int mrec_snap_script_ok\s*\([^;{]*\)\s*\n\{", sp, re.M)
    check(len(_defs) == 1,
          "the validator is DEFINED exactly once, in sdlport.c",
          "found %d definitions" % len(_defs))
    check(len(re.findall(r"^int mrec_snap_script_ok\s*\([^;{]*\)\s*;", sp, re.M)) == 1,
          "...with the prototype that keeps it callable from openbor.c")
    check("int mrec_snap_script_ok" not in ob.replace(
              "extern int mrec_snap_script_ok", ""),
          "openbor.c holds NO second copy of the validator -- it externs the real one")
    check(re.search(r"extern int mrec_snap_script_ok\s*\(", ob) is not None,
          "the writer declares the extern it calls")
    check(re.search(r"mrec_snap_script_ok\s*\(\s*_mr_sb", ob) is not None,
          "the writer actually CALLS it before embedding")

    # C2. the same byte cap on both sides. The writer cannot see the reader's
    #     #define across the TU boundary, so it carries a literal -- which is
    #     precisely the shape that goes stale silently. Compared here, exactly
    #     as the identity-name bound above is.
    rcap = re.search(r"#define MREC_SCRIPT_MAX_BYTES\s+(\d+)", sp)
    wcap = re.search(r"_mr_fl\s*<=\s*(\d+)", ob)
    check(rcap is not None, "the reader declares a script byte cap")
    check(wcap is not None, "the writer bounds the script buffer at all")
    if rcap and wcap:
        check(int(rcap.group(1)) == int(wcap.group(1)),
              "writer script cap == reader MREC_SCRIPT_MAX_BYTES",
              "writer=%s reader=%s" % (wcap.group(1), rcap.group(1)))

print()
print("%d/%d checks passed" % (checks - len(fails), checks))
if fails:
    print("FAILED: " + "; ".join(fails))
sys.exit(1 if fails else 0)
