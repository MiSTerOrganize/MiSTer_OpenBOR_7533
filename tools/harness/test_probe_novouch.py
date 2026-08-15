#!/usr/bin/env python3
"""test_probe_novouch.py -- the probe's no-vouch guard, on real takes.

WHAT THIS TESTS AND WHY IT CAN BE TRUSTED
-----------------------------------------
mrec_probe_take() decides whether a picked take is allowed to reset the content
and play. An identity count of 0 used to return "plays" unconditionally, so a
take that identifies no PAK ran against ANY of them -- and its payload was then
restored on nobody's authority. That is what turned the round-14 payload hole
from targeted into universal.

The guard has to find the payload count, which sits past a VARIABLE-LENGTH
identity section (the recorder save stem follows the count) and then past the
frame block. A first version computed HDR + 2 + frames and landed inside the
stem -- the exact hand-computed-offset mistake the container's own comments say
broke this format once before. Reading the code cannot tell you which; running
it can.

So this cuts mrec_probe_take out of the EMITTED openbor.c, compiles it natively
with the header macros it needs, and drives it with takes from mrec_synth.py --
including stems of several lengths, because a stem-length bug is invisible at
one length.

    python3 test_probe_novouch.py [--keep]
"""

import os
import re
import shutil
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

PROLOGUE = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
/* The probe hashes the loaded PAK through mrec_pak_hash. That reaches the
 * engine, so it is stubbed: this suite is about the no-vouch path, where the
 * hash is never consulted, and every case here carries no identity entry. A
 * stub that ALWAYS FAILS is the safe direction -- if a case ever reached the
 * compare, it would refuse rather than silently pass. */
#define NSHA1_DIGEST_LEN 20
static const char *packfile = "TestPak.pak";
static int mrec_pak_hash(const char *p, unsigned char out[NSHA1_DIGEST_LEN])
{ (void)p; (void)out; return -1; }
"""

TEST_MAIN = r"""
/* 0 = would play, 1 = refused (why[] says so), 2 = driver usage error.
 * Distinct, so a driver mistake can never read as a refusal. */
int main(int argc, char **argv)
{
    char why[128];
    int ok;
    if (argc < 2) { fprintf(stderr, "usage: %s <take>\n", argv[0]); return 2; }
    why[0] = 0;
    ok = mrec_probe_take(argv[1], why, (int)sizeof(why));
    printf("%s\n", why);
    return ok ? 0 : 1;
}
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


def cut(eng):
    """The MREC_* header macros plus mrec_probe_take, from the emitted C."""
    src = open(os.path.join(eng, "openbor.c"), encoding="utf-8", errors="replace").read()
    # MREC_ENGINE_VER too: the probe refuses a version mismatch before it ever
    # reaches the identity section, so a re-typed value here would make every
    # case below refuse for the wrong reason.
    defines = re.findall(
        r"^#define\s+MREC_(?:OFF|LEN|HDR|FRAME)_\w+.*$|^#define\s+MREC_ENGINE_VER\b.*$",
        src, re.M)
    # The offsets are the whole point -- a re-typed copy here would agree with a
    # broken probe by construction.
    if len(defines) < 8:
        raise SystemExit("expected the MREC_* header offset macros in the emitted C, "
                         "found %d -- did they move or get renamed?" % len(defines))
    start = src.find("int mrec_probe_take(const char *path, char *why, int whysz)\n{")
    if start < 0:
        raise SystemExit("mrec_probe_take not found in the emitted openbor.c")
    end = src.find("\n}\n", start)
    if end < 0:
        raise SystemExit("could not find the end of mrec_probe_take")
    body = src[start:end + 3]
    if "does not say which game" not in body:
        raise SystemExit("the cut is missing the no-vouch guard -- was it removed?")
    return "\n".join(defines) + "\n" + body


def compile_probe(work, body):
    csrc = os.path.join(work, "probe.c")
    open(csrc, "w", encoding="utf-8", newline="\n").write(PROLOGUE + body + TEST_MAIN)
    exe = os.path.join(work, "probe")
    r = subprocess.run([os.environ.get("CC", "gcc"), "-O1", "-g", "-o", exe, csrc],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr[-4000:])
        raise SystemExit("the extracted probe did not compile")
    return exe


def verdict(exe, path):
    r = subprocess.run([exe, path], capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def main():
    keep = "--keep" in sys.argv
    work = tempfile.mkdtemp(prefix="probenv_")
    print("workdir:", work)
    try:
        eng = build_tree(work)
        exe = compile_probe(work, cut(eng))
        print("mrec_probe_take compiled natively from the emitted C\n")

        def take(name, **kw):
            p = os.path.join(work, name)
            mrec_synth.write_take(p, **kw)
            return p

        PAY = [("TestPak.sav", b"SAVE")]

        # 🛑 SEVERAL STEM LENGTHS. The guard walks past a variable-length stem to
        # reach the payload count; at one fixed length an off-by-N is invisible,
        # and the first version of the guard skipped the stem entirely.
        for stem in ("TestPak", "", "A", "x" * 100):
            label = "stem=%r" % (stem if len(stem) < 12 else "x*100")
            p = take("nv.inp", pak="TestPak", frames=6, content_hash=None,
                     stem=stem, payload=PAY)
            rc, why = verdict(exe, p)
            check("refuse: no identity + payload (%s)" % label,
                  rc == 1 and "does not say which game" in why,
                  "rc=%d why=%r" % (rc, why))

            p = take("nv_empty.inp", pak="TestPak", frames=6, content_hash=None,
                     stem=stem, payload=[])
            rc, why = verdict(exe, p)
            check("accept: no identity, NO payload (%s)" % label,
                  rc == 0, "rc=%d why=%r" % (rc, why))

        # Frame count varies the other term of the same walk.
        for frames in (0, 1, 250):
            p = take("nvf.inp", pak="TestPak", frames=frames, content_hash=None,
                     payload=PAY)
            rc, why = verdict(exe, p)
            check("refuse: no identity + payload (frames=%d)" % frames,
                  rc == 1 and "does not say which game" in why,
                  "rc=%d why=%r" % (rc, why))

        # A take that DOES vouch takes the hash path instead. The stub always
        # fails, so it must refuse -- and must NOT report the no-vouch reason,
        # which would mean the guard fired on the wrong take.
        p = take("id.inp", pak="TestPak", frames=6, payload=PAY)
        rc, why = verdict(exe, p)
        check("a take WITH identity is judged by hash, not by this guard",
              rc == 1 and "does not say which game" not in why,
              "rc=%d why=%r" % (rc, why))

    finally:
        if keep:
            print("\nkept:", work)
        else:
            shutil.rmtree(work, ignore_errors=True)

    bad = [n for n, ok, _ in RESULTS if not ok]
    print("\n%d/%d checks passed" % (len(RESULTS) - len(bad), len(RESULTS)))
    for n in bad:
        print("  -", n)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
