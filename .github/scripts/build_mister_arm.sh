#!/bin/bash
# build_mister_arm.sh — Build OpenBOR 4.0 Build 7533 ARM binary for MiSTer
#
# Runs inside arm32v7/debian:bullseye-slim Docker container.
# Called by GitHub Actions CI workflow.
#
# Expects /build to be mounted from the repo checkout.
set +e

SDL_PREFIX=/tmp/sdl2

apt-get update -qq
apt-get install -y -qq gcc g++ make wget git python3 pkg-config autoconf automake libtool
if ! which wget >/dev/null 2>&1; then echo "ERROR: apt-get install failed — wget not found"; exit 1; fi
apt-get clean

# ── Build SDL 2.0.8 (custom dummy that writes to DDR3) ──────────
# Per CLAUDE.md: no ALSA, no real fbcon. We patch SDL2's "dummy"
# video driver in-place so its FrameBuffer hook converts the final
# composited SDL surface to RGB565 and writes it to the DDR3 ring
# the FPGA reads.
#
# SDL 2.0.8 pinned per OpenBOR upstream — newer SDL2 versions cause
# instability (DCurrent reverted past this version).
echo "=== Building SDL 2.0.8 ==="
cd /tmp
wget -q https://www.libsdl.org/release/SDL2-2.0.8.tar.gz
tar xzf SDL2-2.0.8.tar.gz
cd SDL2-2.0.8

# Patch the dummy video driver -- this is what runs when
# SDL_VIDEODRIVER=dummy. Inject DDR3 mmap + frame writer.
python3 /build/.github/scripts/patch_sdl_dummy.py src/video/dummy/SDL_nullframebuffer.c

./configure \
  --prefix=$SDL_PREFIX \
  --disable-video-x11 \
  --disable-video-wayland \
  --disable-video-opengl \
  --disable-video-opengles \
  --disable-video-vulkan \
  --disable-video-kmsdrm \
  --disable-video-rpi \
  --disable-video-directfb \
  --disable-video-cocoa \
  --disable-shared \
  --enable-static \
  --disable-pulseaudio \
  --disable-esd \
  --disable-alsa \
  --disable-jack \
  --disable-arts \
  --disable-nas \
  --disable-sndio \
  --disable-fusionsound \
  --disable-libsamplerate \
  --quiet
make -j$(nproc) --quiet
make install --quiet

# Hard-fail if SDL2 headers didn't install. The downstream SDL2_gfx
# and OpenBOR builds need these — easier to debug here than to chase
# cascading "SDL.h: No such file" errors.
test -f $SDL_PREFIX/include/SDL2/SDL.h || { echo "ERROR: SDL2 build/install failed — SDL.h not present"; exit 1; }
test -f $SDL_PREFIX/lib/libSDL2.a || { echo "ERROR: SDL2 build/install failed — libSDL2.a not present"; exit 1; }

# ── Build SDL2_gfx 1.0.4 ─────────────────────────────────────────
echo "=== Building SDL2_gfx 1.0.4 ==="
cd /tmp
wget -q https://www.ferzkopp.net/Software/SDL2_gfx/SDL2_gfx-1.0.4.tar.gz
tar xzf SDL2_gfx-1.0.4.tar.gz
cd SDL2_gfx-1.0.4
./autogen.sh 2>/dev/null
# Use sdl2-config from our SDL2 install so headers/libs resolve.
export SDL2_CONFIG=$SDL_PREFIX/bin/sdl2-config
./configure \
  --prefix=$SDL_PREFIX \
  --disable-shared \
  --enable-static \
  --with-sdl-prefix=$SDL_PREFIX \
  --disable-sdltest \
  --disable-mmx \
  --quiet
make -j$(nproc) --quiet
make install --quiet
unset SDL2_CONFIG
test -f $SDL_PREFIX/lib/libSDL2_gfx.a || { echo "ERROR: SDL2_gfx build/install failed — libSDL2_gfx.a not present"; exit 1; }

# ── Build libogg 1.3.5 ───────────────────────────────────────────
echo "=== Building libogg ==="
cd /tmp
wget -q https://downloads.xiph.org/releases/ogg/libogg-1.3.5.tar.gz
tar xzf libogg-1.3.5.tar.gz
cd libogg-1.3.5
./configure --prefix=$SDL_PREFIX --disable-shared --enable-static --quiet
make -j$(nproc) --quiet
make install --quiet

# ── Build libvorbis 1.3.7 ────────────────────────────────────────
echo "=== Building libvorbis ==="
cd /tmp
wget -q https://downloads.xiph.org/releases/vorbis/libvorbis-1.3.7.tar.gz
tar xzf libvorbis-1.3.7.tar.gz
cd libvorbis-1.3.7
./configure --prefix=$SDL_PREFIX --disable-shared --enable-static --with-ogg=$SDL_PREFIX --quiet
make -j$(nproc) --quiet
make install --quiet
cd /tmp && rm -rf libvorbis-1.3.7 libvorbis-1.3.7.tar.gz
rm -rf SDL2-2.0.8 SDL2-2.0.8.tar.gz
rm -rf SDL2_gfx-1.0.4 SDL2_gfx-1.0.4.tar.gz
rm -rf libogg-1.3.5 libogg-1.3.5.tar.gz

# ── Build zlib 1.2.13 ────────────────────────────────────────────
echo "=== Building zlib ==="
cd /tmp
wget -q https://zlib.net/fossils/zlib-1.2.13.tar.gz || wget -q https://github.com/madler/zlib/releases/download/v1.2.13/zlib-1.2.13.tar.gz
if [ ! -f zlib-1.2.13.tar.gz ]; then echo "ERROR: zlib download failed"; exit 1; fi
tar xzf zlib-1.2.13.tar.gz
cd zlib-1.2.13
./configure --prefix=$SDL_PREFIX --static
make -j$(nproc) --quiet
make install --quiet

# ── Build libpng 1.6.39 ──────────────────────────────────────────
echo "=== Building libpng ==="
cd /tmp
wget -q https://download.sourceforge.net/libpng/libpng-1.6.39.tar.gz
tar xzf libpng-1.6.39.tar.gz
cd libpng-1.6.39
CPPFLAGS="-I$SDL_PREFIX/include" LDFLAGS="-L$SDL_PREFIX/lib" \
./configure --prefix=$SDL_PREFIX --disable-shared --enable-static --quiet
make -j$(nproc) --quiet
make install --quiet
cd /tmp && rm -rf zlib-1.2.13 zlib-1.2.13.tar.gz libpng-1.6.39 libpng-1.6.39.tar.gz

# ── Clone OpenBOR Build 7533 from DCurrent's GitHub ──────────────
# Build 7533 is the latest stable OpenBOR 4.0 release (May 2025).
# Required for modern fangames (Rescue-Palooza, Final Fight LNS,
# Avengers UBF v2.7+, Zvitor arcade ports, RVGM set). Backward
# compatible with PAK collections built for 4086.
# Source lives under engine/ subdirectory.
echo "=== Cloning OpenBOR Build 7533 ==="
cd /tmp
git clone --filter=blob:none https://github.com/DCurrent/openbor.git
cd openbor
git checkout v7533
cd engine

# ── Set version ──────────────────────────────────────────────────
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

# ── Apply POSIX compat patch ────────────────────────────────────
sed -i 's/stricmp/strcasecmp/g' openbor.h

# ── Copy native_video_writer + native_audio_writer into source tree ─
cp /build/src/native_video_writer.c .
cp /build/src/native_video_writer.h .
cp /build/src/native_audio_writer.c .
cp /build/src/native_audio_writer.h .

# ── DIAG (2026-06-10): a build shipped a binary lacking native_video_writer.c
# source changes despite the cp above (binary had no [VCP]/[VCV] markers).
# Print exactly what is about to compile, flag duplicates, and force a fresh
# object so no stale .o can be linked.
echo "=== NVW DIAG: pwd=$(pwd) ==="
sha256sum native_video_writer.c native_audio_writer.c
echo "NVW_VCP_COUNT=$(grep -c VCP native_video_writer.c)"
echo "NVW dupes under /tmp:"; find /tmp -name 'native_video_writer.*' 2>/dev/null
rm -f native_video_writer.o native_audio_writer.o
echo "=== END NVW DIAG ==="

# ── Apply Makefile + source patches ──────────────────────────────
# CRITICAL: hard-fail if apply_patches.py errors. Previously `set +e`
# at the top let silent patch failures through — CI claimed "success"
# but shipped a partially-patched binary (steps 1-3 applied, step 4+
# silently skipped on a RuntimeError). Bit us hard during the ATOV
# palette fix when v2's sprite.c pattern didn't match upstream's
# `drawmethod->flipx` field (it expected the renamed `config & FLIP_X`
# form). Diagnosing that took an extra deploy+test cycle that wouldn't
# have happened if CI had failed loudly. Make CI loud about it.
python3 /build/.github/scripts/apply_patches.py /tmp/openbor/engine /build/patches
PATCHES_RC=$?
if [ $PATCHES_RC -ne 0 ]; then
    echo "ERROR: apply_patches.py failed with exit code $PATCHES_RC — refusing to ship a partially-patched binary"
    exit 1
fi

# ── Build ────────────────────────────────────────────────────────
echo "=== Building OpenBOR for MiSTer ==="
make BUILD_MISTER=1 SDL_PREFIX=$SDL_PREFIX -j$(nproc)

echo "=== Binary info ==="
ls -lh OpenBOR

# DIAG (2026-06-10): grep the freshly-built binary IN CI. If [VCP] appears
# here but NOT in the downloaded artifact -> upload/download issue. If absent
# here too -> the compile/link genuinely drops it. BIN_MD5 lets us compare to
# the locally-downloaded md5.
echo "=== FINAL BINARY [VCP] CHECK ==="
echo "BIN_VCP_OCCURRENCES=$(grep -ao 'VCP' OpenBOR 2>/dev/null | wc -l)"
echo "BIN_WFMARKER=$(grep -ao 'WFMARKER' OpenBOR 2>/dev/null | head -1)"
echo "BIN_DEINT_OCC=$(grep -ao 'deint=' OpenBOR 2>/dev/null | wc -l)"
echo "BIN_MD5=$(md5sum OpenBOR | awk '{print $1}')"
echo "--- object-file forensics (compile-drop vs link-drop) ---"
ls -la native_video_writer.o 2>/dev/null
echo "OBJ_STRINGS=$(strings -n 3 native_video_writer.o 2>/dev/null | grep -iE 'vcp|deint|wfmarker|mapped|pinned' | tr '\n' '|')"
echo "BIN_STRINGS=$(strings -n 3 OpenBOR 2>/dev/null | grep -iE 'vcp|deint|wfmarker' | tr '\n' '|')"
echo "BIN_HAS_MAPPED=$(grep -ao 'NativeVideoWriter: mapped' OpenBOR 2>/dev/null | head -1)"
echo "=== END FINAL BINARY [VCP] CHECK ==="

# ── Copy result back to mounted volume ───────────────────────────
cp OpenBOR /build/OpenBOR
# Unstripped binary (Makefile TARGET = OpenBOR.elf, built with -g per our
# CFLAGS) for crash_symbolize.py. The SHIPPED binary stays the stripped
# OpenBOR; this copy is artifact-only and is never committed/shipped.
if [ -f OpenBOR.elf ]; then
    cp OpenBOR.elf /build/OpenBOR_unstripped
    echo "unstripped OpenBOR.elf captured ($(ls -lh OpenBOR.elf | awk '{print $5}'))"
else
    echo "WARN: OpenBOR.elf (unstripped TARGET) not found -- crash symbolization unavailable for this build"
fi
echo "=== Build complete ==="
