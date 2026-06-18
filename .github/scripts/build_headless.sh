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

# ── Distro deps (x86-64, no source builds — fast) ──────────────────
sudo apt-get update -qq
sudo apt-get install -y -qq build-essential gcc make pkg-config git python3 \
  libsdl2-dev libsdl2-gfx-dev libpng-dev zlib1g-dev libvorbis-dev libogg-dev
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
