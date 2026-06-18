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
- [ ] PC OpenBOR.exe / real-MiSTer confirm the 7 crashes. Hardware step. (Avengers
      ec=1 already shown to be PAK-identical to the MiSTer install -> contaminated
      signal, not a clean bug; ec=1 is lower-confidence than crashes.)
- 4086 = ad-hoc only (see corpus-class note above) — NOT mass-scanned.
- Render-correctness remains a gap (no open ground-truth; PC OpenBOR.exe is the reference). See `feedback_hybrid_core_diff_harness_required.md`.
