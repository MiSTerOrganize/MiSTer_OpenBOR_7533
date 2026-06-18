# OpenBOR diff-harness findings

Automated crash/hang/script-compile scan of the local ~450-PAK corpus
(`MiSTerFrontier/OpenBOR_Paks/Paks`) via the headless harness (`diff_harness.yml`
+ `pak_run_scan.sh` in an ubuntu:24.04 glibc container). Two builds scanned:
**stock** v7533, and **patched** = v7533 + our shipped engine-logic patches
(`apply_patches.py OB_HEADLESS=1`). Run params: `OB_FRAMES=90 OB_ALARM=20`.

## Aggregate (450 PAKs)

| exit class | stock v7533 | patched (our build) |
|---|---|---|
| `0` clean (ran 90 frames) | 398 | **431** |
| `1` script-compile fail | 49 | **12** |
| `139` crash (SIGSEGV) | 3 | **7** |
| `98/124` hang | 0 | 0 |

Engine-logic patches calibrated **37 of 49** ec=1 (PLAYER_MIN_Z/MAX_Z + other
script constants now registered). Crashes rose 3→7 because PAKs that previously
bailed early with a script-compile error now compile deeper and reach a crashing
path — i.e. the patched build *exposes* more crashes, as expected.

## Crashes on the patched (shipped-equivalent) build — 7 PAKs, 2 signatures

Backtraces resolved with `addr2line -e OpenBOR_headless` (binary has debug_info).
All are **load-time** crashes in real engine code (not video/SDL/harness). An
engine should error gracefully on a bad PAK, not segfault — so these are
candidate engine-robustness bugs. **Pending PC OpenBOR.exe / real-MiSTer
confirmation** (headless build excludes only input/audio patches, which don't
touch these load paths, but hardware confirms they hit the shipped build).

### Signature B — `load_cached_model` @cmd/@script translation crash (SIGSEGV) — PINNED via gdb
gdb backtrace (Moscow RE-Action, loading model `"cum"`):
```
#0 sprintf(__s=namebuf, __fmt=ifid_text "    if(animhandle==%d)...")   stdio2.h:30
#1 load_cached_model(name="cum", owner="models.txt")   openbor.c:17339 (≈ upstream 17197/17239)
#2 load_models()  openbor.c:18490   #3 startup()  #4 openborMain()
```
The crash is in `load_cached_model`'s **@cmd/@script → inline-script translation**
(the block that turns a model's `@cmd`/`@script` animation directives into a
generated OpenBOR script). The `sprintf(namebuf, ifid_text, newanim->index)` at
upstream openbor.c:17197/17239 faults. Two candidate mechanisms (disambiguate
with a -O0/ASan run): (a) `newanim` NULL/dangling when an `@cmd`/`@script`
appears in a state where no current anim is set; (b) the nearby
`scriptbuf[scriptlen - strclen(X)] = 0` lines (17191/17201/17235/17245/17283/85)
are **negative-index writes when `scriptlen < strclen(X)`** (buffer underflow)
that corrupt memory, surfacing at the next sprintf. Real engine-robustness bug
in the @cmd script-translation path.
- Monster Girl Dimensions.pak
- Moscow RE-Action.pak
- Rescue Command - Against the Amazon Girls.pak

### Signature A — preprocessor lexer token-buffer overflow on a long string literal (fortify abort) — PINNED via gdb
gdb backtrace (Memory Loss): `*** buffer overflow detected *** → SIGABRT`
```
#9  strcpy(dest, "...STORY: Invalid filename. 'Story' entity should have an 'alias'...")
#10 pp_token_Init           source/preprocessorlib/pp_lexer.c:63   (destlen=129)
#11 pp_lexer_GetTokenStringLiteral → #12 pp_lexer_GetNextToken
#13 pp_parser_lex_token → #15 Lexer_GetNextToken
```
The script **preprocessor lexer** copies a string-literal token into a fixed
~128-byte buffer via `strcpy` (`pp_token_Init`, pp_lexer.c:63). A PAK script with
a string literal longer than that buffer overflows it; the x86 fortify build
catches it (`__strcpy_chk`) and aborts. On a non-fortify ARM build this would be
a silent heap/stack smash. Real engine bug — the lexer must bound string-literal
token length. (My crash handler maps SIGABRT→exit 139, so these showed as
"crash" in the scan alongside Signature B's raw SIGSEGV.)
- Heaven's Anime Girls.pak
- Hiryu No Ken [Demo].pak
- Memory Loss.pak
- Ogres Mayhem.pak

## Script-compile-fail (ec=1) on patched build — 12 PAKs (candidate script-API gaps)

These compile-fail during PAK script compile = the **preprocess category** signal.
⚠️ NOT confirmed bugs — ec=1 is a CONTAMINATED signal, trust the crashes more.
Checked 2026-06-18: the dev MiSTer's `Avengers - United Battle Force.pak` is
**byte-identical** to the corpus copy (both md5 `5b9867f8c0595519e3abb8486b6d30ec`,
89213098 bytes) — so its ec=1 is NOT a PAK-version difference. Yet Avengers loads
+ plays on the MiSTer. Most likely the failing model (deadpool `takedamagescript`,
`changeentityproperty`) is **lazy-loaded** ('know' model — compiled only when
first spawned, so it doesn't block load on hardware) or the ship build handles a
property the headless build doesn't. Net: ec=1 mixes (a) lazy-model compile
failures that don't break the PAK on hardware, (b) headless-vs-ship script-API
gaps, and (c) possibly real gaps — so each needs per-PAK hardware confirmation.
The crashes (Signatures A/B, in the model-load + preprocessor-lexer paths, NOT
the gated control/sblaster code) are the high-confidence findings.
- Art Of Figting - Trouble In South Town.pak
- Avengers - United Battle Force.pak
- Bare Knuckle VACUUM.pak
- Dungeons & Dragons - Rise Of Warduke.pak
- Golden Axe Myth.pak
- Golden Axe Remake - Special Edition.pak
- Kunio-Kun Renegade L A Remaster.pak
- Lust Rush.pak
- Scorer Horror.pak
- Shadows of Death.pak
- Street Fighter Vs. The King of Fighters.pak
- Streets of Rage X2 Megamix.pak

## PC reference confirmation (stock OpenBOR.exe v4.0 Build 7530, commit 9695908)

Ran each of the 7 on the **stock** Windows PC build (`_pc_test/OpenBOR.v4.0.Build.7533/`,
single-PAK auto-load, crash = NT exception exit code). Stock = NO our patches.

| PAK | Sig | Headless patched (Linux, fortify) | Stock PC v7530 | Interpretation |
|---|---|---|---|---|
| Moscow RE-Action | B | SIGSEGV | **0xC0000005 CRASH** | Real — reproduces on pure-stock reference. `load_cached_model` bug reachable without our patches. |
| Monster Girl Dimensions | B | SIGSEGV | exit 1 (bail `'heart'`) | Stock bails at script-compile; crash reached only after our script-constant patches compile it deeper. |
| Rescue Command - Against the Amazon Girls | B | SIGSEGV | exit 1 (bail `'tuto1'`) | Same — patched-only reach. |
| Memory Loss | A | SIGABRT `__strcpy_chk` | exit 1 (bail `'Ryan'`) | Stock bails at compile; Sig-A overflow reached only on patched build. |
| Ogres Mayhem | A | SIGABRT `__strcpy_chk` | exit 1 (bail `'toadSmoke'`) | Same. |
| Heaven's Anime Girls | A | SIGABRT `__strcpy_chk` | **loads fully, runs clean (exit 0)** | Sig-A is a **fortify-detected latent overflow** — silent on the non-fortified MSVC build (loaded sprites/models/level, 1647 sprites, clean shutdown). |
| Hiryu No Ken [Demo] | A | SIGABRT `__strcpy_chk` | clean exit 0 (loads, same shutdown pattern) | Same as Heaven's — latent overflow silent on stock. |

**Two honest conclusions:**
1. **Signature B (`load_cached_model` @cmd/@script `sprintf`) is a real engine NULL/OOB bug.**
   Moscow crashes pure-stock → confirmed. MGD + Rescue reach it only on our patched build
   (we register the script constants their scripts need → they compile → load proceeds to
   the crashing model). The underlying code is unbounded upstream.
2. **Signature A (`pp_token_Init` string-literal `strcpy`, pp_lexer.c:63) is a real LATENT
   buffer overflow that the fortified Linux headless build CATCHES (`__strcpy_chk` → abort)
   but the non-fortified stock Windows build TOLERATES silently** (Heaven's/Hiryu load fine;
   Memory Loss/Ogres bail earlier at script-compile for unrelated reasons). This is the
   fortified harness doing its job — surfacing UB that "works by luck" on other builds.
   Whether it crashes the shipped **ARM** build depends on ARM hardening: Debian/bullseye
   gcc defaults to `-fstack-protector-strong`, so if the token buffer is stack-allocated the
   canary catches the smash → abort on ARM; otherwise silent corruption. Either way it's a
   real bug to fix (bound the string-literal token length).

**Net for hardware:** Moscow is confirmed real on the reference. The other Sig-B PAKs (MGD,
Rescue) need the **patched** build → confirm on the **MiSTer** (the only patched runtime).
The Sig-A PAKs are latent overflows; the cleanest proof is the fix, not a hardware repro.

**Recommended engine-robustness fixes (apply_patches.py system layer, NOT cart edits):**
- **Sig A:** bound the string-literal token copy in `pp_token_Init` (pp_lexer.c) — truncate +
  graceful error instead of unbounded `strcpy`.
- **Sig B:** guard `load_cached_model` @cmd/@script translation — NULL-check `newanim` and
  bounds-check `scriptlen` before the negative-index writes / `sprintf`.
- Both are outside the LOCKED palette path; still require the standard 7533 regression ritual
  (ATOV + TMNT-RP + a modern PAK) + 4086 parity audit before ship.

## How to reproduce
```
# build: GitHub Actions -> "Diff Harness" workflow (diff_harness.yml) -> download
#        the openbor-headless-linux artifact (OpenBOR_headless).
# scan (dev machine, Docker Desktop, glibc container):
docker run --rm -e OB_FRAMES=90 -e OB_ALARM=20 \
  -v "<PAKS>:/paks:ro" \
  -v "<DIR with OpenBOR_headless + pak_run_scan.sh>:/binsrc:ro" \
  -v "<HOST OUT>:/work" \
  ubuntu:24.04 bash /binsrc/pak_run_scan.sh
# results: /work/scan_results.txt (idx|exitcode|relpath) + /work/logs/<pak>.log
# resolve a crash: addr2line -f -C -e OpenBOR_headless <+0x... addrs from the log>
#
# WHEN addr2line IS INCONCLUSIVE (stack smash = wild addrs, or -O2/-flto inlining
# hides the callee) -> PIN IT WITH GDB (this is how Signatures A + B above were
# pinned). In the glibc container, binary built -g:
docker run --rm -e OB_PAK=/paks/<crashing>.pak -e OB_FRAMES=90 -e OB_ALARM=20 \
  -v "<PAKS>:/paks:ro" -v "<DIR with OpenBOR_headless>:/binsrc:ro" \
  ubuntu:24.04 bash -c "apt-get update -qq && apt-get install -y -qq gdb libsdl2-2.0-0 libvpx9 >/dev/null 2>&1; \
    cp /binsrc/OpenBOR_headless /tmp/ob && cd /tmp && \
    gdb -batch -ex 'handle SIGSEGV stop nopass' -ex run -ex 'bt 16' -ex 'info args' -ex 'info locals' --args ./ob"
# 'handle SIGSEGV stop nopass' makes gdb catch the signal BEFORE the binary's own
# crash handler. gdb resolves inlines + shows the faulting frame's args (the
# format string + dest buffer) that addr2line cannot. For same-length corruption,
# add a -O0/ASan build or gdb 'watch' on the overflowing buffer.
```

## Corpus-class scope (why this is a 7533 deliverable, not 4086)
🛑 Mass-scan only against the engine's SUPPORTED PAK class. **7533 is the modern
engine meant to run this modern corpus → its crashes are real (the 2 bugs above).**
**4086 is legacy-compat — modern PAKs crash it by design-incompatibility**, so a
full-corpus crash scan on 4086 = expected, never-fixable noise. 4086 is therefore
**ad-hoc only** (run a specific legacy PAK headless when debugging a 4086 bug; no
mass-scan). See `MiSTer_OpenBOR_4086/tools/harness/README.md` +
`feedback_hybrid_core_diff_harness_required.md` (corpus-class principle).

## Status / next
- [x] Pinned Signature A (pp_lexer.c:63 string-literal overflow) + Signature B
      (load_cached_model @cmd/@script translation, openbor.c~17197) via gdb.
- [x] PC OpenBOR.exe (stock v7530) confirm — DONE (see table above). Moscow = real
      crash on pure stock (0xC0000005). Sig-B MGD/Rescue + all Sig-A bail/load on stock
      (crash is patched-build-reachable / fortify-detected latent overflow). Avengers
      ec=1 already shown PAK-identical to the MiSTer install -> contaminated signal.
- [x] MiSTer real-ARM confirm (2026-06-18) — all 3 Sig-B crash on the shipped patched
      ARM build, each dying mid-`load_cached_model` at the exact predicted model:
      Moscow → `'cum'`, Monster Girl Dimensions → `'Luciferpkvored'`, Rescue Command →
      `'w8monstermpfull'` (logs `/media/fat/logs/OpenBOR_7533/OpenBorLog.*.txt`, crash =
      black screen + Master_Daemon respawn). Heaven's Anime Girls (Sig A) LOADS & PLAYS
      on ARM (Level Loaded, 1647 sprites) — the overflow is silent on the non-fortified
      ARM build, confirming Sig A is a latent overflow not a hardware crash today.
- [x] Wrote the 2 engine-robustness fixes in apply_patches.py (step 12a Sig A pp_lexer
      bound + bounded copy; step 12b Sig B load_cached_model newanim NULL-safe + 6
      size_t cut guards). Local dry-run from fresh v7533 clone: EXIT_CODE=0 + all patches
      applied. Outside the LOCKED palette path; apply in BOTH ship + headless builds.
- [ ] Validate: headless harness re-run on the 3 crashers (no crash) → ship ARM artifact
      → hardware-verify 3 crashers load + palette regression (ATOV/TMNT-RP/modern) → ship.
      NOTE: ship blocked until the TEMPORARY fps PROFILE patches are removed (CI gate
      skips commit-back while diagnostic markers present); test via workflow_dispatch
      artifact + manual WinSCP deploy in the meantime.
- 4086 = ad-hoc only (see corpus-class note above) — NOT mass-scanned.
- Render-correctness remains a gap (no open ground-truth; PC OpenBOR.exe is the reference). See `feedback_hybrid_core_diff_harness_required.md`.
