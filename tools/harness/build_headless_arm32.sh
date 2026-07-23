#!/bin/bash
# build_headless_arm32.sh — Build OpenBOR_headless for arm32 (Cortex-A9 class,
# armhf) locally in Docker. The arm32 counterpart of the x86-64 CI build
# (.github/scripts/build_headless.sh): same clone, same ship engine-logic
# patches (apply_patches.py OB_HEADLESS=1), same headless harness overrides
# (apply_patches_headless.py) — compiled for the ARM CPU class the shipped
# binary runs on, with the same Debian bullseye gcc-10 toolchain the ship
# build uses.
#
# This is a DEBUG/VERIFY harness build only. It never ships anything — the
# distributed ARM binary comes exclusively from the repo's build.yml CI.
#
# Run INSIDE an arm32v7/debian:bullseye-slim container with the repo mounted
# at /build (start the container from PowerShell so the -v mounts survive):
#
#   docker run -d --name obarm --platform linux/arm/v7 \
#     -v "<repo>:/build" -v "<paks>:/paks:ro" \
#     arm32v7/debian:bullseye-slim sleep 14400
#   docker exec obarm bash /build/tools/harness/build_headless_arm32.sh
#
# Outputs (inside the container; extract with docker cp):
#   /tmp/OpenBOR_headless_arm32   unstripped armhf ELF (symbols kept for
#                                 addr2line/GDB crash forensics)
#   /tmp/oblibs_arm32/            non-glibc runtime .so bundle — needed to run
#                                 the dynamic binary on a device that lacks
#                                 distro SDL2/vpx/vorbis/png libs:
#                                 LD_LIBRARY_PATH=<bundle> ./OpenBOR_headless
#
# The binary runs under QEMU (docker exec, clean container filesystem) AND on
# real arm32 hardware over the same bullseye-era glibc (2.31) — the
# environment-vs-code discriminator: identical binary, two filesystems.
set +e
set -x

REPO=${REPO:-/build}

# ── Toolchain + distro deps (bullseye armhf; gcc-10 = ship toolchain) ──
APTOPT="-o Acquire::Retries=5 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30"
apt-get $APTOPT update -qq
apt-get $APTOPT install -y -qq build-essential gcc make pkg-config git ca-certificates \
  python3 libsdl2-dev libsdl2-gfx-dev libpng-dev zlib1g-dev libvorbis-dev libogg-dev \
  libvpx-dev
which gcc pkg-config || { echo "ERROR: toolchain install failed"; exit 1; }
pkg-config --exists sdl2 || { echo "ERROR: libsdl2-dev not found via pkg-config"; exit 1; }
echo "gcc: $(gcc -dumpversion)  SDL2: $(pkg-config sdl2 --modversion)"

# ── Clone OpenBOR v7533 (same source as the ship + x86 headless builds) ─
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

# ── Neutralize -Werror (same guard as the x86 headless build; bullseye
#    gcc-10 is lenient but this keeps the two headless builds identical) ─
sed -i 's/-Werror/-Wno-error/g' Makefile

# ── Native writer headers (header resolution for apply_patches.py; the .o is
#    BUILD_MISTER-gated so it is NOT compiled into this headless binary) ─
cp "$REPO/src/native_video_writer.c" . 2>/dev/null || true
cp "$REPO/src/native_video_writer.h" . 2>/dev/null || true
cp "$REPO/src/native_audio_writer.c" . 2>/dev/null || true
cp "$REPO/src/native_audio_writer.h" . 2>/dev/null || true

# ── Ship engine-logic patches (OB_HEADLESS=1 skips MiSTer-infra-only) ──
echo "=== apply_patches.py (ship engine-logic patches, OB_HEADLESS=1) ==="
OB_HEADLESS=1 python3 "$REPO/.github/scripts/apply_patches.py" /tmp/openbor/engine "$REPO/patches"
SRC=$?
if [ $SRC -ne 0 ]; then
  echo "ERROR: apply_patches.py failed (rc=$SRC)"; exit 1
fi

# ── Headless harness overrides (main + video frame-counter) ────────
echo "=== apply_patches_headless.py ==="
python3 "$REPO/.github/scripts/apply_patches_headless.py" /tmp/openbor/engine "$REPO/patches"
HRC=$?
if [ $HRC -ne 0 ]; then
  echo "ERROR: apply_patches_headless.py failed (rc=$HRC)"; exit 1
fi

# ── Build: upstream arm Linux target + the ship build's CPU flags ──
# BUILD_LINUX_LE_arm is the stock upstream armhf/SDL2 target (no OpenGL,
# distro headers under /usr/include). It sets no ARCHFLAGS of its own, so the
# command-line ARCHFLAGS below injects the exact Cortex-A9 CPU flags the ship
# BUILD_MISTER target uses.
echo "=== make BUILD_LINUX_LE_arm=1 (cortex-a9 hard-float neon) ==="
make BUILD_LINUX_LE_arm=1 \
  ARCHFLAGS="-mcpu=cortex-a9 -mtune=cortex-a9 -mfloat-abi=hard -mfpu=neon" \
  -j"$(nproc)"
RC=$?
echo "make rc=$RC"

echo "=== build output ==="
ls -lh OpenBOR.elf OpenBOR 2>/dev/null
if [ ! -f OpenBOR.elf ] && [ ! -f OpenBOR ]; then
  echo "ARM32 HEADLESS BUILD FAILED — see make output above"
  exit 1
fi

# Keep the UNSTRIPPED .elf (addr2line/GDB need the symbols).
cp -f OpenBOR.elf /tmp/OpenBOR_headless_arm32 2>/dev/null || cp -f OpenBOR /tmp/OpenBOR_headless_arm32
file /tmp/OpenBOR_headless_arm32

# ── Collect the non-glibc runtime .so bundle for on-device runs ────
# The binary links distro SDL2/gfx/vpx/vorbis/ogg/png/z dynamically; a
# Buildroot-style device has glibc 2.31 (== bullseye) but not those libs.
# Bundle every resolved .so EXCEPT the glibc family, then run on-device with
# LD_LIBRARY_PATH pointing at the bundle.
rm -rf /tmp/oblibs_arm32
mkdir -p /tmp/oblibs_arm32
ldd /tmp/OpenBOR_headless_arm32 | awk '$3 ~ /^\// {print $3}' | while read -r so; do
  base=$(basename "$so")
  case "$base" in
    libc.so*|libm.so*|libpthread.so*|libdl.so*|librt.so*|ld-linux*|libresolv.so*) ;;
    *) cp -Lf "$so" /tmp/oblibs_arm32/ ;;
  esac
done
ls -lh /tmp/oblibs_arm32/
echo "ARM32 HEADLESS BUILD OK"
echo "  binary: /tmp/OpenBOR_headless_arm32"
echo "  libs:   /tmp/oblibs_arm32/"
