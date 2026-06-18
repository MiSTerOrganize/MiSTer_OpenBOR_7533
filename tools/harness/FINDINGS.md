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

### Signature B — `load_models → sprintf` SIGSEGV (buffer-overflow class)
Chain: `openborMain (openbor.c:51423) → startup (47309) → load_models (18491) →
sprintf` → SIGSEGV inside the libc sprintf. A model/path string formatted with a
bad/oversized argument during PAK model load. Clean backtrace.
- Monster Girl Dimensions.pak
- Moscow RE-Action.pak
- Rescue Command - Against the Amazon Girls.pak

### Signature A — `drawstatus` / stack-corruption crash
Backtrace shows wild/tiny return addresses (0x1e, 0xdf, 0x11c) = a stack smash /
call through a garbage pointer; nearest resolved frame is `drawstatus`
(openbor.c:22881). Exact faulting write needs deeper forensics. On STOCK these
two of them (Heaven's, Hiryu) crashed earlier in the script parser
(`Script_AppendText → Parser_*` deep recursion); on the patched build they
compile further and crash here.
- Heaven's Anime Girls.pak
- Hiryu No Ken [Demo].pak
- Memory Loss.pak
- Ogres Mayhem.pak

## Script-compile-fail (ec=1) on patched build — 12 PAKs (candidate script-API gaps)

These compile-fail during PAK script compile = the **preprocess category** signal.
⚠️ NOT confirmed bugs: some may be PAK-version differences vs the corpus copy, or
ship-build gaps. Need hardware/PC confirmation. Notable: **Avengers** fails on
`changeentityproperty` (deadpool `takedamagescript`) yet is "confirmed working"
on the dev MiSTer — strongly suggests a PAK-version difference; verify the
installed Avengers PAK == corpus copy.
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

## Status / next
- [ ] PC OpenBOR.exe / real-MiSTer confirm the 7 crashes + spot-check the 12 ec=1 (esp. Avengers PAK-version). Hardware step.
- [ ] Pin Signature B sprintf overflow (extract the offending PAK's model/path) and Signature A memory smash.
- [ ] Adapt the headless build to OpenBOR_4086 (v4086 upstream) — closes 4086's diff-harness gap.
- Render-correctness remains a gap (no open ground-truth; PC OpenBOR.exe is the reference). See `feedback_hybrid_core_diff_harness_required.md`.
