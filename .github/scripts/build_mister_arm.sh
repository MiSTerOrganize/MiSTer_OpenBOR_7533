#!/bin/bash
# build_mister_arm.sh — Build OpenBOR 3979 ARM binary for MiSTer
#
# Runs inside arm32v7/debian:bullseye-slim Docker container.
# Called by GitHub Actions CI workflow.
#
# Expects /build to be mounted from the repo checkout.
set +e

SDL_PREFIX=/tmp/sdl12

apt-get update -qq
apt-get install -y -qq gcc g++ make wget git python3
if ! which wget >/dev/null 2>&1; then echo "ERROR: apt-get install failed — wget not found"; exit 1; fi
apt-get clean

# ── Build SDL 1.2.15 (custom dummy that writes to DDR3) ──────────
# Per CLAUDE.md: no ALSA, no real fbcon. We patch SDL's "dummy"
# video driver in-place so its UpdateRects hook converts the final
# composited SDL surface to RGB565 and writes it to the DDR3 ring
# the FPGA reads. This way OpenBOR runs through its full SDL
# pipeline (SDL_BlitSurface, format conversion, etc.) and we tap
# the LAST stage with already-converted pixels in a known SDL
# format -- avoids OpenBOR's per-render-path quirks (8-bit-mode
# blend bugs, etc.) that bit us when intercepting at video_copy_screen.
echo "=== Building SDL 1.2.15 ==="
cd /tmp
wget -q https://www.libsdl.org/release/SDL-1.2.15.tar.gz
tar xzf SDL-1.2.15.tar.gz
cd SDL-1.2.15

# Patch the dummy video driver -- this is what runs when
# SDL_VIDEODRIVER=dummy. Inject DDR3 mmap + UpdateRects writer.
python3 /build/.github/scripts/patch_sdl_dummy.py src/video/dummy/SDL_nullvideo.c

./configure \
  --prefix=$SDL_PREFIX \
  --disable-video-x11 \
  --disable-video-opengl \
  --disable-cdrom \
  --disable-shared \
  --enable-static \
  --disable-pulseaudio \
  --disable-esd \
  --disable-alsa \
  --disable-video-fbcon \
  --enable-video-dummy \
  --quiet
make -j$(nproc) --quiet
make install --quiet

# ── Build SDL_gfx 2.0.26 ─────────────────────────────────────────
echo "=== Building SDL_gfx 2.0.26 ==="
cd /tmp
wget -q https://www.ferzkopp.net/Software/SDL_gfx-2.0/SDL_gfx-2.0.26.tar.gz
tar xzf SDL_gfx-2.0.26.tar.gz
cd SDL_gfx-2.0.26
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
rm -rf SDL-1.2.15 SDL-1.2.15.tar.gz
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

# ── Clone exact OpenBOR r4086 from DCurrent's GitHub ─────────────
# r4086 is the most popular build for the ~300-game community packs.
# SourceForge SVN is frozen; DCurrent/openbor on GitHub has the full
# history. r4086 = 52 commits after r4034 ("Removed vaulting code")
# = commit af23dc9c. Source lives under engine/ subdirectory.
echo "=== Cloning OpenBOR r4086 ==="
cd /tmp
git clone --filter=blob:none https://github.com/DCurrent/openbor.git
cd openbor
git checkout af23dc9c
cd engine

# ── Set version ──────────────────────────────────────────────────
cat > version.h << 'VERSIONEOF'
#ifndef VERSION_H
#define VERSION_H
#define VERSION_NAME "OpenBOR"
#define VERSION_MAJOR "3"
#define VERSION_MINOR "0"
#define VERSION_BUILD "4086"
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

# ── Apply Makefile patches ───────────────────────────────────────
python3 /build/.github/scripts/apply_patches.py /tmp/openbor/engine /build/patches

# ── Build ────────────────────────────────────────────────────────
echo "=== Building OpenBOR for MiSTer ==="
make BUILD_MISTER=1 SDL_PREFIX=$SDL_PREFIX -j$(nproc)

echo "=== Binary info ==="
ls -lh OpenBOR

# ── Copy result back to mounted volume ───────────────────────────
cp OpenBOR /build/OpenBOR
echo "=== Build complete ==="
