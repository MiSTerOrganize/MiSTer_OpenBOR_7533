# OpenBOR diff / debug harness

The OpenBOR counterpart of the PICO-8 diff harness (`MiSTer_PICO-8/tools/harness/`).
Finds engine bugs by exercising PAKs off-device and classifying failures, using the
same category model as PICO-8 (see workspace `#BENCHMARK_TOOLS.md` section C +
`feedback_hybrid_core_diff_harness_required.md`). Shared between OpenBOR_7533 and
OpenBOR_4086 (same PAK format + engine family); edit in 7533, mirror to 4086.

## Status

| Category | Status | Tool |
|---|---|---|
| **Decode** (PAK-integrity) | ✅ DONE — 450/450 local corpus clean | `pak_decode_scan.py` |
| **Headless build** | ✅ DONE — compiles+links on x86 (`diff_harness.yml`) | `build_headless.sh` |
| **arm32 on-target build** | ✅ DONE 2026-07-23 — local Docker (arm32v7 bullseye, ship gcc-10 + Cortex-A9 flags); runs under QEMU AND on real arm32 hardware | `build_headless_arm32.sh` |
| **Crashes** | ✅ FUNCTIONAL — SIGSEGV/BUS/ABRT/FPE backtrace→addr2line | `apply_patches_headless.py` + `pak_run_scan.sh` |
| **Hangs** (SIGALRM wall-clock, re-armed/frame) | ✅ FUNCTIONAL | same |
| **Input-fed mass-scan** | 🏗️ (basic run works; generic input-feed TODO) | `pak_run_scan.sh` |
| **Preprocess** (script/model parse, #include) | 🏗️ planned (engine-logic patches layer) | (planned) |
| **Render-correctness** | 🔴 GAP — no open ground-truth (PC OpenBOR.exe only) | — |

**Runner:** `OpenBOR_headless` is **dynamic glibc+SDL2** (unlike PICO-8's fully-static z8headless that runs on the musl docker-desktop WSL). It runs in an **`ubuntu:24.04` glibc container matching the `diff_harness.yml` build env** — `docker run` mounting the PAK corpus + binary + an out dir, executing `pak_run_scan.sh`. This is *running* a diagnostic in a container, not building (the no-local-Docker rule is about the ARM ship build). Milestone 1b verified: A Tale of Vengeance → 60 frames → exit 0.

## arm32 on-target build — `build_headless_arm32.sh`

The arm32 (Cortex-A9 class) counterpart of `build_headless.sh`: same clone + same
ship engine-logic patches (`apply_patches.py OB_HEADLESS=1`) + same headless
overrides (`apply_patches_headless.py`), compiled inside an
`arm32v7/debian:bullseye-slim` container (the ship build's own toolchain, gcc-10)
via the stock upstream `BUILD_LINUX_LE_arm` target with the ship CPU flags
(`-mcpu=cortex-a9 -mfloat-abi=hard -mfpu=neon`). Debug/verify harness only —
never a ship artifact.

```
docker run -d --name obarm --platform linux/arm/v7 \
  -v "<repo>:/build" -v "<paks>:/paks:ro" arm32v7/debian:bullseye-slim sleep 14400
docker exec obarm bash /build/tools/harness/build_headless_arm32.sh
```

Outputs `/tmp/OpenBOR_headless_arm32` (unstripped, symbols kept for
addr2line/GDB) + `/tmp/oblibs_arm32/` (the non-glibc runtime `.so` bundle —
the binary is dynamic; a Buildroot-style arm device has bullseye-matching
glibc 2.31 but not distro SDL2/vpx/vorbis/png, and note `libnsl.so.2` is a
separate bullseye package, not part of glibc, so it belongs in the bundle too).
Run it two ways:

- **Under QEMU** (clean container filesystem): `docker exec` with `OB_PAK`/
  `OB_FRAMES`/`OB_ALARM` env, exactly like the x86 runner.
- **On real arm32 hardware** (real filesystem): copy binary + bundle over, then
  `LD_LIBRARY_PATH=<bundle> OB_PAK=<pak> ./OpenBOR_headless_arm32`. The headless
  main forces SDL dummy video/audio, so it never touches an FPGA, framebuffer,
  or running processes.

Same binary on both = the environment-vs-code discriminator: identical output →
the bug is elsewhere; divergent output → it's device filesystem state, not code.
Verified 2026-07-23: A Tale of Vengeance → 60 frames → exit 0 on BOTH sides.

## Decode category (done) — `pak_decode_scan.py`

```
python tools/harness/pak_decode_scan.py <paks_root> [out_file]
```
Reads every PAK by the canonical packfile format (upstream `engine/source/gamelib/
packfile.c/.h` directory layout — the same the engine's reader walks) and validates
structural integrity: directory offset, per-entry `pns_len`, `filestart+filesize`
within EOF (truncation), name decodability. A PAK our reader chokes on = one the
engine chokes on too. No headless engine needed. Local corpus
(`OpenBOR_Paks/Paks`, ~450 PAKs): 450/450 clean.

This is the OpenBOR analog of PICO-8's shrinko8 decode-differential — except
PAKs are packfile *archives*, not PXA-compressed code, so "decode" = archive
integrity rather than a two-tool decompressor diff (there is no "shrinko8 for
OpenBOR"; the packfile format spec IS the reference).

## Crash / hang / render categories — headless build plan (the remaining arc)

OpenBOR has NO SDL-free core lib (unlike zepto8core), so the headless harness is a
real build-out, iterated via a `diff_harness.yml` CI workflow (native x86 ubuntu,
no QEMU — OpenBOR compiles on Linux/PC natively). Plan:

1. **`diff_harness.yml`**: clone DCurrent/openbor v7533 (v4086 for the sister), apply
   our ENGINE-LOGIC patches only (palette pipeline, stale-pointer fixes,
   screen_status normalize, range defaults, loadsprite hash — the behaviors we ship
   and want to test), NOT the MiSTer-infra patches (DDR3 `main()`, video-DDR3).
2. **Headless video sink**: a headless `native_video_writer.c` whose `WriteFrame`
   captures the latest frame to memory (PNG dump on demand) instead of mmapping
   `/dev/mem` at 0x3A000000. Hook the same `video_copy_screen` point the MiSTer
   build patches.
3. **Headless driver / main**: replace the MiSTer OSD/`.s0` `main()` with one that
   takes `--pak <path> --frames N --dump <list> [--alarm SECS]`, mounts the PAK,
   runs N frames of the engine loop, dumps frames, exits. Crash handler
   (SIGSEGV/SIGABRT/SIGFPE → backtrace → addr2line) + `--alarm` wall-clock SIGALRM
   for hangs (engine-agnostic; catches both C and script-VM infinite loops — most
   OpenBOR engine work is in the load path, so a "load every PAK headless" sweep
   directly exercises our patches).
4. **Preprocess**: once headless, diff the engine's parsed model/script output (post
   `#include` / model-load) for structural consistency across the PAK library.
5. **Mass-scan**: `pak_scan.sh` runs `openbor_headless` over every PAK, classifies
   crash / hang / black / animates (mirror of PICO-8 `scan_library.sh`).

Render-correctness stays a GAP (no open reference renderer; PC OpenBOR.exe is the
only ground truth — the same situation as PICO-8 needing the official binary).

Expect multiple CI cycles to get the headless build green (OpenBOR is finicky:
GCC UB flags, SDL2 deps, monolithic main). This mirrors how the PICO-8 headless
harness took an extended arc to land.
