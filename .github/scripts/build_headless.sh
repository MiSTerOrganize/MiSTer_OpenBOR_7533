#!/bin/bash
# build_headless.sh — Build OpenBOR v7533 engine HEADLESS on native x86-64 for
# the diff/debug harness (NOT the MiSTer ARM ship build — that's
# build_mister_arm.sh). Runs on ubuntu-latest in diff_harness.yml. No QEMU, no
# DDR3, no SDL-dummy-DDR3 patch — uses distro SDL2 + the stock upstream
# BUILD_LINUX_LE_x86_64 target, run later with SDL_VIDEODRIVER=dummy.
#
# MILESTONE 1a: prove the engine compiles on x86 with distro deps + stock
# target. No harness, no engine-logic patches yet. Iterate from here.
set +e
set -x

REPO="$(pwd)"   # repo checkout root (for patches/ + headless patcher)

# ── Distro deps (x86-64, no source builds — fast) ──────────────────
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential gcc make pkg-config git python3 \
  libsdl2-dev libsdl2-gfx-dev libpng-dev zlib1g-dev libvorbis-dev libogg-dev \
  libvpx-dev
which gcc pkg-config || { echo "ERROR: toolchain install failed"; exit 1; }
pkg-config --exists sdl2 || { echo "ERROR: libsdl2-dev not found via pkg-config"; exit 1; }
echo "SDL2 cflags: $(pkg-config sdl2 --cflags)"
echo "SDL2 libs:   $(pkg-config sdl2 --libs)"

# ── Clone OpenBOR v7533 (same source as the ship build) ────────────
cd /tmp
rm -rf openbor
git clone --filter=blob:none https://github.com/DCurrent/openbor.git
cd openbor
git checkout v7533
cd engine

# ── version.h (mirror the ship build) ──────────────────────────────
cat > version.h << 'VERSIONEOF'
#ifndef VERSION_H
#define VERSION_H
#define VERSION_NAME "OpenBOR"
#define VERSION_MAJOR "4"
#define VERSION_MINOR "0"
#define VERSION_BUILD "7533"
#define VERSION "v"VERSION_MAJOR"."VERSION_MINOR" Build "VERSION_BUILD
#endif
VERSIONEOF

# ── POSIX compat (mirror ship build) ───────────────────────────────
sed -i 's/stricmp/strcasecmp/g' openbor.h

# ── Neutralize -Werror for the newer ubuntu-latest GCC ─────────────
# The ship build uses bullseye GCC (older, lenient); ubuntu-latest GCC 13/14
# promotes more warnings (unused-result/address/enum-int-mismatch) to errors
# under the target's bare -Werror. Disable error-promotion (bare -Werror ->
# -Wno-error; -Werror=X -> -Wno-error=X) so the stock source compiles. This is
# a HEADLESS-only diagnostic build; the ship build's warning posture is unchanged.
sed -i 's/-Werror/-Wno-error/g' Makefile

# ── Native writer headers (header resolution for apply_patches.py; the .o is
#    BUILD_MISTER-gated so it is NOT compiled into the x86_64 headless binary) ─
cp "$REPO/src/native_video_writer.c" . 2>/dev/null || true
cp "$REPO/src/native_video_writer.h" . 2>/dev/null || true
cp "$REPO/src/native_audio_writer.c" . 2>/dev/null || true
cp "$REPO/src/native_audio_writer.h" . 2>/dev/null || true

# ── Layer the SHIP engine-logic patches so the harness tests OUR build ──
#    (palette pipeline, stale-pointer fixes, screen_status, range defaults,
#    loadsprite hash, the PLAYER_MIN_Z/etc. script constants — the last of
#    which is why 49 stock-headless PAKs exit 1 on "Can't find openbor constant").
#    The MiSTer-infra bits (DDR3 main, video DDR3 write, native_video_writer.o)
#    are all ifdef BUILD_MISTER / MISTER_NATIVE_VIDEO, NOT active for the x86_64
#    target — so they're patched in but not compiled. apply_patches_headless.py
#    then overrides main() + the video present hook for the headless run.
echo "=== apply_patches.py (ship engine-logic patches, OB_HEADLESS=1) ==="
OB_HEADLESS=1 python3 "$REPO/.github/scripts/apply_patches.py" /tmp/openbor/engine "$REPO/patches"
SRC=$?
if [ $SRC -ne 0 ]; then
  echo "ERROR: apply_patches.py failed (rc=$SRC)"; exit 1
fi

# ── Apply headless harness overrides (main + video frame-counter) ──
echo "=== apply_patches_headless.py ==="
python3 "$REPO/.github/scripts/apply_patches_headless.py" /tmp/openbor/engine "$REPO/patches"
HRC=$?
if [ $HRC -ne 0 ]; then
  echo "ERROR: apply_patches_headless.py failed (rc=$HRC)"; exit 1
fi

# ── Build: stock upstream x86-64 Linux target ──────────────────────
echo "=== make BUILD_LINUX_LE_x86_64=1 ==="
make BUILD_LINUX_LE_x86_64=1 -j$(nproc)
RC=$?
echo "make rc=$RC"

echo "=== build output ==="
ls -lh OpenBOR.elf OpenBOR 2>/dev/null
if [ -f OpenBOR.elf ] || [ -f OpenBOR ]; then
  echo "HEADLESS BUILD OK (milestone 1a)"
  file OpenBOR.elf 2>/dev/null
  cp -f OpenBOR.elf /tmp/OpenBOR_headless 2>/dev/null || cp -f OpenBOR /tmp/OpenBOR_headless 2>/dev/null
else
  echo "HEADLESS BUILD FAILED — see make output above"
  exit 1
fi
