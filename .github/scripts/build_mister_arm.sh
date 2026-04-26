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

# ── Apply Makefile + source patches ──────────────────────────────
python3 /build/.github/scripts/apply_patches.py /tmp/openbor/engine /build/patches

# ── Build ────────────────────────────────────────────────────────
echo "=== Building OpenBOR for MiSTer ==="
make BUILD_MISTER=1 SDL_PREFIX=$SDL_PREFIX -j$(nproc)

echo "=== Binary info ==="
ls -lh OpenBOR

# ── Copy result back to mounted volume ───────────────────────────
cp OpenBOR /build/OpenBOR
echo "=== Build complete ==="
