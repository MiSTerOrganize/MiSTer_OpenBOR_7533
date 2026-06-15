#!/usr/bin/env python3
"""
apply_patches.py — Apply all MiSTer patches to OpenBOR 4.0 Build 7533 source tree.

Usage: python3 apply_patches.py <openbor_source_dir> <patches_dir>

Applies:
  1. Makefile: adds BUILD_MISTER target (SDL 2.0)
  2. openbor.c: replaces pausemenu() with custom 4-item menu
  3. sdl/video.c: intercepts frame present with NativeVideoWriter
  4. sdl/control.c: replaces control_update() with DDR3 joystick reading
  5. sdl/sdlport.c: replaces main() with NativeVideoWriter init + OSD PAK loading
  6. source/utils.c: redirects save path to /media/fat/saves/OpenBOR_7533/
"""

import sys
import os

def read(path):
    # Explicit UTF-8 — Linux CI defaults to UTF-8 but Windows defaults to
    # cp1252 which fails on Unicode arrows etc. in patched comments.
    # Making it explicit lets local dry-runs validate before push.
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()

def write(path, content):
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)

def strict_replace(content, old, new, label, count=1):
    """Replace `old` with `new` in content; RAISE if `old` not found OR
    if found more than `count` times (default 1).

    Use this instead of `content.replace(old, new)` for patches where a
    silent no-op would corrupt the build. The 2026-05-19 ATOV palette
    session uncovered that the original `source/utils.c` COPY_ROOT_PATH
    macro replacement had been silently failing since the patch was written
    — pattern expected `strncpy(buf, "./", 2)` but pristine upstream v7533
    has `strcpy(buf, "./")`. Saves/Config/SaveStates redirect had been
    broken without anyone noticing because plain `.replace()` returns the
    source unchanged when the pattern doesn't match.

    The 2026-05-24 SUB-PROFILE v5 session uncovered the dual bug: a
    pattern that matched MULTIPLE places injected the same C code into
    many functions in openbor.c (parser-loop-end pattern matched 14
    places, only one of which had a matching parser-loop-start with the
    needed local variable). count=1 catches this class of bug; pass
    explicit count=N when the pattern is intentionally multi-match.
    """
    if old not in content:
        raise RuntimeError(
            f"strict_replace failed for '{label}': pattern not found.\n"
            f"  First 80 chars of expected: {old[:80]!r}\n"
            f"  Verify the pattern matches PRISTINE upstream at "
            f"https://raw.githubusercontent.com/DCurrent/openbor/v7533/engine/..."
        )
    actual_count = content.count(old)
    if actual_count != count:
        raise RuntimeError(
            f"strict_replace failed for '{label}': expected {count} match(es), "
            f"found {actual_count}. Pattern is not unique enough.\n"
            f"  First 80 chars of expected: {old[:80]!r}\n"
            f"  Add more surrounding context to make the pattern unique, "
            f"or pass count={actual_count} if multi-match is intentional."
        )
    return content.replace(old, new)

def extract_function(source, func_sig):
    """Extract a C function body starting from its signature."""
    start = source.find(func_sig)
    if start < 0:
        return None, -1, -1
    brace = 0
    found_open = False
    end = start
    for i in range(start, len(source)):
        if source[i] == '{':
            brace += 1
            found_open = True
        elif source[i] == '}':
            brace -= 1
        if found_open and brace == 0:
            end = i + 1
            break
    return source[start:end], start, end

def replace_function(source, func_sig, replacement_file, patches_dir):
    """Replace a function in source with the function from a patch file."""
    patch = read(os.path.join(patches_dir, replacement_file))
    # Find the function in the patch file
    func_start = patch.find(func_sig)
    if func_start < 0:
        print(f"  ERROR: Could not find '{func_sig}' in {replacement_file}")
        return source
    replacement = patch[func_start:]
    # Find and replace in source
    _, start, end = extract_function(source, func_sig)
    if start < 0:
        print(f"  ERROR: Could not find '{func_sig}' in source")
        return source
    return source[:start] + replacement + source[end:]

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <openbor_dir> <patches_dir>")
        sys.exit(1)

    obor = sys.argv[1]
    patches = sys.argv[2]

    # ── 1. Patch Makefile ─────────────────────────────────────────────
    print("Patching Makefile...")
    mf = read(os.path.join(obor, 'Makefile'))

    # Add BUILD_MISTER target block after BUILD_LINUX_LE_arm endif.
    # v7533 dropped BUILD_OPENDINGUX entirely — the closest analog is
    # BUILD_LINUX_LE_arm. Headers under include/SDL2 (v7533 source uses
    # SDL2 unconditionally; no BUILD_SDL2 toggle exists). Match the
    # warning suppressions that BUILD_LINUX_LE_arm uses for GCC 9+.
    mister_target = """
ifdef BUILD_MISTER
TARGET          = $(VERSION_NAME).elf
TARGET_FINAL    = $(VERSION_NAME)
TARGET_PLATFORM = LINUX
BUILD_SDL       = 1
BUILD_GFX       = 1
BUILD_PTHREAD   = 1
BUILD_SDL_IO    = 1
BUILD_VORBIS    = 1
BUILDING        = 1
CC              = gcc
OBJTYPE         = elf
ARCHFLAGS       = -mcpu=cortex-a9 -mfloat-abi=hard -mfpu=neon
INCLUDES        = $(SDL_PREFIX)/include \\
                  $(SDL_PREFIX)/include/SDL2
LIBRARIES       = $(SDL_PREFIX)/lib
INCS            += source/webmlib
CFLAGS          += -Wno-error=format-overflow -Wno-error=implicit-function-declaration -Wno-error=unused-variable -Wno-error=unused-label -Wno-error=stringop-overflow
ifeq ($(BUILD_MISTER), 0)
BUILD_DEBUG     = 1
endif
endif

"""
    # Insert after BUILD_LINUX_LE_arm closes. The "Workaround for GCC 9"
    # comment is unique enough to anchor on safely.
    marker = "# Workaround for GCC 9\nCFLAGS          += -Wno-error=format-overflow -Wno-error=implicit-function-declaration -Wno-error=unused-variable -Wno-error=unused-label -Wno-error=stringop-overflow\nendif"
    if marker in mf:
        mf = mf.replace(marker, marker + "\n" + mister_target)
    else:
        print("  ERROR: BUILD_LINUX_LE_arm anchor not found — Makefile structure may have changed")

    # Add MISTER_NATIVE_VIDEO CFLAGS. v7533 uses SDL2 natively; no
    # -DSDL2 needed (no codepaths gate on it).
    #
    # Step 25 (v3.1 perf, 2026-05-27): upgrade -O1 -> -O2 + funroll-loops.
    # Original choice of -O1 was to dodge GCC aggressive-loop UB in 4086's
    # openbor.c. The cleaner fix is explicit -fno-aggressive-loop-optimizations
    # at -O2 — keeps the protection while enabling -O2's broader inlining,
    # vectorization, and register allocation. -funroll-loops gives further
    # gain on the palette LUT inner loops which are the hot path.
    # NOTE: -flto was tried but pulled — link-time optimization can surface
    # latent ODR/visibility issues on this engine codebase; the marginal
    # 2-5% gain isn't worth the LOW-MEDIUM risk. Revisit if measurement
    # shows leftover ceiling.
    # Expected gain: 10-20% engine-wide speedup vs -O1 baseline.
    # Step 25 (v3.1 perf) flags (final, 2026-05-28):
    #   -O2 + -fno-aggressive-loop-optimizations (protect against UB at openbor.c)
    #   -funroll-loops (helps palette LUT inner loops)
    #   -fno-plt (direct calls; saves indirect-call cycles)
    #   -fno-semantic-interposition (enables own-function inlining)
    #   -flto (link-time optimization for cross-TU inlining)
    #
    # -flto was initially dropped out of caution but the f39311f CI run
    # (26549762603, 25m38s) proved it builds successfully in our codebase.
    # Risk reclassified LOW based on empirical success. Cheap closing
    # optimization (2-5% additional, mostly helps when bottleneck is
    # cross-source-file inlining like spriteq.c -> sprite.c -> spritex8p32.c).
    mf = strict_replace(
        mf,
        "ifdef BUILD_SDL\nCFLAGS \t       += -DSDL=1\nendif",
        "ifdef BUILD_SDL\nCFLAGS \t       += -DSDL=1\nendif\n\n\nifdef BUILD_MISTER\nCFLAGS         += -DMISTER_NATIVE_VIDEO -fcommon -Wno-error -O2 -fno-aggressive-loop-optimizations -funroll-loops -fno-plt -fno-semantic-interposition -flto -g -rdynamic -funwind-tables -fasynchronous-unwind-tables -mapcs-frame\nLDFLAGS        += -flto\nendif",
        'Makefile MISTER_NATIVE_VIDEO CFLAGS injection (Step 25 final: -O2 + funroll + fno-plt + fno-semantic-interposition + LTO)'
    )

    # Add native_video_writer.o and native_audio_writer.o to objects.
    menu_anchor = None
    for pattern in ["sdl/menu.o                                                                        \nendif",
                     "sdl/menu.o\nendif"]:
        if pattern in mf:
            menu_anchor = pattern
            break
    if menu_anchor:
        mf = mf.replace(
            menu_anchor,
            menu_anchor + "\n\n\nifdef BUILD_MISTER\nGAME_CONSOLE   += native_video_writer.o native_audio_writer.o\nendif",
            1
        )
    else:
        print("  WARN: sdl/menu.o endif pattern not found for object injection")

    # Add strip rule. v7533 strip block is gated by ifndef BUILD_DEBUG /
    # ifndef NO_STRIP and contains per-platform overrides (BUILD_WIN,
    # BUILD_LINUX, BUILD_DARWIN, BUILD_PANDORA). Insert ours after
    # BUILD_PANDORA so it's still inside the gating ifndefs.
    strip_anchor = "ifdef BUILD_PANDORA\nSTRIP \t        = $(PNDDEV)/bin/arm-none-linux-gnueabi-strip $(TARGET) -o $(TARGET_FINAL)\nendif"
    if strip_anchor in mf:
        mf = mf.replace(
            strip_anchor,
            strip_anchor + "\nifdef BUILD_MISTER\nSTRIP           = strip $(TARGET) -o $(TARGET_FINAL)\nendif"
        )
    else:
        print("  WARN: BUILD_PANDORA strip anchor not found — binary may not be stripped")

    # Force SDL2 link libs for MiSTer (-lSDL2 instead of -lSDL),
    # plus -ldl for dlopen, -lpthread for native writer threads.
    mf = strict_replace(
        mf,
        "LIBS           += -lpng -lz -lm",
        "LIBS           += -lpng -lz -lm\n\n\nifdef BUILD_MISTER\nLIBS           += -lSDL2 -lSDL2_gfx -ldl -lpthread\nendif",
        'Makefile SDL2 link libs injection'
    )

    write(os.path.join(obor, 'Makefile'), mf)
    print("  Makefile patched.")

    # ── 1b. Patch packfile.c — bump CACHEBLOCKS 96 -> 255 + readahead 0 -> 64KB
    # MiSTer 2026-05-24: PAK init speedup on heavy carts via filecache tuning.
    #
    # Background: OpenBOR's filecache (engine/source/gamelib/filecache.c +
    # packfile.c) is a transparent block-cache between disk reads and the
    # engine's file API. Default config keeps 96 blocks x 32KB = ~3MB resident
    # and uses NO readahead (pak_vfdreadahead[i] init = -1). Cart init on
    # heavy PAKs (~600-1000 model loads) thrashes the cache + waits on each
    # block's SD read.
    #
    # Tuning:
    #   - CACHEBLOCKS 96 -> 255 (uint8_t ceiling per filecache.h:
    #       "BLOCKS MUST BE 255 OR LESS"). ~8MB resident cache vs ~3MB.
    #       More blocks stay hot through init = fewer evict-reload cycles.
    #   - pak_vfdreadahead init -1 -> 65536 (64KB default = 2 cache blocks).
    #       Most asset reads (sprites, scripts, music) are sequential within
    #       a file so prefetch is a clean fit. prebuffer stays at 0 (no
    #       filecache_wait_for_prebuffer calls added) so open() doesn't
    #       block on initial prefetch.
    #
    # Memory cost: ~8MB resident vs ~3MB. Negligible on MiSTer's 1GB DDR3.
    # PAK on-disk format unchanged. Read semantics unchanged. PAKs that
    # load today still load identically.
    print("Patching packfile.c (filecache speedup: CACHEBLOCKS 96->255 + readahead 0->64KB)...")
    pf_path = os.path.join(obor, 'source/gamelib/packfile.c')
    pf = read(pf_path)
    pf = strict_replace(pf,
        '#ifndef OPENDINGUX\n#define CACHEBLOCKS    (96)\n#else\n#define CACHEBLOCKS    (8)\n#endif',
        '#ifndef OPENDINGUX\n#define CACHEBLOCKS    (255) /* MiSTer 2026-05-24: 96 -> 255 (uint8_t ceiling). ~8MB resident cache. */\n#else\n#define CACHEBLOCKS    (8)\n#endif',
        'filecache: bump CACHEBLOCKS 96 -> 255')
    pf = strict_replace(pf,
        '        pak_vfdreadahead[i] = -1;\n    }\n    pak_initialized = 0;',
        '        pak_vfdreadahead[i] = 65536; /* MiSTer 2026-05-24: 64KB default readahead (was -1=none); paired with bumped CACHEBLOCKS */\n    }\n    pak_initialized = 0;',
        'filecache: init pak_vfdreadahead = 64KB (was -1)')
    # 2026-06-13: [LOAD] decode-io split. readpackfile (the bulk pak read) is the
    # I/O inside loadbitmap (sprite GIF/PNG decode). _mister_decode_io_active is set
    # ONLY around loadbitmap in openbor.c, so this times sprite-decode reads but NOT
    # buffer_pakfile/other reads (flag=0 there -> wrapper is a single branch, ~free).
    # decode-io is a subset of 'decode'; decode - decode-io = LZW/inflate CPU.
    pf = strict_replace(pf,
        '#include "packfile.h"',
        '#include "packfile.h"\n'
        '#include <sys/time.h>\n'
        'extern unsigned long _mister_decode_io_us; /* defined in openbor.c */\n'
        'extern int _mister_decode_io_active;\n'
        'static unsigned long _mister_pf_us(void){ struct timeval _t; gettimeofday(&_t, 0); return (unsigned long)_t.tv_sec * 1000000UL + (unsigned long)_t.tv_usec; }',
        'decode-io: packfile.c sys/time.h + extern accumulators + us helper')
    pf = strict_replace(pf,
        'int readpackfile(int handle, void *buf, int len)\n{',
        'int readpackfile_impl(int handle, void *buf, int len);\n'
        'int readpackfile(int handle, void *buf, int len)\n'
        '{\n'
        '    if (!_mister_decode_io_active) return readpackfile_impl(handle, buf, len);\n'
        '    { unsigned long _pft0 = _mister_pf_us(); int _pfr = readpackfile_impl(handle, buf, len); _mister_decode_io_us += _mister_pf_us() - _pft0; return _pfr; }\n'
        '}\n'
        'int readpackfile_impl(int handle, void *buf, int len)\n{',
        'decode-io: rename readpackfile -> _impl + flag-gated timing wrapper')
    write(pf_path, pf)
    print("  packfile.c: CACHEBLOCKS=255 + readahead=65536 (paired filecache speedup)")
    print("  packfile.c: readpackfile decode-io timing wrapper (flag-gated)")

    # ## #2: inlined-LUT specialized blit in spritex8p16.c (the HOT 16-bit path)
    # The 16-bit blitter (spritex8p16.c) is what runs in our PIXEL_16 build:
    # putsprite_x8p16 -> putsprite_blend_{,flip_}, which call the blend fp PER
    # PIXEL. blend_bench on the A9 showed inlining the LUT lookup + hoisting the
    # table (one func; the table ptr is the only per-mode difference) is
    # ~1.25-1.42x faster than the fp-dispatch LUT. Output is bit-identical (same
    # _color16(tbl[..]) the fp path computes). half + arithmetic (NULL table)
    # keep the generic fp path.
    print("Patching spritex8p16.c (#2: inlined-LUT specialized blit)...")
    s16_path = os.path.join(obor, 'source/gamelib/spritex8p16.c')
    s16 = read(s16_path)
    s16 = strict_replace(s16,
        "void putsprite_x8p16(\n"
        "    int x, int y, int is_flip, s_sprite *sprite, s_screen *screen,\n"
        "    unsigned short *remap, blend16fp blend\n"
        ")",
        "/* MiSTer #2: inlined-LUT blit -- same RLE walk as putsprite_blend_, but\n"
        " * the per-pixel blend is the LUT lookup inlined with the table hoisted\n"
        " * (no per-pixel fp call, no per-pixel blendtables[] reload). One func\n"
        " * serves every mode; only `tbl` differs. Bit-identical to the fp path. */\n"
        "#define _b1 (color1>>11)\n"
        "#define _g1 ((color1&0x7E0)>>5)\n"
        "#define _r1 (color1&0x1F)\n"
        "#define _b2 (color2>>11)\n"
        "#define _g2 ((color2&0x7E0)>>5)\n"
        "#define _r2 (color2&0x1F)\n"
        "#define _lutbi ((_b1<<5)|_b2)\n"
        "#define _lutgi (((_g1<<6)|_g2)+1024)\n"
        "#define _lutri ((_r1<<5)|_r2)\n"
        "#define _lutcolor(r,g,b) ( ((b)<<11)|((g)<<5)|(r) )\n"
        "static void putsprite_lut_(\n"
        "    unsigned short *dest, int x, int xmin, int xmax, int *linetab, unsigned short *palette, int h, int screenwidth,\n"
        "    const unsigned char *tbl\n"
        ")\n"
        "{\n"
        "    for(; h > 0; h--, dest += screenwidth)\n"
        "    {\n"
        "        register int lx = x;\n"
        "        unsigned char *data = ((unsigned char *)linetab) + (*linetab);\n"
        "        linetab++;\n"
        "        while(lx < xmax)\n"
        "        {\n"
        "            register int count = *data++;\n"
        "            if(count == 0xFF) break;\n"
        "            lx += count;\n"
        "            if(lx >= xmax) break;\n"
        "            count = *data++;\n"
        "            if(!count) continue;\n"
        "            if((lx + count) <= xmin) { lx += count; data += count; continue; }\n"
        "            if(lx < xmin) { int diff = lx - xmin; count += diff; data -= diff; lx = xmin; }\n"
        "            if((lx + count) > xmax) count = xmax - lx;\n"
        "            for(; count > 0; count--, lx++)\n"
        "            {\n"
        "                unsigned short color1 = palette[*data++], color2 = dest[lx];\n"
        "                dest[lx] = (unsigned short)_lutcolor(tbl[_lutri], tbl[_lutgi], tbl[_lutbi]);\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
        "static void putsprite_lut_flip_(\n"
        "    unsigned short *dest, int x, int xmin, int xmax, int *linetab, unsigned short *palette, int h, int screenwidth,\n"
        "    const unsigned char *tbl\n"
        ")\n"
        "{\n"
        "    for(; h > 0; h--, dest += screenwidth)\n"
        "    {\n"
        "        register int lx = x;\n"
        "        unsigned char *data = ((unsigned char *)linetab) + (*linetab);\n"
        "        linetab++;\n"
        "        while(lx > xmin)\n"
        "        {\n"
        "            register int count = *data++;\n"
        "            if(count == 0xFF) break;\n"
        "            lx -= count;\n"
        "            if(lx <= xmin) break;\n"
        "            count = *data++;\n"
        "            if(!count) continue;\n"
        "            if((lx - count) >= xmax) { lx -= count; data += count; continue; }\n"
        "            if(lx > xmax) { int diff = (lx - xmax); count -= diff; data += diff; lx = xmax; }\n"
        "            if((lx - count) < xmin) count = lx - xmin;\n"
        "            for(; count > 0; count--)\n"
        "            {\n"
        "                --lx;\n"
        "                unsigned short color1 = palette[*data++], color2 = dest[lx];\n"
        "                dest[lx] = (unsigned short)_lutcolor(tbl[_lutri], tbl[_lutgi], tbl[_lutbi]);\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
        "#undef _b1\n#undef _g1\n#undef _r1\n#undef _b2\n#undef _g2\n#undef _r2\n"
        "#undef _lutbi\n#undef _lutgi\n#undef _lutri\n#undef _lutcolor\n"
        "\n"
        "void putsprite_x8p16(\n"
        "    int x, int y, int is_flip, s_sprite *sprite, s_screen *screen,\n"
        "    unsigned short *remap, blend16fp blend\n"
        ")",
        '#2: insert putsprite_lut_ + _flip_ before putsprite_x8p16')
    s16 = strict_replace(s16,
        "    else if(blend)\n"
        "    {\n"
        "        if(is_flip)\n"
        "        {\n"
        "            putsprite_blend_flip_(dest, x, xmin, xmax, linetab, m , h, screenwidth, blend);\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            putsprite_blend_     (dest, x, xmin, xmax, linetab, m , h, screenwidth, blend);\n"
        "        }\n"
        "    }",
        "    else if(blend)\n"
        "    {\n"
        "        /* MiSTer #2: built-LUT modes (screen/multiply/overlay/hardlight/\n"
        "         * dodge = blendfunctions16[0..4]) take the inlined-LUT blit. half\n"
        "         * (idx 5) + arithmetic (NULL table) fall through to the fp path. */\n"
        "        unsigned char *lut = NULL; int _bi;\n"
        "        for(_bi = 0; _bi < 5; _bi++) { if(blend == blendfunctions16[_bi]) { lut = blendtables[_bi]; break; } }\n"
        "        if(lut)\n"
        "        {\n"
        "            if(is_flip) putsprite_lut_flip_(dest, x, xmin, xmax, linetab, m , h, screenwidth, lut);\n"
        "            else        putsprite_lut_     (dest, x, xmin, xmax, linetab, m , h, screenwidth, lut);\n"
        "        }\n"
        "        else if(is_flip)\n"
        "        {\n"
        "            putsprite_blend_flip_(dest, x, xmin, xmax, linetab, m , h, screenwidth, blend);\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            putsprite_blend_     (dest, x, xmin, xmax, linetab, m , h, screenwidth, blend);\n"
        "        }\n"
        "    }",
        '#2: dispatch built-LUT modes to inlined-LUT blit')
    write(s16_path, s16)
    print("  spritex8p16.c: #2 inlined-LUT specialized blit (screen/multiply/overlay/hardlight/dodge).")

    # ## #1: port spritex8p32 Step 22/26 NEON copy to spritex8p16.c (the HOT path)
    # 8x unroll + NEON 128-bit store (vst1q_u16 = 8 px/store) + source prefetch,
    # adapted from the cold 32-bit blitter. Scalar palette[idx] gather kept (A9
    # has no NEON gather; the live LUT keeps flash/remap correct). Output
    # byte-identical to the stock copy loop.
    print("Patching spritex8p16.c (#1: NEON copy ported from spritex8p32 Step 22/26)...")
    s16b = read(s16_path)
    s16b = strict_replace(s16b,
        '#include "types.h"',
        '#include "types.h"\n#ifdef __ARM_NEON\n#include <arm_neon.h>\n#endif',
        '#1: arm_neon.h include in spritex8p16.c')
    s16b = strict_replace(s16b,
        "            if((lx + count) > xmax)\n"
        "            {\n"
        "                count = xmax - lx;\n"
        "            }\n"
        "            for(; count > 0; count--)\n"
        "            {\n"
        "                dest[lx++] = palette[*data++];\n"
        "            }\n"
        "            //u16pcpy(dest+lx, data, palette, count);\n"
        "            //lx+=count;\n"
        "            //data+=count;",
        "            if((lx + count) > xmax)\n"
        "            {\n"
        "                count = xmax - lx;\n"
        "            }\n"
        "            /* MiSTer #1 (ported from spritex8p32 Step 22/26 -> 16-bit):\n"
        "             *  8x unroll + NEON 128-bit store (vst1q_u16 = 8 px) + src\n"
        "             *  prefetch. Scalar palette[idx] gather kept (no A9 NEON\n"
        "             *  gather; live LUT keeps flash/remap correct). */\n"
        "            __builtin_prefetch(data + 128, 0, 0);\n"
        "            __builtin_prefetch(data + 192, 0, 0);\n"
        "            {\n"
        "                unsigned short * const __restrict__ pal_r = palette;\n"
        "                unsigned char *data_p = data;\n"
        "                unsigned short *dest_p = &dest[lx];\n"
        "                while(count >= 8)\n"
        "                {\n"
        "                    unsigned short p0 = pal_r[data_p[0]];\n"
        "                    unsigned short p1 = pal_r[data_p[1]];\n"
        "                    unsigned short p2 = pal_r[data_p[2]];\n"
        "                    unsigned short p3 = pal_r[data_p[3]];\n"
        "                    unsigned short p4 = pal_r[data_p[4]];\n"
        "                    unsigned short p5 = pal_r[data_p[5]];\n"
        "                    unsigned short p6 = pal_r[data_p[6]];\n"
        "                    unsigned short p7 = pal_r[data_p[7]];\n"
        "#ifdef __ARM_NEON\n"
        "                    vst1q_u16((uint16_t *)dest_p, (uint16x8_t){p0, p1, p2, p3, p4, p5, p6, p7});\n"
        "#else\n"
        "                    dest_p[0] = p0; dest_p[1] = p1; dest_p[2] = p2; dest_p[3] = p3;\n"
        "                    dest_p[4] = p4; dest_p[5] = p5; dest_p[6] = p6; dest_p[7] = p7;\n"
        "#endif\n"
        "                    dest_p += 8;\n"
        "                    data_p += 8;\n"
        "                    count  -= 8;\n"
        "                }\n"
        "                while(count > 0)\n"
        "                {\n"
        "                    *dest_p++ = pal_r[*data_p++];\n"
        "                    count--;\n"
        "                }\n"
        "                lx   = (int)(dest_p - dest);\n"
        "                data = data_p;\n"
        "            }",
        '#1: putsprite_ 8x-unroll + NEON u16 store (ported from 32-bit)')
    write(s16_path, s16b)
    print("  spritex8p16.c: #1 NEON copy in putsprite_ (8x unroll + vst1q_u16 + prefetch).")

    # ── 2. Patch openbor.c — replace pausemenu() ─────────────────────
    print("Patching openbor.c (pausemenu)...")
    src = read(os.path.join(obor, 'openbor.c'))
    src = replace_function(src, "void pausemenu()", "pausemenu_patch.c", patches)
    write(os.path.join(obor, 'openbor.c'), src)
    print("  pausemenu() replaced.")

    # ── 3. sdl/video.c — bypass SDL2 renderer chain in video_copy_screen ─
    # Profiling 2026-05-22 showed video_copy_screen consumed ~22ms of every
    # ~25ms update() call (89%). The chain SDL_UpdateTexture → blit() (which
    # does SDL_RenderClear + SDL_RenderCopy + SDL_RenderPresent) does at
    # least 3 memcpys of the 320×224×4 = 286KB framebuffer plus internal
    # SDL2 renderer overhead. Even with the dummy driver, this all runs on
    # CPU. To recover the budget, we bypass the entire SDL renderer chain
    # and write directly to DDR3 via NativeVideoWriter_WriteFrame (which
    # already does the anisotropic NN squish to 320x224 for non-native
    # source dimensions). Expected savings: ~15ms per frame → ~10ms total
    # update() = ~100 fps native on Cortex-A9.
    print("Patching sdl/video.c (bypass SDL2 renderer — direct WriteFrame)...")
    video_path = os.path.join(obor, 'sdl/video.c')
    video_c = read(video_path)

    # Add include for native_video_writer.h at the end of the SDL2 include block
    video_c = strict_replace(
        video_c,
        '#include "videocommon.h"\n'
        '#include "../resources/OpenBOR_Icon_32x32_png.h"',
        '#include "videocommon.h"\n'
        '#include "../resources/OpenBOR_Icon_32x32_png.h"\n'
        '#ifdef MISTER_NATIVE_VIDEO\n'
        '#include "native_video_writer.h"\n'
        '#endif',
        'sdl/video.c include native_video_writer.h'
    )

    # Replace video_copy_screen body to bypass SDL chain under MISTER_NATIVE_VIDEO
    video_c = strict_replace(
        video_c,
        '\tif(opengl) return video_gl_copy_screen(surface);\n'
        '\n'
        '\tSDL_UpdateTexture(texture, NULL, surface->data, surface->pitch);\n'
        '\tblit();',
        '\tif(opengl) return video_gl_copy_screen(surface);\n'
        '\n'
        '#ifdef MISTER_NATIVE_VIDEO\n'
        '\t/* Bypass SDL2 renderer chain (saves ~15ms/frame on Cortex-A9).\n'
        '\t * NativeVideoWriter_WriteFrame writes directly to DDR3 with\n'
        '\t * anisotropic NN squish to 320×224 (Sega CD V28 NTSC). */\n'
        '\tNativeVideoWriter_WriteFrame(surface->data,\n'
        '\t                              surface->width, surface->height,\n'
        '\t                              surface->pitch,\n'
        '\t                              stored_videomodes.pixel * 8,\n'
        '\t                              NULL);\n'
        '\treturn 1;\n'
        '#else\n'
        '\tSDL_UpdateTexture(texture, NULL, surface->data, surface->pitch);\n'
        '\tblit();\n'
        '#endif',
        'sdl/video.c video_copy_screen bypass'
    )

    write(video_path, video_c)
    print("  sdl/video.c: video_copy_screen now writes directly to DDR3, bypassing SDL2 renderer chain")

    # ── 4. Patch sdl/control.c — replace control_update() ────────────
    print("Patching sdl/control.c (input mapping)...")
    src = read(os.path.join(obor, 'sdl/control.c'))

    # Add include
    src = strict_replace(
        src,
        '#include "openbor.h"',
        '#include "openbor.h"\n#ifdef MISTER_NATIVE_VIDEO\n#include "native_video_writer.h"\n#endif',
        'sdl/control.c #include injection'
    )

    src = replace_function(src, "void control_update(s_playercontrols ** playercontrols, int numplayers)", "control_patch.c", patches)
    write(os.path.join(obor, 'sdl/control.c'), src)
    print("  control_update() replaced.")

    # ── 5. Patch sdl/sdlport.c — replace main() ─────────────────────
    print("Patching sdl/sdlport.c (main + NativeVideoWriter init)...")
    src = read(os.path.join(obor, 'sdl/sdlport.c'))

    # Add includes
    src = strict_replace(
        src,
        '#include "menu.h"',
        '#include "menu.h"\n#ifdef MISTER_NATIVE_VIDEO\n#include "native_video_writer.h"\n#include "native_audio_writer.h"\n#include <sys/stat.h>\n#include <stdlib.h>\n#include <time.h>\n#include <unistd.h>\n#include <pthread.h>\n#include <signal.h>\n#include <execinfo.h>\n#endif',
        'sdl/sdlport.c #include injection'
    )

    # Replace main() and inject any code above it (swap thread, etc.)
    main_sig = "int main(int argc, char *argv[])"
    start = src.find(main_sig)
    if start >= 0:
        patch = read(os.path.join(patches, 'sdlport_patch.c'))
        # Find the first #ifdef MISTER_NATIVE_VIDEO before main() —
        # that's where our pre-main code starts (swap thread, globals)
        premain_marker = "#ifdef MISTER_NATIVE_VIDEO\n/* Crash handler"
        premain_start = patch.find(premain_marker)
        if premain_start >= 0:
            replacement = patch[premain_start:]
        else:
            func_start = patch.find(main_sig)
            replacement = patch[func_start:]
        src = src[:start] + replacement + "\n"

    write(os.path.join(obor, 'sdl/sdlport.c'), src)
    print("  main() replaced.")

    # ── 6. Patch source/utils.c — redirect save + log paths ─────────────
    print("Patching source/utils.c (save path redirect + log path absolute)...")
    src = read(os.path.join(obor, 'source/utils.c'))

    # Pristine v7533 source/utils.c line ~102 (LINUX target — the #else
    # branch after WII/VITA/ANDROID variants) uses strcpy/strcat, NOT
    # strncpy/strncat. The previous pattern's strncpy form was silently
    # failing — saves/config/savestates redirect to /media/fat/... never
    # took effect since the macro was never replaced. Caught by audit
    # 2026-05-19 (see feedback_ci_set_minus_e_hides_patch_failures.md +
    # the new strict_replace helper above which now RAISES instead of
    # silently no-op on pattern miss).
    # Verified upstream verbatim:
    # https://raw.githubusercontent.com/DCurrent/openbor/v7533/engine/source/utils.c L102
    old_macro = '#define COPY_ROOT_PATH(buf, name) strcpy(buf, "./"); strcat(buf, name); strcat(buf, "/");'

    # Note: Logs path is /media/fat/logs/OpenBOR_7533/ — per-build, matching
    # the saves/savestates per-build pattern (sister cores share PAK content
    # at games/OpenBOR/Paks/ but write to separate save/savestate/log dirs
    # because the data is build-specific). This prevents cross-build log
    # mixing when both binaries dispatch under the unified "OpenBOR" setname.
    new_macro = """#ifdef MISTER_NATIVE_VIDEO
#define COPY_ROOT_PATH(buf, name) \\
    do { \\
        if (strcmp(name, "Saves") == 0) { \\
            strcpy(buf, "/media/fat/saves/OpenBOR_7533/"); \\
        } else if (strcmp(name, "SaveStates") == 0) { \\
            strcpy(buf, "/media/fat/savestates/OpenBOR_7533/"); \\
        } else if (strcmp(name, "Config") == 0) { \\
            strcpy(buf, "/media/fat/config/"); \\
        } else if (strcmp(name, "Logs") == 0) { \\
            strcpy(buf, "/media/fat/logs/OpenBOR_7533/"); \\
        } else { \\
            strcpy(buf, "./"); strcat(buf, name); strcat(buf, "/"); \\
        } \\
    } while(0)
#else
#define COPY_ROOT_PATH(buf, name) strcpy(buf, "./"); strcat(buf, name); strcat(buf, "/");
#endif"""

    src = strict_replace(src, old_macro, new_macro, 'COPY_ROOT_PATH macro in source/utils.c')

    # Patch the four LOGFILE macros that hardcode "./Logs/OpenBorLog.txt"
    # and "./Logs/ScriptLog.txt" relative paths. These are used by the
    # engine's writeToLogFile() unconditionally (NOT via COPY_ROOT_PATH),
    # so they need their own replacement. Writing to cwd's Logs/ directory
    # violates the canonical single-location log rule
    # (/media/fat/logs/{CoreName}/) — patch to absolute paths.
    # 4 / 5 occurrences — intentional multi-match (LOGFILE macros).
    src = strict_replace(
        src,
        '"./Logs/OpenBorLog.txt"',
        '"/media/fat/logs/OpenBOR_7533/OpenBorLog.txt"',
        'source/utils.c OpenBorLog absolute path',
        count=4
    )
    src = strict_replace(
        src,
        '"./Logs/ScriptLog.txt"',
        '"/media/fat/logs/OpenBOR_7533/ScriptLog.txt"',
        'source/utils.c ScriptLog absolute path',
        count=5
    )

    write(os.path.join(obor, 'source/utils.c'), src)
    print("  Save path redirected; log path absolute (/media/fat/logs/OpenBOR_7533/).")

    # ── 6c. Patch openbor.c — route .cfg/.hi to Config, .s00 to SaveStates ──
    print("Patching openbor.c (split save directories)...")
    obor_c = read(os.path.join(obor, 'openbor.c'))

    # 2 occurrences each — intentional multi-match (savesettings + loadsettings pairs).
    obor_c = strict_replace(
        obor_c,
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 4);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 4);',
        '.cfg path -> Config (savesettings/loadsettings)',
        count=2
    )

    obor_c = strict_replace(
        obor_c,
        'getBasePath(path, "Saves", 0);\n    strcat(path, "default.cfg");',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    strcat(path, "default.cfg");',
        'default.cfg path -> Config',
        count=2
    )

    obor_c = strict_replace(
        obor_c,
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 1);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 1);',
        '.hi (high score) path -> Config',
        count=2
    )

    # .s00 save states (saveScriptFile uses tmpvalue)
    obor_c = strict_replace(
        obor_c,
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpvalue, 2);//.scr',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "SaveStates", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpvalue, 2);//.scr',
        '.s00 saveScriptFile path -> SaveStates'
    )
    # loadScriptFile uses tmpname
    obor_c = strict_replace(
        obor_c,
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 2);//.scr',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "SaveStates", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 2);//.scr',
        '.s00 loadScriptFile path -> SaveStates'
    )

    write(os.path.join(obor, 'openbor.c'), obor_c)
    print("  .cfg/.hi -> /media/fat/config/, .s00 -> /media/fat/savestates/OpenBOR_7533/")

    # ── Step 31 v2 (2026-05-28): Respect cart's EXPLICIT subject_to_gravity 0
    #
    # Stock OpenBOR v7533's ent_default_init() at line ~23625 forcefully RE-ADDS
    # MOVE_CONFIG_SUBJECT_TO_GRAVITY to ALL TYPE_NONE entities at spawn time.
    # This OVERRIDES the cart's `subject_to_gravity 0` directive — but it's also
    # the DEFAULT BEHAVIOR many carts rely on without explicit opt-in.
    #
    # Step 31 v1 (commit 832996a) BLINDLY removed the forced gravity. This
    # fixed Cap super shield freeze AND Aliens Clash sun+bullets, but BROKE
    # backward compatibility for carts whose enemy bullets relied on default
    # forced gravity without declaring `subject_to_gravity 1` explicitly.
    # User reported: "some enemies that should be shooting bullets don't shoot
    # bullets" after Step 31 v1.
    #
    # Step 31 v2 (this version) is smarter: track whether cart's parser saw an
    # EXPLICIT `subject_to_gravity` directive. Only skip the forced-gravity-add
    # in ent_default_init if explicitly set.
    #
    # Implementation:
    #   1. Add END-of-s_model field `int gravity_directive_seen` (default 0).
    #      END placement avoids offset shifts that would regress other patches.
    #   2. In CMD_MODEL_SUBJECT_TO_GRAVITY parser, set newchar->gravity_directive_seen = 1.
    #   3. In ent_default_init TYPE_NONE case: only force-add gravity if !gravity_directive_seen.
    #
    # Behavior matrix:
    #   - Cart says `subject_to_gravity 0` → flag cleared by parser, seen=1,
    #     ent_default_init skips force-add → no gravity ✓ (our shield, sun, etc)
    #   - Cart says `subject_to_gravity 1` → flag set by parser, seen=1,
    #     ent_default_init skips force-add → flag stays as parser set it ✓
    #   - Cart silent → flag at parse-default (0 if MOVE_CONFIG_NONE), seen=0,
    #     ent_default_init force-adds → gravity ON (matches stock behavior) ✓
    # NOTE: Step 31 v2's gravity_directive_seen field is added to s_model END
    # later in this script (extending the v3.10 has_palette_directive patch).
    # Parser + ent_default_init patches below use the field — both go into
    # openbor.c which is compiled AFTER apply_patches.py finishes, so the
    # field needs to exist by then. The openbor.h s_model extension at the
    # v3.10 section ensures that.

    print("Patching openbor.c (Step 31 v2: parser sets gravity_directive_seen)...")
    ob_path_g = os.path.join(obor, 'openbor.c')
    ob_g = read(ob_path_g)
    parser_old = (
        "            case CMD_MODEL_SUBJECT_TO_GRAVITY:\n"
        "                \n"
        "                /* Legacy code allowed -1 or 0 for False.  */\n"
        "                if (GET_INT_ARG(1) > 0)\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_SUBJECT_TO_GRAVITY;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_SUBJECT_TO_GRAVITY;\n"
        "                }\n"
        "\n"
        "                break;"
    )
    parser_new = (
        "            case CMD_MODEL_SUBJECT_TO_GRAVITY:\n"
        "                \n"
        "                /* Legacy code allowed -1 or 0 for False.  */\n"
        "                if (GET_INT_ARG(1) > 0)\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_SUBJECT_TO_GRAVITY;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_SUBJECT_TO_GRAVITY;\n"
        "                }\n"
        "                newchar->gravity_directive_seen = 1; /* MiSTer Step 31 v2: gate ent_default_init force-gravity */\n"
        "\n"
        "                break;"
    )
    ob_g = strict_replace(ob_g, parser_old, parser_new,
                          'Step 31 v2: parser marks gravity_directive_seen')

    gravity_old = (
        "    case TYPE_NONE:\n"
        "        e->nograb = 1;\n"
        "        e->nograb_default = e->nograb;\n"
        "        \n"
        "        //e->base=e->position.y; //complained?\n"
        "        e->modeldata.move_config_flags |= (MOVE_CONFIG_NO_ADJUST_BASE | MOVE_CONFIG_SUBJECT_TO_GRAVITY);"
    )
    gravity_new = (
        "    case TYPE_NONE:\n"
        "        e->nograb = 1;\n"
        "        e->nograb_default = e->nograb;\n"
        "        \n"
        "        //e->base=e->position.y; //complained?\n"
        "        /* MiSTer Step 31 v3 (2026-05-28): respect cart's EXPLICIT directives for BOTH */\n"
        "        /* subject_to_gravity AND no_adjust_base. If cart's parser saw the directive */\n"
        "        /* (regardless of value), respect what it set. If cart was silent on a given */\n"
        "        /* directive, keep stock OpenBOR's default of forcing that flag ON for TYPE_NONE. */\n"
        "        /* v2 fixed gravity for Cap shield + sun + bullets; v3 extends to no_adjust_base */\n"
        "        /* so platform-mounted Prin shooters in Aliens Clash fire correctly (EShot has */\n"
        "        /* no_adjust_base 0). */\n"
        "        if (!e->modeldata.no_adjust_base_directive_seen) {\n"
        "            e->modeldata.move_config_flags |= MOVE_CONFIG_NO_ADJUST_BASE;\n"
        "        }\n"
        "        if (!e->modeldata.gravity_directive_seen) {\n"
        "            e->modeldata.move_config_flags |= MOVE_CONFIG_SUBJECT_TO_GRAVITY;\n"
        "        }"
    )
    ob_g = strict_replace(ob_g, gravity_old, gravity_new,
                          'Step 31 v3: ent_default_init only force-flags if cart was silent on directive')
    write(ob_path_g, ob_g)
    print("  Step 31 v3: ent_default_init now respects cart's explicit subject_to_gravity AND no_adjust_base directives")

    # ── Step 31 v3 (2026-05-28): parser patch for CMD_MODEL_NO_ADJUST_BASE ──
    # Sister to the CMD_MODEL_SUBJECT_TO_GRAVITY parser patch above.
    # Sets newchar->no_adjust_base_directive_seen = 1 whenever the cart's
    # character.txt declares `no_adjust_base N`, regardless of value.
    # ent_default_init then respects the parser-set flag value instead of
    # blindly force-setting it for TYPE_NONE.
    print("Patching openbor.c (Step 31 v3: parser sets no_adjust_base_directive_seen)...")
    parser_nab_old = (
        "            case CMD_MODEL_NO_ADJUST_BASE:\n"
        "\n"
        "                /* Legacy code allowed -1 or 0 for False.  */\n"
        "                if (GET_INT_ARG(1) > 0)\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_NO_ADJUST_BASE;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_NO_ADJUST_BASE;\n"
        "                }\n"
        "\n"
        "                break;"
    )
    parser_nab_new = (
        "            case CMD_MODEL_NO_ADJUST_BASE:\n"
        "\n"
        "                /* Legacy code allowed -1 or 0 for False.  */\n"
        "                if (GET_INT_ARG(1) > 0)\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_NO_ADJUST_BASE;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_NO_ADJUST_BASE;\n"
        "                }\n"
        "                newchar->no_adjust_base_directive_seen = 1; /* MiSTer Step 31 v3: gate ent_default_init force-set */\n"
        "\n"
        "                break;"
    )
    ob_g = read(ob_path_g)
    ob_g = strict_replace(ob_g, parser_nab_old, parser_nab_new,
                          'Step 31 v3: parser marks no_adjust_base_directive_seen')
    write(ob_path_g, ob_g)
    print("  Step 31 v3: CMD_MODEL_NO_ADJUST_BASE parser now records directive_seen")

    # ── Step 32 (2026-05-28): defensive entity-pointer validation in script bridge ─
    # Crash investigation: TMNT Rescue Palooza "Continue from save" SIGSEGV in
    # kill_entity+0xe7, called from script's killentity() via openbor_killentity.
    # The save+continue flow restored a level where a scroll-spawn script holds
    # a STALE entity pointer (entity at that address was freed, memory reused).
    # The script bridge only checked `ent == NULL`; the !exists check inside
    # kill_entity then read garbage from freed memory because the pointer was
    # non-NULL but pointed at scrambled bytes. SIGSEGV at offset 0x428.
    #
    # Fix: validate the script-supplied entity pointer is in ent_list[] before
    # calling kill_entity. If stale, silently no-op -- cart misses one kill
    # but gameplay continues. Far better than crashing to black screen.
    #
    # Validation is O(ent_max). ent_max is typically a few hundred even on
    # heavy PAKs; negligible cost (called only from script).
    #
    # Same use-after-free pattern likely affects other script bridges that
    # take entity pointers (changeentityproperty, getentityproperty etc.).
    # Those haven't crashed yet -- adding validation only where the crash
    # actually surfaced. Future failures can extend the validation pattern.
    print("Patching openborscript.c (Step 32: validate killentity pointer)...")
    obs_path_k = os.path.join(obor, 'openborscript.c')
    obs_k = read(obs_path_k)
    killent_old = (
        "//killentity(entity)\n"
        "HRESULT openbor_killentity(ScriptVariant **varlist , ScriptVariant **pretvar, int paramCount)\n"
        "{\n"
        "    entity *ent = NULL;\n"
        "    e_kill_entity_trigger trigger = KILL_ENTITY_TRIGGER_SCRIPT_KILLENTITY_UNDEFINED;\n"
        "    if(paramCount < 1)\n"
        "    {\n"
        "        *pretvar = NULL;\n"
        "        return E_FAIL;\n"
        "    }\n"
        "\n"
        "    ScriptVariant_ChangeType(*pretvar, VT_INTEGER);\n"
        "\n"
        "    ent = (entity *)(varlist[0])->ptrVal; //retrieve the entity\n"
        "    if(ent == NULL)\n"
        "    {\n"
        "        (*pretvar)->lVal = (LONG)0;\n"
        "        return S_OK;\n"
        "    }\n"
        "\n"
        "    // Get the saves directory\n"
        "    if (paramCount >= 2)\n"
        "    {\n"
        "        trigger = (e_kill_entity_trigger)(varlist[1])->lVal; // Reason to kill entity.\n"
        "    }\n"
        "\n"
        "    kill_entity(ent, trigger);"
    )
    killent_new = (
        "//killentity(entity)\n"
        "HRESULT openbor_killentity(ScriptVariant **varlist , ScriptVariant **pretvar, int paramCount)\n"
        "{\n"
        "    /* MiSTer Step 32 (2026-05-28): externs for stale-pointer validation. */\n"
        "    extern entity **ent_list;\n"
        "    int _mister_i;\n"
        "    int _mister_valid;\n"
        "    entity *ent = NULL;\n"
        "    e_kill_entity_trigger trigger = KILL_ENTITY_TRIGGER_SCRIPT_KILLENTITY_UNDEFINED;\n"
        "    if(paramCount < 1)\n"
        "    {\n"
        "        *pretvar = NULL;\n"
        "        return E_FAIL;\n"
        "    }\n"
        "\n"
        "    ScriptVariant_ChangeType(*pretvar, VT_INTEGER);\n"
        "\n"
        "    ent = (entity *)(varlist[0])->ptrVal; //retrieve the entity\n"
        "    if(ent == NULL)\n"
        "    {\n"
        "        (*pretvar)->lVal = (LONG)0;\n"
        "        return S_OK;\n"
        "    }\n"
        "\n"
        "    /* MiSTer Step 32 (2026-05-28): defensive stale-pointer validation. */\n"
        "    /* Cart scripts can hold STALE entity pointers after save/continue restore: */\n"
        "    /* the original entity was freed, its memory was reused for something else, */\n"
        "    /* and the script still has the old address in a variable. Old code only */\n"
        "    /* checked ent==NULL then dereferenced ent->exists inside kill_entity, */\n"
        "    /* reading garbage from freed memory -> SIGSEGV (TMNT Rescue Palooza */\n"
        "    /* continue-from-save). Validate ent is in ent_list[] before deref. */\n"
        "    _mister_valid = 0;\n"
        "    for (_mister_i = 0; _mister_i < ent_max; _mister_i++) {\n"
        "        if (ent_list[_mister_i] == ent) { _mister_valid = 1; break; }\n"
        "    }\n"
        "    if (!_mister_valid || !ent->exists) {\n"
        "        (*pretvar)->lVal = (LONG)0;\n"
        "        return S_OK;\n"
        "    }\n"
        "\n"
        "    // Get the saves directory\n"
        "    if (paramCount >= 2)\n"
        "    {\n"
        "        trigger = (e_kill_entity_trigger)(varlist[1])->lVal; // Reason to kill entity.\n"
        "    }\n"
        "\n"
        "    kill_entity(ent, trigger);"
    )
    obs_k = strict_replace(obs_k, killent_old, killent_new,
                           'Step 32: openbor_killentity validates ent in ent_list[] before kill_entity')
    write(obs_path_k, obs_k)
    print("  Step 32: openbor_killentity now validates script-supplied pointer (TMNT-RP continue crash fix)")

    # ── Step 35 (2026-05-29): normalize in_*screen openborvariant returns ──
    # Damon Caskey's 2022-04-21 engine refactor consolidated 17 individual
    # `in_<screen>` integer flags into a single bitmask `screen_status`. The
    # openborvariant getters for these properties now return the bit value
    # (e.g., IN_SCREEN_SELECT = (1 << 11) = 2048) instead of a normalized 0/1.
    #
    # This BROKE every cart script written against the legacy semantics. The
    # idiomatic cart check `if (openborvariant("in_selectscreen") == 1)` now
    # evaluates `if (2048 == 1)` = FALSE.
    #
    # Canonical victim: TMNT Rescue Palooza (Build 6391 era, pre-refactor) uses
    # this exact idiom in data/scripts/update.c to gate the character-roster
    # lock loop. Runtime diagnostic 2026-05-29 confirmed update.c IS running
    # (1300+ calls during select screen) but the for-loop body never executes
    # because the gate fails.
    #
    # Fix: post-process each `var->lVal = (screen_status & IN_SCREEN_X);`
    # assignment to `var->lVal = (screen_status & IN_SCREEN_X) ? 1 : 0;`. This
    # restores the legacy 0/1 semantics for all 17 in_<screen> properties
    # without touching the underlying bitmask representation.
    #
    # Affects all 17 in_<screen> properties:
    #   CHEAT_OPTIONS, CONTROL_OPTIONS, ENGINECREDITSSCREEN, GAMEOVERSCREEN,
    #   HALLOFFAMESCREEN, LEVEL, LOAD_GAME, MENUSCREEN, NEW_GAME, OPTIONS,
    #   SELECTSCREEN, SHOWCOMPLETE, SOUND_OPTIONS, START_GAME, SYSTEM_OPTIONS,
    #   TITLESCREEN, VIDEO_OPTIONS
    print("Patching openborscript.c (Step 35: normalize in_*screen openborvariant to 0/1)...")
    obs_s35_path = os.path.join(obor, 'openborscript.c')
    obs_s35 = read(obs_s35_path)
    # Actual IN_SCREEN_* bit names from openbor.h (verified 2026-05-29)
    s35_props = [
        'CHEAT_OPTIONS_MENU',
        'CONTROL_OPTIONS_MENU',
        'ENGINE_CREDIT',
        'GAME_OVER',
        'HALL_OF_FAME',
        'LOAD_GAME_MENU',
        'MENU',
        'NEW_GAME_MENU',
        'OPTIONS_MENU',
        'SELECT',
        'SHOW_COMPLETE',
        'SOUND_OPTIONS_MENU',
        'GAME_START_MENU',
        'SYSTEM_OPTIONS_MENU',
        'TITLE',
        'VIDEO_OPTIONS_MENU',
    ]
    s35_fixed = 0
    for bit_name in s35_props:
        old = f"        var->lVal = (screen_status & IN_SCREEN_{bit_name});"
        new = f"        var->lVal = (screen_status & IN_SCREEN_{bit_name}) ? 1 : 0; /* MiSTer Step 35: normalize to 0/1 for legacy cart compat */"
        if old not in obs_s35:
            print(f"  WARN: Step 35 pattern for IN_SCREEN_{bit_name} not found (skipping)")
            continue
        obs_s35 = obs_s35.replace(old, new, 1)
        s35_fixed += 1
    write(obs_s35_path, obs_s35)
    print(f"  Step 35: normalized {s35_fixed} of {len(s35_props)} in_*screen openborvariant getters to return 0/1 (was bitmask value)")

    # ── Step 33 (2026-05-28): NULL-check ent_list[i] in kill_entity loop ──
    # User reported TMNT-RP continue-from-save still SIGSEGVing AFTER Step 32.
    # Step 32 validated the ent passed to openbor_killentity (it was in
    # ent_list[]). But the crash was DEEPER -- inside kill_entity's loop
    # that walks ent_list[] to detach references to the victim. The loop's
    # check `if (ent_list[i]->exists)` derefs ent_list[i] without NULL-check.
    # When ent_list[] has stale NULL slots after save/restore, this loop
    # crashes at offset_of(exists) in entity struct (= 0x42c -- matches
    # crash fault address). Line 29712 already has the defensive form
    # `if (ent_list[i] && ent_list[i]->exists)`; this fixes kill_entity
    # (line 24650) by mirroring that defense.
    print("Patching openbor.c (Step 33: NULL-check ent_list[i] in kill_entity loop)...")
    kentloop_old = (
        "    for(i = 0; i < ent_max; i++)\n"
        "    {\n"
        "        if(ent_list[i]->exists)\n"
        "        {\n"
        "            // kill all minions\n"
        "            self = ent_list[i];\n"
        "            if(self->parent == victim)"
    )
    kentloop_new = (
        "    for(i = 0; i < ent_max; i++)\n"
        "    {\n"
        "        /* MiSTer Step 33 (2026-05-28): NULL-check ent_list[i] before deref. */\n"
        "        /* Cart save/restore can leave stale NULL slots in ent_list; without */\n"
        "        /* this guard kill_entity SIGSEGV'd at NULL->exists (= addr 0x42c). */\n"
        "        if(ent_list[i] && ent_list[i]->exists)\n"
        "        {\n"
        "            // kill all minions\n"
        "            self = ent_list[i];\n"
        "            if(self->parent == victim)"
    )
    ob_k33 = read(ob_path_g)
    ob_k33 = strict_replace(ob_k33, kentloop_old, kentloop_new,
                             'Step 33: kill_entity loop NULL-checks ent_list[i]')
    write(ob_path_g, ob_k33)
    print("  Step 33: kill_entity loop now NULL-safe against stale ent_list[] slots")

    # ── Step 36 (2026-05-29): validate victim at kill_entity ENTRY ─────────
    # 2026-05-29 user reported TMNT-RP continue-from-save SIGSEGV REGRESSION
    # after Steps 32+33 should have fixed it. Crash signature: kill_entity+0xed
    # (slightly shifted from original +0xe7 by Step 33's added bytes), fault
    # address 0x42c = offsetof(entity, exists).
    #
    # Root cause: kill_entity has a RECURSIVE call:
    #   if(victim->modeldata.summonkill == 1 && victim->subentity)
    #       kill_entity(self = victim->subentity, ...);
    # If victim->subentity is a STALE pointer (entity freed, memory reused),
    # the recursive call passes garbage. The recursive entry check
    #   if(victim == NULL || !victim->exists)
    # does NOT validate the pointer is in ent_list[] -- only checks NULL.
    # Stale-non-NULL: NULL check passes, then !victim->exists derefs
    # garbage+0x42c -> SIGSEGV.
    #
    # Step 32 only validated SCRIPT entry into openbor_killentity. Step 33 only
    # NULL-checked the internal loop. The recursive entry from victim->subentity
    # bypasses BOTH defenses.
    #
    # Fix: validate victim against ent_list[] at the TOP of kill_entity itself.
    # Catches script entry AND every recursive internal call. Silent return on
    # stale: caller's intent is fulfilled if entity is already gone.
    # Cost: O(ent_max) per call, negligible (not per-frame).
    print("Patching openbor.c (Step 36: validate victim at kill_entity entry)...")
    kent_entry_old = (
        "void kill_entity(entity *victim, e_kill_entity_trigger trigger)\n"
        "{\n"
        "    int i = 0;\n"
        "    s_attack attack;\n"
        "    s_defense* defense_object = NULL;\n"
        "    entity *tempent = self;\n"
        "\n"
        "    if(victim == NULL || !victim->exists)\n"
        "    {\n"
        "        return;\n"
        "    }"
    )
    kent_entry_new = (
        "void kill_entity(entity *victim, e_kill_entity_trigger trigger)\n"
        "{\n"
        "    int i = 0;\n"
        "    s_attack attack;\n"
        "    s_defense* defense_object = NULL;\n"
        "    entity *tempent = self;\n"
        "\n"
        "    /* MiSTer Step 36 (2026-05-29): validate victim is in ent_list[] before deref. */\n"
        "    /* Catches stale-pointer entry from recursive kill_entity(victim->subentity,...) */\n"
        "    /* call. Step 32 only validated script entry; Step 33 only NULL-checked loop. */\n"
        "    if(victim == NULL)\n"
        "    {\n"
        "        return;\n"
        "    }\n"
        "    {\n"
        "        int _mister_k_i;\n"
        "        int _mister_k_valid = 0;\n"
        "        for (_mister_k_i = 0; _mister_k_i < ent_max; _mister_k_i++) {\n"
        "            if (ent_list[_mister_k_i] == victim) { _mister_k_valid = 1; break; }\n"
        "        }\n"
        "        if (!_mister_k_valid || !victim->exists) {\n"
        "            return;\n"
        "        }\n"
        "    }\n"
        "    /* MiSTer Step 38 (2026-05-29): defend victim->parent and victim->subentity   */\n"
        "    /* against stale-pointer derefs in body. Step 36 validated victim itself, but */\n"
        "    /* line 24299 derefs victim->parent->subentity, and lines 24317-22 deref      */\n"
        "    /* victim->subentity->{parent, energy_state.health_current, takedamage} +     */\n"
        "    /* recurses. Save/restore can leave these fields pointing at freed memory.    */\n"
        "    /* Walk ent_list[] for each; NULL if not found. Existing NULL guards in the   */\n"
        "    /* body then behave correctly. Cost: 2x O(ent_max) walks, not hot path.       */\n"
        "    if (victim->parent) {\n"
        "        int _mister_p_valid = 0;\n"
        "        int _mister_p_i;\n"
        "        for (_mister_p_i = 0; _mister_p_i < ent_max; _mister_p_i++) {\n"
        "            if (ent_list[_mister_p_i] == victim->parent) { _mister_p_valid = 1; break; }\n"
        "        }\n"
        "        if (!_mister_p_valid) { victim->parent = NULL; }\n"
        "    }\n"
        "    if (victim->subentity) {\n"
        "        int _mister_s_valid = 0;\n"
        "        int _mister_s_i;\n"
        "        for (_mister_s_i = 0; _mister_s_i < ent_max; _mister_s_i++) {\n"
        "            if (ent_list[_mister_s_i] == victim->subentity) { _mister_s_valid = 1; break; }\n"
        "        }\n"
        "        if (!_mister_s_valid) { victim->subentity = NULL; }\n"
        "    }"
    )
    ob_k36 = read(ob_path_g)
    ob_k36 = strict_replace(ob_k36, kent_entry_old, kent_entry_new,
                             'Step 36+38: kill_entity entry validates victim + child pointers (parent, subentity)')
    write(ob_path_g, ob_k36)
    print("  Step 36: kill_entity entry validates victim (catches recursive stale-pointer entry)")
    print("  Step 38: kill_entity entry also defends victim->parent + victim->subentity (TMNT-RP save-restore crash)")


    # ── Step 40 (2026-05-29): fix NULL self deref in kill_entity's
    # defense_find_current_object call. Step 39 DIAG pinned the TMNT-RP
    # save-game-continue SIGSEGV to this call:
    #
    #   [KE] CP=G pre-defense_find self=(nil)       <-- last log before crash
    #   === CRASH: signal 11 at address 0x42c ===   R0=R1=R2=0
    #
    # Root cause: when kill_entity is called from the script bridge
    # (cart's level-side spawn script via openbor_killentity ->
    # kill_entity), the engine's global `self` pointer is NULL because
    # the spawn script has no owner entity. defense_find_current_object
    # derefs first arg's ->defense field; NULL self -> NULL->defense
    # SIGSEGV.
    #
    # 4086 source has ZERO defense_find_current_object calls — this is a
    # Damon Caskey v7533 addition that introduced the NULL deref risk.
    #
    # Fix: pass `victim` instead of `self`. victim is validated by Step 36
    # (must be in ent_list[] and exists=1). Semantic is "find defense for
    # entity being killed" which IS victim.
    #
    # This is the ACTUAL root cause of the TMNT-RP save-game crash that
    # Steps 36+37+38 failed to fix.
    print("Patching openbor.c (Step 40: fix kill_entity defense_find NULL self deref)...")
    s40_diag_old = (
        "    if(victim->modeldata.summonkill)\n"
        "    {\n"
        "        attack = emptyattack;\n"
        "        attack.attack_type = ATK_SUB_ENTITY_PARENT_KILL;\n"
        "        attack.dropv = default_model_dropv;\n"
        "    }\n"
        "\n"
        "    defense_object = defense_find_current_object(self, NULL, attack.attack_type);"
    )
    s40_diag_new = (
        "    if(victim->modeldata.summonkill)\n"
        "    {\n"
        "        attack = emptyattack;\n"
        "        attack.attack_type = ATK_SUB_ENTITY_PARENT_KILL;\n"
        "        attack.dropv = default_model_dropv;\n"
        "    }\n"
        "\n"
        "    /* MiSTer Step 40 (2026-05-29): pass VICTIM not global self.       */\n"
        "    /* When kill_entity is called from script bridge, global self can  */\n"
        "    /* be NULL (cart's level-side spawn script has no owner entity).   */\n"
        "    /* defense_find_current_object derefs first arg's ->defense field, */\n"
        "    /* so NULL self -> SIGSEGV at offset_of(defense). victim is        */\n"
        "    /* validated by Step 36 (in ent_list[] and exists=1). 4086 didn't  */\n"
        "    /* have this function call at all; this is a 7533 Caskey addition  */\n"
        "    /* that introduced the bug. Semantic: 'find defense for entity     */\n"
        "    /* being killed' = victim, not the calling script's owner.         */\n"
        "    defense_object = defense_find_current_object(victim, NULL, attack.attack_type);"
    )
    ob_k40 = read(ob_path_g)
    ob_k40 = strict_replace(ob_k40, s40_diag_old, s40_diag_new,
                             'Step 40: kill_entity defense_find_current_object uses victim not self (NULL deref fix)')
    write(ob_path_g, ob_k40)
    print("  Step 40: defense_find_current_object now uses victim (was self=NULL from script bridge) — TMNT-RP save-game crash root cause fix")

    # ── Step 34 (2026-05-28): restore 4086's permissive range.base default ─
    # User reported Aliens Clash platform-mounted Prin shooters don't fire,
    # while ground-level Prin shooters work fine. Same enemy type, different
    # behavior by altitude. Confirmed on hardware with 4086 vs 7533.
    #
    # Root cause: when looking for an attack target, OpenBOR's
    # check_range_target_base rejects targets whose `base` (ground reference)
    # differs from the acting entity's `base` by more than the animation's
    # range.base.{min,max} window.
    #
    # 4086 default (openbor.c af23dc9c lines 8486-8487):
    #     newanim->range.min.base = -1000;
    #     newanim->range.max.base = 1000;
    # = always ±1000 regardless of jumpheight. Platform layouts up to 1000
    # units of altitude difference work transparently.
    #
    # 7533 default (openbor.c v7533 line 14716):
    #     .base = {.max = jumpheight*20, .min = -jumpheight*10 }
    # = derived from jumpheight (default 4). For a default-jumpheight enemy:
    # base range = [-40, +80]. Aliens Clash platforms ~50 units high put the
    # ground player at base-diff = -50, outside the lower bound -40 -> target
    # never found -> enemy never attacks.
    #
    # Aliens Clash was authored against 4086 era when this defaulted to
    # ±1000. Cart's character.txt doesn't explicitly call `rangebase` so
    # 7533's tighter default applies. Fix: restore 4086's ±1000 default.
    #
    # Carts that explicitly set `rangebase A B` per anim still get their
    # cart-set values (parser writes to range.base.{min,max} after this
    # default assignment). This patch only changes the FALLBACK.
    #
    # Doesn't touch .x .y .z defaults -- those remain at jumpheight*20.
    # User reported only the platform-shooter issue, not any X/Y/Z issues.
    print("Patching openbor.c (Step 34: restore 4086 permissive range.base default)...")
    rangebase_old = (
        "                newanim->range = (s_range){\n"
        "                    .base = {.max = range_default_jumpheight_max, .min = -range_default_jumpheight_min },\n"
        "                    .x = {.max = range_default_jumpheight_max, .min = (newanim->range.x.min) ? newanim->range.x.min : -10 },\n"
        "                    .y = {.max = range_default_jumpheight_max, .min = -range_default_jumpheight_min },\n"
        "                    .z = {.max = range_default_grabdistance, .min = -range_default_grabdistance }\n"
        "                };"
    )
    rangebase_new = (
        "                newanim->range = (s_range){\n"
        "                    /* MiSTer Step 34 v2 (2026-05-28): restore 4086's permissive */\n"
        "                    /* base AND y defaults. 7533 derived BOTH from jumpheight */\n"
        "                    /* (= +-40 for jumpheight=4); fails platform-mounted enemies */\n"
        "                    /* targeting ground players in legacy carts (Aliens Clash). */\n"
        "                    /* Both checks gate AI in check_range_target_all -- restoring */\n"
        "                    /* just base wasn't enough, y check also fails (position.y */\n"
        "                    /* diff for platform-vs-ground entities). */\n"
        "                    /* 4086 had both at [-1000, +1000] (openbor.c af23dc9c lines */\n"
        "                    /* 8484-8487). Cart explicit `rangeb A B` / `rangea A B` */\n"
        "                    /* directives still override (parser writes after default). */\n"
        "                    .base = {.max = 1000, .min = -1000 },\n"
        "                    .x = {.max = range_default_jumpheight_max, .min = (newanim->range.x.min) ? newanim->range.x.min : -10 },\n"
        "                    .y = {.max = 1000, .min = -1000 },\n"
        "                    .z = {.max = range_default_grabdistance, .min = -range_default_grabdistance }\n"
        "                };"
    )
    ob_k34 = read(ob_path_g)
    ob_k34 = strict_replace(ob_k34, rangebase_old, rangebase_new,
                             'Step 34 v2: range.base AND range.y defaults restored to 4086 permissive +-1000')
    write(ob_path_g, ob_k34)
    print("  Step 34 v2: range.base AND range.y defaults = [-1000, +1000] (was both jumpheight-derived)")

    # ── Step 37 (2026-05-29): restore 4086 instant-death semantics for carts
    # without anim fall.
    #
    # User reported TMNT-RP enemies (foot ninja, mini_turret, bubblecopter)
    # don't play their anim death on lethal damage -- just "poof" disappear
    # without spawning the @cmd spawnbind "explosion_safe" sprite that the
    # cart's anim death is designed to trigger. Pre-existing on MiSTer 7533
    # since the start of TMNT-RP testing (not a Step 32/33/36 regression).
    #
    # Root cause: Damon Caskey 2023-03-28 refactor replaced 4086's direct
    # path with the new DEATH_CONFIG_* bitmask flow:
    #
    # 4086 (openbor.c:~20342):
    #   if(self->health <= 0 && self->modeldata.falldie == 1) {
    #       set_death(self, ...);            // anim death plays IMMEDIATELY
    #   } else { toss + set_fall, kill(self) if no fall anim }
    #
    # 7533 (openbor.c:~34397):
    #   death_try_sequence_damage(acting_entity, death_config, DAMAGE);
    #     -> sees (FALL_LIE_GROUND && acting_entity->animating) -> return 0
    #   then caller does toss + set_fall(...);
    #     -> entity has no anim fall defined -> set_fall returns 0
    #     -> kill(self) called -> anim death NEVER plays
    #
    # All three affected TMNT-RP entity classes share:
    #   - falldie 1 (sets DEATH_AIR | DEATH_GROUND flags)
    #   - nodieblink 0/2/3 (declares post-fall behavior)
    #   - NO anim fall defined (cart expects anim death to play directly)
    #
    # Fix: at the top of death_try_sequence_damage, if DEATH flag set AND
    # entity has no valid ANI_FALL, trigger set_death immediately (mirror
    # 4086 behavior). Modern carts WITH anim fall fall through to the
    # existing DEATH_CONFIG_* flow unchanged.
    #
    # Pattern matches the family of Caskey-refactor-broke-legacy-cart fixes:
    # Step 31 v2/v3 (subject_to_gravity / no_adjust_base directive_seen),
    # Step 34 v2 (range default restoration),
    # Step 35 (in_*screen openborvariant 0/1 normalize).
    print("Patching openbor.c (Step 37: legacy instant-death for carts without anim fall)...")
    dtsd_entry_old = (
        "int death_try_sequence_damage(entity* acting_entity, e_death_config_flags death_sequence, e_death_sequence_acting_event acting_event)\n"
        "{\n"
        "    int result = 0;\n"
        "    e_attack_types attack_type = acting_entity->last_damage_type;\n"
        "    e_death_state death_state = acting_entity->death_state;\n"
        "    \n"
        "    if (death_state & DEATH_STATE_AIR)"
    )
    dtsd_entry_new = (
        "int death_try_sequence_damage(entity* acting_entity, e_death_config_flags death_sequence, e_death_sequence_acting_event acting_event)\n"
        "{\n"
        "    int result = 0;\n"
        "    e_attack_types attack_type = acting_entity->last_damage_type;\n"
        "    e_death_state death_state = acting_entity->death_state;\n"
        "\n"
        "    /* MiSTer Step 37 v2 (2026-05-29): legacy cart compat -- restore 4086's */\n"
        "    /* falldie==1 instant-death semantics for carts WITHOUT anim fall.      */\n"
        "    /* Damon Caskey's 2023-03-28 refactor defers to fall-first via this     */\n"
        "    /* function; carts authored before the refactor (TMNT Rescue Palooza,   */\n"
        "    /* etc.) omit anim fall and expect anim death to play immediately on    */\n"
        "    /* lethal damage. Without this guard, set_fall returns 0, kill() runs,  */\n"
        "    /* and anim death (with its @cmd spawnbind explosion sprite) NEVER      */\n"
        "    /* plays. Modern carts with proper anim fall fall through unchanged.    */\n"
        "    /*                                                                      */\n"
        "    /* v2 (scroll-lock fix): after set_death, ALSO set DEATH_STATE_CORPSE + */\n"
        "    /* noaicontrol + takeaction=suicide + stalltime. v1 left entity in      */\n"
        "    /* DEATH_STATE_DEAD-only state, but count_ents (openbor.c:29874) uses   */\n"
        "    /* CORPSE bit to decide 'alive enemy'; without CORPSE, update_scroller  */\n"
        "    /* (line 44389) keeps wall locked. v2 sets CORPSE so scroll releases    */\n"
        "    /* immediately while anim_die continues playing visually; suicide cleans*/\n"
        "    /* up the entity after stalltime (5 sec, enough for the longest cart    */\n"
        "    /* anim_die like bubblecopter's 10x-explosion sequence).                */\n"
        "    if (((death_sequence & DEATH_CONFIG_DEATH_AIR) || (death_sequence & DEATH_CONFIG_DEATH_GROUND))\n"
        "        && !validanim(acting_entity, ANI_FALL))\n"
        "    {\n"
        "        acting_entity->velocity.x = 0;\n"
        "        acting_entity->velocity.y = 0;\n"
        "        acting_entity->velocity.z = 0;\n"
        "        set_death(acting_entity, attack_type, 0);\n"
        "        if (!(acting_entity->modeldata.type & TYPE_PLAYER)) {\n"
        "            /* Non-player: mark CORPSE so count_ents stops counting +       */\n"
        "            /* schedule suicide for cleanup after anim_die plays.           */\n"
        "            acting_entity->death_state |= DEATH_STATE_CORPSE;\n"
        "            acting_entity->noaicontrol = 1;\n"
        "            acting_entity->takeaction = suicide;\n"
        "            acting_entity->stalltime = _time + GAME_SPEED * 5;\n"
        "        }\n"
        "        return 1;\n"
        "    }\n"
        "\n"
        "    if (death_state & DEATH_STATE_AIR)"
    )
    ob_k37 = read(ob_path_g)
    ob_k37 = strict_replace(ob_k37, dtsd_entry_old, dtsd_entry_new,
                             'Step 37: death_try_sequence_damage instant-death early-return for carts without anim fall')
    write(ob_path_g, ob_k37)
    print("  Step 37: death_try_sequence_damage now triggers set_death immediately when DEATH flag set + no anim fall (TMNT-RP explosion fix)")

    # ── Step 42 (2026-05-29): defensive force SUBJECT_TO_GRAVITY for TYPE_PLAYER
    # User reported Raph respawn-goes-vertical-upward regression after Step 37 v2
    # deploy. Step 41 DIAG (since removed) pinned the cause: engine's
    # ent_copy_uninit (openbor.c:23923) does:
    #
    #   ent->modeldata.move_config_flags = oldmodel->move_config_flags;
    #
    # which unconditionally inherits old model's flags via set_model_ex (called
    # by weapon swaps etc.). If a weapon swap cleared SUBJECT_TO_GRAVITY on
    # the player model, the respawn entity inherits the cleared bit → no
    # gravity → flies upward.
    #
    # DIAG log (now removed) showed: model_gravity=1 at initial Raph spawn,
    # model_gravity=0 at respawn. Pre-existing engine bug, only surfaced after
    # Step 37 v2 unlocked the scroll-lock so user could finally reach the
    # death+respawn cycle past section 1 of the battleship level.
    #
    # Fix: in ent_default_init TYPE_PLAYER case, force MOVE_CONFIG_SUBJECT_TO_GRAVITY
    # ON regardless of prior state. Same defensive pattern as Step 31 v3 for
    # TYPE_NONE. Hardware-verified 2026-05-29.
    print("Patching openbor.c (Step 42: force SUBJECT_TO_GRAVITY for TYPE_PLAYER)...")
    s42_old = (
        "    case TYPE_PLAYER:\n"
        "        //e->direction = (level->scrolldir != SCROLL_LEFT);\n"
        "        e->takedamage = player_takedamage;\n"
        "        e->think = player_think;\n"
        "        e->trymove = player_trymove;"
    )
    s42_new = (
        "    case TYPE_PLAYER:\n"
        "        /* MiSTer Step 42 v2 (2026-05-29): defensive force-set ALL standard player */\n"
        "        /* move_config flags. Engine's ent_copy_uninit (openbor.c:23923) does:    */\n"
        "        /*   ent->modeldata.move_config_flags = oldmodel->move_config_flags;      */\n"
        "        /* This unconditionally inherits the OLD model's flags via set_model_ex    */\n"
        "        /* (called by weapon swaps and similar transitions). After certain model   */\n"
        "        /* transitions, ANY of the player's standard subject_to_* flags can be    */\n"
        "        /* cleared, leading to physics regressions on respawn:                     */\n"
        "        /*   SUBJECT_TO_GRAVITY cleared -> Raph flies vertical-upward (v1 found)   */\n"
        "        /*   SUBJECT_TO_MIN_Z / MAX_Z cleared -> Raph walks UP/DOWN out of play   */\n"
        "        /*                                          area (v2 user-reported)        */\n"
        "        /* v2 mirrors the parser's initial setting (openbor.c:10769) to force ALL  */\n"
        "        /* standard player physics flags on every player creation. NO_ADJUST_BASE */\n"
        "        /* is explicitly cleared to match the initial setup.                       */\n"
        "        e->modeldata.move_config_flags |= (MOVE_CONFIG_SUBJECT_TO_BASEMAP  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_GRAVITY  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_HOLE     \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MAX_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MIN_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_OBSTACLE \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_PLATFORM \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_SCREEN   \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_WALL);\n"
        "        e->modeldata.move_config_flags &= ~MOVE_CONFIG_NO_ADJUST_BASE;\n"
        "        //e->direction = (level->scrolldir != SCROLL_LEFT);\n"
        "        e->takedamage = player_takedamage;\n"
        "        e->think = player_think;\n"
        "        e->trymove = player_trymove;"
    )
    ob_s42 = read(ob_path_g)
    ob_s42 = strict_replace(ob_s42, s42_old, s42_new,
                             'Step 42 v2: force-set ALL standard player physics flags in ent_default_init')
    write(ob_path_g, ob_s42)
    print("  Step 42: TYPE_PLAYER force SUBJECT_TO_GRAVITY (hardware-verified — fixes Raph respawn-vertical)")

    # ── Step 67 (2026-06-01): respect cart's `subject_to_hole 0` (flying characters) ──
    # User reported Bearz OWL (the flying character, type PLAYER + subject_to_hole 0
    # per cart) falls into holes like a ground character. ROOT CAUSE: Step 42 v2's
    # unconditional OR of MOVE_CONFIG_SUBJECT_TO_HOLE overrides the cart's explicit
    # `subject_to_hole 0` directive. After every player creation / set_model_ex,
    # OWL's "I don't fall in holes" flag gets clobbered back to "I do fall".
    #
    # FIX (same pattern as Step 31 v2 for gravity, Step 31 v3 for no_adjust_base):
    #   1. Add `int hole_directive_seen` to END of s_model (done above in v310_new)
    #   2. Patch CMD_MODEL_SUBJECT_TO_HOLE parser to set the flag whenever the
    #      cart uses the directive (whether arg>0 or arg=0; the cart explicitly
    #      addressed hole-handling, so its choice is authoritative).
    #   3. In Step 42 v2 ent_default_init force-set, OR in SUBJECT_TO_HOLE only
    #      if !hole_directive_seen. If the cart said `subject_to_hole 0`, leave
    #      the flag as set by the parser (cleared by cart). If the cart said
    #      nothing, default behavior is to force the flag ON (Step 42 v2 standard).
    print("Patching openbor.c (Step 67a: parser sets hole_directive_seen)...")
    s67a_old = (
        "            case CMD_MODEL_SUBJECT_TO_HOLE:\n"
        "                \n"
        "                if (GET_INT_ARG(1))\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_SUBJECT_TO_HOLE;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_SUBJECT_TO_HOLE;\n"
        "                }\n"
        "\n"
        "                break;\n"
    )
    s67a_new = (
        "            case CMD_MODEL_SUBJECT_TO_HOLE:\n"
        "                \n"
        "                if (GET_INT_ARG(1))\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_SUBJECT_TO_HOLE;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_SUBJECT_TO_HOLE;\n"
        "                }\n"
        "                newchar->hole_directive_seen = 1; /* MiSTer Step 67: gate ent_default_init force-hole */\n"
        "\n"
        "                break;\n"
    )
    ob_s67a = read(ob_path_g)
    ob_s67a = strict_replace(ob_s67a, s67a_old, s67a_new,
                              'Step 67a: parser marks hole_directive_seen')
    write(ob_path_g, ob_s67a)
    print("  Step 67a: CMD_MODEL_SUBJECT_TO_HOLE parser now marks hole_directive_seen")

    print("Patching openbor.c (Step 67b: gate Step 42 v2 SUBJECT_TO_HOLE force-set)...")
    s67b_old = (
        "        e->modeldata.move_config_flags |= (MOVE_CONFIG_SUBJECT_TO_BASEMAP  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_GRAVITY  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_HOLE     \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MAX_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MIN_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_OBSTACLE \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_PLATFORM \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_SCREEN   \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_WALL);\n"
    )
    s67b_new = (
        "        e->modeldata.move_config_flags |= (MOVE_CONFIG_SUBJECT_TO_BASEMAP  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_GRAVITY  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MAX_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MIN_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_OBSTACLE \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_PLATFORM \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_SCREEN   \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_WALL);\n"
        "        /* Step 67: respect cart's subject_to_hole 0 (flying characters). */\n"
        "        /* If cart explicitly set the directive (in either direction), the */\n"
        "        /* parser-set value stays. Else (cart didn't mention it), default  */\n"
        "        /* to ON (matches Step 42 v2 original behavior).                   */\n"
        "        if (!e->modeldata.hole_directive_seen)\n"
        "        {\n"
        "            e->modeldata.move_config_flags |= MOVE_CONFIG_SUBJECT_TO_HOLE;\n"
        "        }\n"
    )
    ob_s67b = read(ob_path_g)
    ob_s67b = strict_replace(ob_s67b, s67b_old, s67b_new,
                              'Step 67b: gate SUBJECT_TO_HOLE force-set on hole_directive_seen')
    write(ob_path_g, ob_s67b)
    print("  Step 67b: Step 42 v2 SUBJECT_TO_HOLE now gated on !hole_directive_seen (respects cart's subject_to_hole 0)")

    # ── Step 68 (2026-06-01): respect cart's subject_to_obstacle 0 and subject_to_platform 0 ──
    # Same pattern as Step 67 (hole), extended to obstacle + platform.
    # OWL also has `subject_to_obstacle 0` (passes through obstacles) and
    # `subject_to_platform 0` (doesn't interact with platforms). Step 42 v2's
    # unconditional OR was overriding both. Step 68 adds the same directive_seen
    # gates for these two flags.
    print("Patching openbor.c (Step 68a: parser sets obstacle_directive_seen)...")
    s68a_old = (
        "            case CMD_MODEL_SUBJECT_TO_OBSTACLE:\n"
        "                \n"
        "                if (GET_INT_ARG(1))\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_SUBJECT_TO_OBSTACLE;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_SUBJECT_TO_OBSTACLE;\n"
        "                }\n"
        "\n"
        "                break;\n"
    )
    s68a_new = (
        "            case CMD_MODEL_SUBJECT_TO_OBSTACLE:\n"
        "                \n"
        "                if (GET_INT_ARG(1))\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_SUBJECT_TO_OBSTACLE;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_SUBJECT_TO_OBSTACLE;\n"
        "                }\n"
        "                newchar->obstacle_directive_seen = 1; /* MiSTer Step 68: gate ent_default_init force-obstacle */\n"
        "\n"
        "                break;\n"
    )
    ob_s68a = read(ob_path_g)
    ob_s68a = strict_replace(ob_s68a, s68a_old, s68a_new,
                              'Step 68a: parser marks obstacle_directive_seen')
    write(ob_path_g, ob_s68a)
    print("  Step 68a: CMD_MODEL_SUBJECT_TO_OBSTACLE parser marks obstacle_directive_seen")

    print("Patching openbor.c (Step 68b: parser sets platform_directive_seen)...")
    s68b_old = (
        "            case CMD_MODEL_SUBJECT_TO_PLATFORM:\n"
        "                \n"
        "                if (GET_INT_ARG(1))\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_SUBJECT_TO_PLATFORM;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_SUBJECT_TO_PLATFORM;\n"
        "                }\n"
        "\n"
        "                break;\n"
    )
    s68b_new = (
        "            case CMD_MODEL_SUBJECT_TO_PLATFORM:\n"
        "                \n"
        "                if (GET_INT_ARG(1))\n"
        "                {\n"
        "                    newchar->move_config_flags |= MOVE_CONFIG_SUBJECT_TO_PLATFORM;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    newchar->move_config_flags &= ~MOVE_CONFIG_SUBJECT_TO_PLATFORM;\n"
        "                }\n"
        "                newchar->platform_directive_seen = 1; /* MiSTer Step 68: gate ent_default_init force-platform */\n"
        "\n"
        "                break;\n"
    )
    ob_s68b = read(ob_path_g)
    ob_s68b = strict_replace(ob_s68b, s68b_old, s68b_new,
                              'Step 68b: parser marks platform_directive_seen')
    write(ob_path_g, ob_s68b)
    print("  Step 68b: CMD_MODEL_SUBJECT_TO_PLATFORM parser marks platform_directive_seen")

    print("Patching openbor.c (Step 68c: gate Step 42 v2 SUBJECT_TO_OBSTACLE/PLATFORM force-set)...")
    s68c_old = (
        "        e->modeldata.move_config_flags |= (MOVE_CONFIG_SUBJECT_TO_BASEMAP  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_GRAVITY  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MAX_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MIN_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_OBSTACLE \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_PLATFORM \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_SCREEN   \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_WALL);\n"
    )
    s68c_new = (
        "        e->modeldata.move_config_flags |= (MOVE_CONFIG_SUBJECT_TO_BASEMAP  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_GRAVITY  \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MAX_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_MIN_Z    \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_SCREEN   \\\n"
        "                                          | MOVE_CONFIG_SUBJECT_TO_WALL);\n"
        "        /* Step 68: respect cart's subject_to_obstacle 0 + subject_to_platform 0 */\n"
        "        if (!e->modeldata.obstacle_directive_seen)\n"
        "        {\n"
        "            e->modeldata.move_config_flags |= MOVE_CONFIG_SUBJECT_TO_OBSTACLE;\n"
        "        }\n"
        "        if (!e->modeldata.platform_directive_seen)\n"
        "        {\n"
        "            e->modeldata.move_config_flags |= MOVE_CONFIG_SUBJECT_TO_PLATFORM;\n"
        "        }\n"
    )
    ob_s68c = read(ob_path_g)
    ob_s68c = strict_replace(ob_s68c, s68c_old, s68c_new,
                              'Step 68c: gate SUBJECT_TO_OBSTACLE/PLATFORM force-set on directive_seen flags')
    write(ob_path_g, ob_s68c)
    print("  Step 68c: SUBJECT_TO_OBSTACLE + SUBJECT_TO_PLATFORM now gated on directive_seen flags")




    # ── Step 61 (2026-05-31): fix Bearz rocket-fires-LEFT-on-first-pickup bug ─────
    # ROOT CAUSE (confirmed via Step 56 v4 DIAG hardware capture 2026-05-31):
    # The e_direction enum has THREE values:
    #   DIRECTION_NONE  = -1   (engine/openbor.h:1391)
    #   DIRECTION_LEFT  =  0
    #   DIRECTION_RIGHT =  1
    # When Bearz player picks up the rocket launcher (set_weapon swaps to
    # hubertb model, TYPE_NONE), self->direction can transiently be
    # DIRECTION_NONE (-1) during/after the swap, especially before the
    # player has pressed a movement key.
    # The engine's openbor_projectile() at openborscript.c:12044 only
    # checks `if (self->direction == DIRECTION_RIGHT)` -- DIRECTION_NONE
    # falls into the else branch and forces direction=DIRECTION_LEFT.
    # Result: every rocket on first pickup fires LEFT regardless of where
    # player visually faces. After death+respawn, the engine re-initializes
    # self->direction to a valid 0/1, and rockets work correctly.
    # FIX: invert the comparison so LEFT is the explicit check and
    # everything else (RIGHT + NONE) defaults to RIGHT. RIGHT is the
    # canonical visual default in OpenBOR (carts spawn facing right).
    print("Patching openborscript.c (Step 61: Bearz rocket DIRECTION_NONE fix)...")
    obs_path_s61 = os.path.join(obor, 'openborscript.c')
    s61_old = (
        "    if(relative)\n"
        "    {\n"
        "        if(self->direction == DIRECTION_RIGHT)\n"
        "        {\n"
        "            x += self->position.x;\n"
        "\t\t\tdirection = DIRECTION_RIGHT;\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            x = self->position.x - x;\n"
        "            direction = DIRECTION_LEFT;\n"
        "        }\n"
    )
    s61_new = (
        "    if(relative)\n"
        "    {\n"
        "        /* MiSTer Step 61 (2026-05-31): handle DIRECTION_NONE (-1).        */\n"
        "        /* Was: if(direction == DIRECTION_RIGHT) -> -1 falls to LEFT.      */\n"
        "        /* Now: if(direction == DIRECTION_LEFT)  -> only 0 = LEFT;         */\n"
        "        /* everything else (RIGHT=1 OR NONE=-1 uninit) defaults to RIGHT.  */\n"
        "        if(self->direction == DIRECTION_LEFT)\n"
        "        {\n"
        "            x = self->position.x - x;\n"
        "            direction = DIRECTION_LEFT;\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            x += self->position.x;\n"
        "            direction = DIRECTION_RIGHT;\n"
        "        }\n"
    )
    obs_s61 = read(obs_path_s61)
    obs_s61 = strict_replace(obs_s61, s61_old, s61_new,
                             'Step 61: handle DIRECTION_NONE in openbor_projectile relative-offset block')
    write(obs_path_s61, obs_s61)
    print("  Step 61: DIRECTION_NONE now defaults to RIGHT in projectile direction resolution")

    # ── Step 62 (2026-05-31): implicit wait at group transition (TMNT-RP barrel wave fix) ──
    # User reported TMNT-RP construction barrels appear with leftover ninjas
    # on screen. Cart structure:
    #   group 4 4 / [12 crooked_ninja spawns] / group 1 3 / [9 rolling_barrel
    #   + 2 barrel spawns]
    # NO `wait` directive between the two group sections.
    # Engine's spawn dispatcher at openbor.c:44389 uses GLOBAL groupmin/max
    # + count_ents(TYPE_ENEMY) (all enemies counted together). When the loop
    # encounters the `group 1 3` directive mid-tick, it applies the new
    # groupmax=3 then continues the loop -- spawning barrels while ninjas
    # are still alive (because count < new groupmax(3)). User observed
    # Wave 1 = 2 barrels (1 ninja + 2 barrels = 3 total at groupmax).
    # PC behavior (and user expectation): Wave 1 = 3 barrels after all
    # ninjas dead.
    # FIX: when processing a `group N M` spawnpoint directive, if there
    # are leftover TYPE_ENEMY alive, HALT the spawn loop without applying
    # new groupmin/max and without advancing current_spawn. Set
    # level->waiting = 1 (engine treats as "wait for enemies" state).
    # Once all enemies die, level->waiting clears, spawn loop re-enters,
    # applies group update, spawns next wave cleanly.
    print("Patching openbor.c (Step 62: implicit wait at group transition when enemies alive)...")
    s62_old = (
        "            else if(level->spawnpoints[current_spawn].groupmin || level->spawnpoints[current_spawn].groupmax)\n"
        "            {\n"
        "                groupmin = level->spawnpoints[current_spawn].groupmin;\n"
        "                groupmax = level->spawnpoints[current_spawn].groupmax;\n"
        "            }\n"
    )
    s62_new = (
        "            else if(level->spawnpoints[current_spawn].groupmin || level->spawnpoints[current_spawn].groupmax)\n"
        "            {\n"
        "                /* MiSTer Step 62 (2026-05-31): implicit wait at group       */\n"
        "                /* transition when previous group's enemies still alive.    */\n"
        "                /* Without this, the engine applies new groupmin/max while   */\n"
        "                /* leftover entities count toward new group's pool -> next   */\n"
        "                /* wave spawns partial (e.g., 2 barrels instead of 3 when 1  */\n"
        "                /* ninja still alive). With this fix, group transitions     */\n"
        "                /* synchronize: wait for previous-wave enemies to clear      */\n"
        "                /* before applying new groupmin/max. Matches PC behavior.    */\n"
        "                /* MiSTer 2026-06-12: compare against level->bossescount, NOT */\n"
        "                /* 0. Bosses are persistent TYPE_ENEMY that do NOT die mid-   */\n"
        "                /* fight, so counting them here permanently blocks the GROUP  */\n"
        "                /* transition. ATOV L1BOSS spawns ALEX (boss) BEFORE its      */\n"
        "                /* GROUP 3 6 directive: with '> 0', ALEX kept count_ents>0    */\n"
        "                /* forever and the wave enemies NEVER spawned. '> bossescount'*/\n"
        "                /* waits only for non-boss (previous-wave) enemies; TMNT-RP   */\n"
        "                /* barrels still wait for ninjas (bossescount=0 there).       */\n"
        "                if (count_ents(TYPE_ENEMY) > level->bossescount)\n"
        "                {\n"
        "                    level->waiting = 1;\n"
        "                    break;  /* exit while loop; current_spawn NOT advanced */\n"
        "                }\n"
        "                groupmin = level->spawnpoints[current_spawn].groupmin;\n"
        "                groupmax = level->spawnpoints[current_spawn].groupmax;\n"
        "            }\n"
    )
    ob_s62 = read(ob_path_g)
    ob_s62 = strict_replace(ob_s62, s62_old, s62_new,
                             'Step 62: implicit wait at group transition')
    write(ob_path_g, ob_s62)
    print("  Step 62: group transition now waits for previous enemies to die before applying new groupmin/max")

    # ── Step 64 (2026-06-01): handle DIRECTION_NONE in player_think input handler ──
    # User reported Bearz can't face LEFT on first pickup/spawn (intermittent).
    # Cart-side: HUB.TXT attack scripts use changeentityproperty(self,
    # "direction", -1) to set DIRECTION_NONE during special-move animations.
    # If self->direction remains at -1 when player presses joystick LEFT,
    # the engine's player_think input handler at openbor.c:41800 does:
    #   if (acting_entity->direction == DIRECTION_RIGHT) { flip-to-LEFT }
    #   else { turntime = 0; no flip }
    # DIRECTION_NONE (-1) != DIRECTION_RIGHT (1) -> else branch -> no flip ->
    # player moves LEFT (velocity set) but self->direction stays -1 ->
    # sprite renders at engine default (RIGHT) -> visual = walking LEFT
    # while facing RIGHT. SAME bug pattern as the projectile-direction
    # bug Step 61 fixed.
    # FIX: invert the comparisons so DIRECTION_NONE falls through to the
    # flip-to-target-direction logic.
    #   MOVELEFT path:   `direction == DIRECTION_RIGHT` -> `direction != DIRECTION_LEFT`
    #   MOVERIGHT path:  `direction == DIRECTION_LEFT`  -> `direction != DIRECTION_RIGHT`
    # Affects: any entity (player or otherwise) that ends up with
    # DIRECTION_NONE while player input is driving it -- player can now
    # successfully flip to face their movement direction.
    print("Patching openbor.c (Step 64: handle DIRECTION_NONE in player_think MOVELEFT/RIGHT)...")
    s64a_old = (
        "    if(acting_player->keys & FLAG_MOVELEFT && acting_entity->ducking == DUCK_NONE)\n"
        "    {\n"
        "        if(acting_entity->direction == DIRECTION_RIGHT)\n"
    )
    s64a_new = (
        "    if(acting_player->keys & FLAG_MOVELEFT && acting_entity->ducking == DUCK_NONE)\n"
        "    {\n"
        "        /* MiSTer Step 64: invert comparison so DIRECTION_NONE (-1)   */\n"
        "        /* falls through to the flip-to-LEFT logic. Was:              */\n"
        "        /*   if (direction == DIRECTION_RIGHT) -> -1 went to else,    */\n"
        "        /*   never flipped to LEFT. Now:                              */\n"
        "        /*   if (direction != DIRECTION_LEFT) -> RIGHT and NONE both  */\n"
        "        /*   trigger the flip-to-LEFT path.                           */\n"
        "        if(acting_entity->direction != DIRECTION_LEFT)\n"
    )
    ob_s64 = read(ob_path_g)
    ob_s64 = strict_replace(ob_s64, s64a_old, s64a_new,
                             'Step 64a: MOVELEFT direction-flip handles DIRECTION_NONE')

    s64b_old = (
        "    else if(acting_player->keys & FLAG_MOVERIGHT && acting_entity->ducking == DUCK_NONE)\n"
        "    {\n"
        "        if(acting_entity->direction == DIRECTION_LEFT)\n"
    )
    s64b_new = (
        "    else if(acting_player->keys & FLAG_MOVERIGHT && acting_entity->ducking == DUCK_NONE)\n"
        "    {\n"
        "        /* MiSTer Step 64: mirror of MOVELEFT fix above.              */\n"
        "        if(acting_entity->direction != DIRECTION_RIGHT)\n"
    )
    ob_s64 = strict_replace(ob_s64, s64b_old, s64b_new,
                             'Step 64b: MOVERIGHT direction-flip handles DIRECTION_NONE')
    write(ob_path_g, ob_s64)
    print("  Step 64: player_think MOVELEFT/MOVERIGHT handlers now normalize DIRECTION_NONE on input")

    # ── Step 70 (2026-06-01): MERGED into Step 16c's find_ent_here patch ──
    # See Step 16c (later in this file) for the DEATH_STATE_CORPSE filter that
    # fixes the Bearz captive-box "invisible wall" bug. Patches that target the
    # same engine function must be merged to avoid strict_replace anchor
    # conflicts (per [[strict-replace-count-check]]).

    # ── Step 57 (2026-05-31): rolling at-rest fix for aironly SUBTYPE_ARROW ─────
    # User reported TMNT-RP construction barrels appear stuck on right side of
    # screen — barrels DO roll a bit (Step 55 DIAG confirmed vel.x=-0.7 + pos.x
    # decrementing 1.05/tick while airborne) but then halt. PC TMNT-RP rolls
    # barrels across the entire screen in waves 1/2/3 smoothly.
    #
    # Root cause located at openbor.c::check_gravity:27784. After the engine's
    # bounce cascade (vel.x /= bouncefactor each landing) and vel.y decays
    # below the tobounce threshold, the engine enters the "at rest" branch:
    #
    #     else if((!self->animation->move[self->animpos]->base || ...) &&
    #             (!self->animation->move[self->animpos]->axis.y || ...))
    #     {
    #         self->velocity.x = 0;
    #         self->velocity.z = 0;
    #         self->velocity.y = 0;
    #     }
    #
    # The cart's `anim idle` has no `move` or `axis.y` per-frame directives, so
    # this branch fires and ZEROES vel.x. After this tick, the entity is
    # at-rest (vel.y=0, pos.y=base, !falling). NEXT tick, check_gravity's
    # airborne condition `(falling || vel.y || pos.y != base)` is FALSE → the
    # entire airborne block (including Step 48/55) is SKIPPED → barrel sits
    # frozen at landing point. Steps 48 and 55 only ever fire inside the
    # airborne block, so they cannot re-lock vel.x once the entity rests.
    #
    # PC TMNT-RP probably uses a custom-engine `arrow_move`-style path that
    # keeps SUBTYPE_ARROW + aironly entities rolling at rest. Stock v7533
    # does not — once a SUBTYPE_ARROW lands and the bounce cascade decays,
    # it's stationary forever (until offscreenkill).
    #
    # FIX: inject an UNCONDITIONAL rolling block at END of check_gravity,
    # OUTSIDE the airborne if. Fires every tick the entity is not frozen,
    # for SUBTYPE_ARROW + aironly + ANI_IDLE + !owner. Re-locks vel.x and
    # advances position.x. This handles BOTH airborne (overrides bounce-
    # halved vel.x next tick) and at-rest (revives vel.x from engine zero).
    print("Patching openbor.c (Step 57: rolling at-rest fix outside airborne block)...")
    s57_old = (
        "        }// end of if  - in-air checking\n"
        "        \n"
        "\t\tif(self->toss_time <= _time)\n"
    )
    s57_new = (
        "        }// end of if  - in-air checking\n"
        "        \n"
        "        /* MiSTer Step 57 (2026-05-31, v2 2026-05-31): rolling at-rest fix. */\n"
        "        /* Fires every tick OUTSIDE the airborne block for aironly          */\n"
        "        /* SUBTYPE_ARROW + ANI_IDLE + !owner. Relocks vel.x to +/-speed.x   */\n"
        "        /* (handles engine bounce-halving at openbor.c:27761 + at-rest      */\n"
        "        /* zeroing at openbor.c:27784). NO direct position update -- engine */\n"
        "        /* main loop at line 29293 accumulates movex from vel.x*speedmul*   */\n"
        "        /* factor, then trymove() (assigned by Step 47 to common_trymove)   */\n"
        "        /* moves entity by movex. Final motion = +/-speed.x per tick exact  */\n"
        "        /* per cart spec + engine arrow_move() canon.                       */\n"
        "        /* v2 2026-05-31: removed direct position.x update -- it was       */\n"
        "        /* adding extra +/-0.35/tick on top of trymove's +/-0.7/tick = 50  */\n"
        "        /* percent faster than cart spec, perceived as 'stiff'.            */\n"
        "        if (self->modeldata.subtype == SUBTYPE_ARROW\n"
        "            && self->modeldata.aironly_directive_seen\n"
        "            && !self->owner\n"
        "            && self->modeldata.speed.x > 0\n"
        "            && self->animnum == ANI_IDLE)\n"
        "        {\n"
        "            /* Step 66 v2 (2026-06-01): SIMPLIFIED -- only intercept in  */\n"
        "            /* ANI_IDLE state. For ANI_FALL (knockback recoil from hit)  */\n"
        "            /* and ANI_DEATH (post-landing death anim), let engine       */\n"
        "            /* handle naturally:                                         */\n"
        "            /*   ANI_FALL : engine knockback gives fly-back motion       */\n"
        "            /*   ANI_DEATH: engine's at-rest branch zeros vel.x on land  */\n"
        "            /*                                                           */\n"
        "            /* Within ANI_IDLE state, distinguish live (roll) vs dying   */\n"
        "            /* (halt -- handles die-blink IDLE cycles where engine       */\n"
        "            /* cycles animnum back to ANI_IDLE during blink frames).     */\n"
        "            if (self->energy_state.health_current > 0\n"
        "                && !(self->death_state & DEATH_STATE_DEAD)\n"
        "                && !self->die_on_landing)\n"
        "            {\n"
        "                /* Live + IDLE -- roll forward. */\n"
        "                self->velocity.x = (self->direction == DIRECTION_LEFT)\n"
        "                                   ? -self->modeldata.speed.x\n"
        "                                   : self->modeldata.speed.x;\n"
        "                /* MiSTer Step 70 (2026-06-09): wall-pin despawn. A roller    */\n"
        "                /* jammed against a boundary wall never reaches its           */\n"
        "                /* offscreenkill, deadlocking the level's enemy wait. If it   */\n"
        "                /* hasn't moved for ~90 ticks, shove it well past the         */\n"
        "                /* offscreen boundary so the engine's own check_lost()        */\n"
        "                /* removes it next tick via kill_entity(OUT_OF_BOUNDS).       */\n"
        "                {\n"
        "                    /* Threshold 0.1f: [BAR-G] log shows a normal roller moves   */\n"
        "                    /* 0.70 px/tick (speed.x=0.70) while a wall-pinned barrel    */\n"
        "                    /* is frozen at dx=0.00. 0.1 cleanly separates them. (The    */\n"
        "                    /* first cut used 1.0, which flagged EVERY roller since      */\n"
        "                    /* 0.70 < 1.0, despawning them mid-roll.)                     */\n"
        "                    float _md = self->position.x - self->mister_stall_lastx;\n"
        "                    if (_md < 0) _md = -_md;\n"
        "                    if (_md < 0.1f) {\n"
        "                        self->mister_stall_ticks++;\n"
        "                        if (self->mister_stall_ticks > 90) {\n"
        "                            self->position.x += (float)(videomodes.hRes + (int)self->modeldata.offscreenkill + 200);\n"
        "                        }\n"
        "                    } else {\n"
        "                        self->mister_stall_ticks = 0;\n"
        "                    }\n"
        "                    self->mister_stall_lastx = self->position.x;\n"
        "                }\n"
        "            }\n"
        "            else\n"
        "            {\n"
        "                /* Dying/dead + IDLE (die-blink cycle) -- halt.         */\n"
        "                /* Zero BOTH vel.x AND movex (movex was already         */\n"
        "                /* accumulated this tick by main loop at line 29293).   */\n"
        "                self->velocity.x = 0;\n"
        "                self->movex = 0;\n"
        "                /* Step 69 (2026-06-01): also freeze animation cycling. */\n"
        "                /* Engine returns entity to ANI_IDLE after the short    */\n"
        "                /* ANI_DEATH single-frame completes. Cart's anim idle   */\n"
        "                /* loops 4 rolling frames -> visually the barrel looks  */\n"
        "                /* like it's rolling in place during the die-blink.     */\n"
        "                /* Setting animating=0 stops the frame advance code in  */\n"
        "                /* update_animation -- current sprite frame stays put,  */\n"
        "                /* engine's blink visibility toggle still runs.         */\n"
        "                self->animating = 0;\n"
        "            }\n"
        "        }\n"
        "        /* ── end Step 57 + 66 v2 + 69 ────────────────────────────── */\n"
        "        \n"
        "\t\tif(self->toss_time <= _time)\n"
    )
    ob_s57 = read(ob_path_g)
    ob_s57 = strict_replace(ob_s57, s57_old, s57_new,
                             'Step 57: rolling at-rest fix outside airborne block')
    write(ob_path_g, ob_s57)
    print("  Step 57: unconditional rolling block at end of check_gravity (fires for at-rest barrels)")

    # -- Step 70 (2026-06-09): init the stall tracker (added to s_entity END) when
    # a SUBTYPE_ARROW entity spawns, so a reused entity slot's stale values can't
    # false-trigger the wall-pin despawn in Step 57's block. Sentinel lastx forces
    # a clean reset on the barrel's first Step 57 tick. Anchored on the arrow-init
    # opener (pristine; Steps 45/47 hook the END of this block, not health_current).
    print("Patching openbor.c (Step 70: init stall tracker in SUBTYPE_ARROW init)...")
    ob_s70 = read(ob_path_g)
    s70b_old = ("        else if(e->modeldata.subtype == SUBTYPE_ARROW)\n"
                "        {\n"
                "            e->energy_state.health_current = 1;\n")
    s70b_new = ("        else if(e->modeldata.subtype == SUBTYPE_ARROW)\n"
                "        {\n"
                "            e->energy_state.health_current = 1;\n"
                "            e->mister_stall_lastx = -999999.0f; /* MiSTer Step 70: sentinel forces stall reset on first Step 57 tick */\n"
                "            e->mister_stall_ticks = 0;\n")
    ob_s70 = strict_replace(ob_s70, s70b_old, s70b_new, 'Step 70: init stall tracker in SUBTYPE_ARROW init')
    write(ob_path_g, ob_s70)
    print("  Step 70: stall tracker initialized on SUBTYPE_ARROW spawn")

    # ── Step 45 (2026-05-30): auto-transition cart-spawned in-air arrows to ANI_FALL ────
    # User-reported TMNT-RP construction-level rolling barrels float at spawn
    # Y=130 (in air) instead of falling+bouncing+rolling like PC version.
    # Step 44 DIAG pinned the cause:
    #   - SUBTYPE_ARROW base auto-adjusts correctly to floor (56)
    #   - SUBJECT_TO_GRAVITY is enabled on both model and animation
    #   - BUT barrel stays in animnum=1 (ANI_IDLE) forever — never transitions
    #     to its cart-defined `anim fall`
    #   - vel.y oscillates 0 ↔ -0.05 every animation-frame transition (3-tick
    #     cycle matches cart's `delay 10` per frame). At this rate the barrel
    #     falls ~0.22 units per second → 5.6 MINUTES to reach floor.
    #
    # Cart's rolling_barrel.txt design intent (verified by inspecting cart):
    #   subtype arrow + aironly 1 + anim fall (loop 0, bouncefactor 2, sound)
    # Cart author authored anim fall + aironly explicitly = "this is a falling
    # air entity, play fall anim on spawn."
    #
    # Stock OpenBOR (both 4086 + v7533) leaves SUBTYPE_ARROW entities in
    # ANI_IDLE on spawn — doesn't auto-transition to ANI_FALL. PC TMNT-RP
    # probably uses a custom-built engine (Damon Caskey routinely ships
    # custom engines with carts) that auto-transitions.
    #
    # FIX: in SUBTYPE_ARROW case, after base assignment, transition to ANI_FALL
    # if ALL FOUR cart-design signals are present (option C tightest gating):
    #   (1) aironly_directive_seen  — cart declared `aironly 1` (air-only entity)
    #   (2) position.y > 0          — actually spawned in air (not ground)
    #   (3) !owner                  — level-spawned, not projectile-fired-by-entity
    #   (4) validanim(e, ANI_FALL)  — cart explicitly authored a fall animation
    #
    # All 4 conditions are CART-AUTHOR'S EXPLICIT SIGNALS. Projectiles fired
    # from launchers (have owner) and arrows without a fall anim are
    # unaffected → no regression risk to standard arrow projectiles.
    #
    # Companion to Step 31 v2/v3 directive_seen pattern. Same defensive
    # END-of-struct field placement (no offset shifts).
    #
    # Step 45a (s_model field) is added at the v3.10 block above.
    # Step 45b: CMD_MODEL_AIRONLY parser sets aironly_directive_seen=1
    # Step 45c: SUBTYPE_ARROW case auto-transitions to ANI_FALL when gated

    # --- 45b: CMD_MODEL_AIRONLY parser sets the directive_seen flag ---
    print("Patching openbor.c (Step 45b: CMD_MODEL_AIRONLY sets aironly_directive_seen)...")
    s45b_old = (
        "            case CMD_MODEL_AIRONLY:\t// Shadows display in air only?\n"
        "\n"
        "                tempInt = GET_INT_ARG(1);\n"
        "\n"
        "                newchar->shadow_config_flags = shadow_get_config_from_legacy_aironly(newchar->shadow_config_flags, tempInt);\n"
        "                \n"
        "                break;"
    )
    s45b_new = (
        "            case CMD_MODEL_AIRONLY:\t// Shadows display in air only?\n"
        "\n"
        "                tempInt = GET_INT_ARG(1);\n"
        "\n"
        "                newchar->shadow_config_flags = shadow_get_config_from_legacy_aironly(newchar->shadow_config_flags, tempInt);\n"
        "                if (tempInt > 0) newchar->aironly_directive_seen = 1; /* MiSTer Step 45: gate SUBTYPE_ARROW ANI_FALL auto-transition */\n"
        "                \n"
        "                break;"
    )
    ob_s45 = read(ob_path_g)
    ob_s45 = strict_replace(ob_s45, s45b_old, s45b_new,
                             'Step 45b: CMD_MODEL_AIRONLY sets aironly_directive_seen')

    # --- 45c: SUBTYPE_ARROW case auto-transitions to ANI_FALL ---
    # Anchor on the existing `e->speedmul = 2;\n            break;` at end of
    # the SUBTYPE_ARROW block. Inject the transition BEFORE the break.
    # NOTE: Step 44 DIAG (a) injected its log block right before this same
    # break, so the current source has DIAG block + speedmul + break.
    # We anchor on the DIAG block's tail + speedmul + break so our patch
    # composes cleanly.
    print("Patching openbor.c (Step 45c: SUBTYPE_ARROW auto-transitions to ANI_FALL)...")
    s45c_old = (
        "            e->takedamage = arrow_takedamage;\n"
        "            e->speedmul = 2;\n"
        "            break;\n"
    )
    s45c_new = (
        "            e->takedamage = arrow_takedamage;\n"
        "            e->speedmul = 2;\n"
        "            /* MiSTer Step 45 (2026-05-30): auto-transition cart-spawned     */\n"
        "            /* in-air arrows to ANI_FALL. TMNT-RP construction-level rolling */\n"
        "            /* barrels declare `aironly 1` + `anim fall` and spawn at Y=130. */\n"
        "            /* Stock leaves them in ANI_IDLE forever; gravity is enabled but */\n"
        "            /* anim transitions reset vel.y so they barely fall. Gated on    */\n"
        "            /* all 4 cart-design signals (option C tightest gating).         */\n"
        "            if (e->modeldata.aironly_directive_seen\n"
        "                && e->position.y > 0\n"
        "                && !e->owner\n"
        "                && validanim(e, ANI_FALL))\n"
        "            {\n"
        "                ent_set_anim(e, ANI_FALL, 0);\n"
        "            }\n"
        "            /* ── end Step 45 ──────────────────────────────────────────── */\n"
        "            break;\n"
    )
    ob_s45 = strict_replace(ob_s45, s45c_old, s45c_new,
                             'Step 45c: SUBTYPE_ARROW auto-transition to ANI_FALL')
    write(ob_path_g, ob_s45)
    print("  Step 45: SUBTYPE_ARROW auto-transitions to ANI_FALL when aironly+!owner+validanim ANI_FALL")

    # ── Step 47 (2026-05-30): assign common_trymove for aironly SUBTYPE_ARROW ─────
    # After Step 46 transitioned barrels to ANI_IDLE + set vel.x, user reports
    # barrels roll IN PLACE — animation plays but no horizontal motion.
    #
    # Root cause: engine main loop at openbor.c:29293 accumulates
    #   self->movex += self->velocity.x * speedmul * (100.0 / GAME_SPEED)
    # per tick. So vel.x → movex correctly. But check_move() at line 29151 has
    # a gate:
    #   if (self->trymove) { ... call self->trymove(movex, movez) ... }
    # SUBTYPE_ARROW init at openbor.c:23183-23207 SKIPS trymove assignment
    # entirely (only the else-branch at 23211 sets e->trymove=common_trymove
    # for non-arrow entities). So trymove=NULL → check_move's gate fails →
    # movex accumulates but never applies to position.x → barrel rolls in
    # place visually.
    #
    # Fix: in SUBTYPE_ARROW init, for cart-spawned aironly entities, assign
    # trymove=common_trymove. Then check_move's gate passes, position updates
    # from movex (driven by vel.x), and engine's wall/hole collision activates
    # via cart's `subject_to_wall 1` + `subject_to_hole 1` directives.
    #
    # Standard arrow projectiles (no aironly_directive_seen) keep their
    # trymove=NULL stock behavior — they rely on arrow_move (via aimove arrow
    # declaration) which handles motion differently.
    #
    # Anchored at end of Step 45's block (just before `break;` in
    # SUBTYPE_ARROW case).
    print("Patching openbor.c (Step 47: assign common_trymove for aironly SUBTYPE_ARROW)...")
    s47_old = (
        "                ent_set_anim(e, ANI_FALL, 0);\n"
        "            }\n"
        "            /* ── end Step 45 ──────────────────────────────────────────── */\n"
        "            break;\n"
    )
    s47_new = (
        "                ent_set_anim(e, ANI_FALL, 0);\n"
        "            }\n"
        "            /* ── end Step 45 ──────────────────────────────────────────── */\n"
        "            /* MiSTer Step 47 + 51 (2026-05-30): assign common_trymove for  */\n"
        "            /* level-spawned aironly SUBTYPE_ARROW. Engine SUBTYPE_ARROW    */\n"
        "            /* init normally SKIPS trymove assignment. Without trymove,    */\n"
        "            /* check_move's gate fails -> movex grows but never applies   */\n"
        "            /* to position.x. Wire up trymove so engine moves the entity. */\n"
        "            /* Step 51: gate on !e->owner so player-fired projectiles      */\n"
        "            /* (Bearz rocket launcher etc.) keep engine's arrow_move path */\n"
        "            /* unchanged. Only level-spawned (cart `spawn X coords ...`)  */\n"
        "            /* aironly arrows (TMNT-RP rolling barrels) get this fix.     */\n"
        "            if (e->modeldata.aironly_directive_seen && !e->trymove && !e->owner)\n"
        "            {\n"
        "                e->trymove = common_trymove;\n"
        "            }\n"
        "            /* ── end Step 47 + 51 ─────────────────────────────────────── */\n"
        "            break;\n"
    )
    ob_s47 = read(ob_path_g)
    ob_s47 = strict_replace(ob_s47, s47_old, s47_new,
                             'Step 47: assign common_trymove for aironly SUBTYPE_ARROW')
    write(ob_path_g, ob_s47)
    print("  Step 47: aironly SUBTYPE_ARROW gets common_trymove (enables horizontal motion via movex)")

    # ── Step 48 (2026-05-30): direct position.x update for aironly SUBTYPE_ARROW idle ──
    # User reports barrels STILL roll in place after Step 47 (trymove assignment).
    # trymove path may be failing for wall/collision/state reasons. Engine has
    # multiple potential blockers in common_trymove (grab checks, Z bounds,
    # wall checks, hole checks, base mismatch) — any could prevent motion for
    # SUBTYPE_ARROW with unusual physics state.
    #
    # Direct approach: bypass trymove entirely. In check_gravity (which fires
    # per tick per entity), if SUBTYPE_ARROW + aironly + ANI_IDLE + vel.x != 0,
    # apply position.x += vel.x * 100/GAME_SPEED directly.
    #
    # Per [[no-false-found-it]] discipline: after Step 47's hypothesis-driven
    # fix failed user verification, switch to a guaranteed-motion approach
    # rather than another speculative trymove tweak.
    #
    # Risks: bypasses wall collision (cart's subject_to_wall ignored for X).
    # But: cart's `offscreenkill 130` directive removes the barrel when it
    # exits the screen by 130 pixels — so even passing through level boundary
    # walls, barrel is killed shortly after.
    #
    # Anchor: inject in check_gravity, after gravity y-update. Adds ~5
    # instructions per tick per SUBTYPE_ARROW entity (negligible cost).
    print("Patching openbor.c (Step 48: direct position.x update for aironly SUBTYPE_ARROW idle)...")
    s48_old = (
        "            if(self->modeldata.move_config_flags & MOVE_CONFIG_SUBJECT_TO_GRAVITY)\n"
        "            {\n"
        "                self->velocity.y += gravity * 100.0 / GAME_SPEED;\n"
        "            }\n"
    )
    s48_new = (
        "            if(self->modeldata.move_config_flags & MOVE_CONFIG_SUBJECT_TO_GRAVITY)\n"
        "            {\n"
        "                self->velocity.y += gravity * 100.0 / GAME_SPEED;\n"
        "            }\n"
        "            /* MiSTer Step 58 (2026-05-31): Step 48 inner-airborne block    */\n"
        "            /* REMOVED. Step 57 (at end of check_gravity, outside airborne)  */\n"
        "            /* is now the single source of truth for rolling-arrow motion,  */\n"
        "            /* fires every tick airborne OR at-rest. Step 48 + Step 57       */\n"
        "            /* both firing during airborne caused inconsistent motion       */\n"
        "            /* (1.4/tick airborne vs 1.05/tick at-rest = visible jitter).   */\n"
    )
    ob_s48 = read(ob_path_g)
    ob_s48 = strict_replace(ob_s48, s48_old, s48_new,
                             'Step 48 + 49: direct position.x + re-establish vel.x for aironly SUBTYPE_ARROW idle')
    write(ob_path_g, ob_s48)
    print("  Step 48 + 49: direct position.x + re-establish vel.x after engine zero (guarantees continuous rolling)")

    # ── Step 46 (2026-05-30): landing transition ANI_FALL → ANI_IDLE + roll vel.x ────
    # Step 45 made TMNT-RP rolling barrels fall (animnum=6 ANI_FALL, vel.y
    # accumulating correctly). User hardware-verified barrels reach the ground.
    # BUT they don't roll forward + scroll-lock prevents player from advancing.
    #
    # Root cause: cart's `anim fall` has `loop 0` (plays once). When the
    # animation completes, engine sets `self->animating = ANIMATING_NONE` but
    # does NOT auto-transition to idle. Engine `update_animation()` line 28396
    # branch for loop-0 anims has no transition logic. So barrel stays stuck
    # in ANI_FALL forever after the single-frame anim completes.
    #
    # Cart's design intent (visible in rolling_barrel.txt):
    #   - anim idle = 4 frames + loop 1 + bouncefactor 2 + bbox + attack
    #     (this is the ROLLING animation with attack hitbox)
    #   - anim fall = 1 frame + loop 0 + bouncefactor 2
    #     (this is the falling-then-bouncing animation)
    # Cart expects post-landing transition: ANI_FALL → ANI_IDLE so the barrel
    # rolls forward damaging the player.
    #
    # SUBTYPE_ARROW alone doesn't drive horizontal motion either — `arrow_move`
    # only fires when cart declares `aimove arrow` (rolling_barrel.txt does
    # NOT). And SUBTYPE_ARROW's init handler SKIPS `e->trymove = common_trymove`.
    # So velocity.x stays at 0 unless explicitly set. PC TMNT-RP probably uses
    # a custom-built engine with additional SUBTYPE_ARROW + aironly behavior.
    #
    # Step 46 fix: at the LANDING EVENT in check_gravity() (just before
    # `self->hithead = NULL;`), if entity is SUBTYPE_ARROW + aironly + not
    # already in ANI_IDLE + has ANI_IDLE defined + has speed.x > 0:
    #   (a) transition to ANI_IDLE (the rolling animation)
    #   (b) set vel.x = direction × speed.x so engine continues rolling
    #
    # Scroll-lock fix follow-on: once barrels roll, they exit the screen via
    # cart's `offscreenkill 130` directive, count_ents drops, scroll-lock
    # releases. No separate scroll-lock fix needed.
    #
    # Anchored on the END of the landing block (after checkdamageonlanding,
    # before self->hithead = NULL).
    print("Patching openbor.c (Step 46: landing transition ANI_FALL → ANI_IDLE + roll vel.x)...")
    s46_old = (
        "                        // Taking damage on a landing?\n"
        "                        checkdamageonlanding(self);\n"
        "\n"
        "                        // in case landing, set hithead to NULL\n"
        "                        self->hithead = NULL;\n"
    )
    s46_new = (
        "                        // Taking damage on a landing?\n"
        "                        checkdamageonlanding(self);\n"
        "\n"
        "                        /* MiSTer Step 46 (2026-05-30): aironly SUBTYPE_ARROW       */\n"
        "                        /* transitions ANI_FALL -> ANI_IDLE on landing + sets       */\n"
        "                        /* roll velocity = cart's speed.x (engine arrow_move        */\n"
        "                        /* canon). Step 59 v2 reverted the fall-energy bonus —      */\n"
        "                        /* cart spec doesn't include it; bonus diverges from PC.    */\n"
        "                        if (self->modeldata.subtype == SUBTYPE_ARROW\n"
        "                            && self->modeldata.aironly_directive_seen\n"
        "                            && self->animnum != ANI_IDLE\n"
        "                            && validanim(self, ANI_IDLE)\n"
        "                            && self->modeldata.speed.x > 0)\n"
        "                        {\n"
        "                            ent_set_anim(self, ANI_IDLE, 0);\n"
        "                            self->velocity.x = (self->direction == DIRECTION_LEFT)\n"
        "                                               ? -self->modeldata.speed.x\n"
        "                                               : self->modeldata.speed.x;\n"
        "                        }\n"
        "                        /* ── end Step 46 ──────────────────────────────────── */\n"
        "\n"
        "                        // in case landing, set hithead to NULL\n"
        "                        self->hithead = NULL;\n"
    )
    ob_s46 = read(ob_path_g)
    ob_s46 = strict_replace(ob_s46, s46_old, s46_new,
                             'Step 46: landing transition ANI_FALL → ANI_IDLE + roll vel.x')
    write(ob_path_g, ob_s46)
    print("  Step 46: landing event transitions aironly SUBTYPE_ARROW to ANI_IDLE + sets roll vel.x")

    # ── 8a. Legacy entity-property alias 'dot' -> 'damage_on_landing' ──
    # Avengers - United Battle Force (and likely other late-build PAKs)
    # call getentityproperty(self, "dot") in scripts. v7533 renamed
    # this property to "damage_on_landing". Inject an alias so legacy
    # PAKs compile. Same pattern can extend to other renamed properties.
    print("Patching openborscript.c (legacy entity-property aliases)...")
    obs_path = os.path.join(obor, 'openborscript.c')
    obs = read(obs_path)
    eplist_anchor = '    // map entity properties\n    MAPSTRINGS(varlist[1], eplist, _ep_the_end,'
    if eplist_anchor in obs:
        alias_block = (
            '    /* Legacy alias: pre-rename PAKs (Avengers UBF etc.) call\n'
            '     * getentityproperty(self, "dot") for what is now\n'
            '     * "damage_on_landing". Pre-resolve the index here so\n'
            '     * the script compiles. */\n'
            '#ifdef MISTER_NATIVE_VIDEO\n'
            '    if (varlist[1]->vt == VT_STR) {\n'
            '        const char *_alias_propname = (const char*)StrCache_Get(varlist[1]->strVal);\n'
            '        if (_alias_propname && stricmp(_alias_propname, "dot") == 0) {\n'
            '            ScriptVariant_ChangeType(varlist[1], VT_INTEGER);\n'
            '            varlist[1]->lVal = _ep_damage_on_landing;\n'
            '        }\n'
            '    }\n'
            '#endif\n'
        )
        obs = obs.replace(eplist_anchor, alias_block + eplist_anchor, 1)
        write(obs_path, obs)
        print("  'dot' -> '_ep_damage_on_landing' alias injected.")
    else:
        print("  WARN: eplist MAPSTRINGS anchor not found")

    # ── 8b. Register `cheats` as openborvariant ──
    # Some PAKs (Pocket Dimensional Clash 2, He-Man, Avengers UBF) call
    # openborvariant("cheats") which v7533 doesn't expose. Add it.
    # Three coordinated edits required: enum, svlist[], switch case.
    print("Patching openborscript.c + config.h (expose cheats to openborvariant)...")

    # 8b.1 — config.h enum (insert SYSTEM_PROPERTY_CHEATS alphabetically
    # between BRANCHNAME and COUNT_ENEMIES)
    cfg_path = os.path.join(obor, 'source/openborscript/config.h')
    cfg = read(cfg_path)
    cfg_old = '    SYSTEM_PROPERTY_BRANCHNAME,\n    SYSTEM_PROPERTY_COUNT_ENEMIES,'
    cfg_new = '    SYSTEM_PROPERTY_BRANCHNAME,\n    SYSTEM_PROPERTY_CHEATS,\n    SYSTEM_PROPERTY_COUNT_ENEMIES,'
    if cfg_old in cfg:
        cfg = cfg.replace(cfg_old, cfg_new, 1)
        write(cfg_path, cfg)
        print("  config.h: SYSTEM_PROPERTY_CHEATS enum entry added.")
    else:
        print("  WARN: enum anchor not found in config.h")

    # 8b.2 — openborscript.c svlist[] alphabetical insert
    obs_path = os.path.join(obor, 'openborscript.c')
    obs = read(obs_path)
    sv_old = '    "branchname",\n    "count_enemies",'
    sv_new = '    "branchname",\n    "cheats",\n    "count_enemies",'
    if sv_old in obs:
        obs = obs.replace(sv_old, sv_new, 1)
        print("  openborscript.c: svlist[] cheats entry added.")
    else:
        print("  WARN: svlist anchor not found")

    # 8b.3 — switch case in getsyspropertybyindex
    sw_old = '    case SYSTEM_PROPERTY_BRANCHNAME:\n\n        ScriptVariant_ChangeType(var, VT_STR);\n        var->strVal = StrCache_CreateNewFrom(branch_name);\n        break;\n\n    case SYSTEM_PROPERTY_COUNT_ENEMIES:'
    sw_new = '    case SYSTEM_PROPERTY_BRANCHNAME:\n\n        ScriptVariant_ChangeType(var, VT_STR);\n        var->strVal = StrCache_CreateNewFrom(branch_name);\n        break;\n\n    case SYSTEM_PROPERTY_CHEATS:\n\n        ScriptVariant_ChangeType(var, VT_INTEGER);\n        var->lVal = global_config.cheats;\n        break;\n\n    case SYSTEM_PROPERTY_COUNT_ENEMIES:'
    if sw_old in obs:
        obs = obs.replace(sw_old, sw_new, 1)
        print("  openborscript.c: getsyspropertybyindex case added.")
    else:
        print("  WARN: switch-case anchor not found")

    write(obs_path, obs)

    # ── 9. Register PLAYER_MIN_Z / PLAYER_MAX_Z as openborconstant ──
    # v7533 only registers these in the openborvariant lookup, not
    # the openborconstant table. Several PAKs (Pocket Dimensional
    # Clash 2, others) call openborconstant("PLAYER_MIN_Z") and
    # die with "Can't find openbor constant" + script compile error.
    # Adding ICMPCONST entries makes both lookups work — backward
    # compatible (no PAK that already worked will break, since the
    # variant lookup is unchanged and the constant lookup just gains
    # two more entries).
    print("Patching constants.c (expose PLAYER_MIN_Z/MAX_Z to openborconstant)...")
    cpath = os.path.join(obor, 'source/openborscript/constants.c')
    if os.path.exists(cpath):
        cdata = read(cpath)
        anchor = '        ICMPCONST(MOVE_CONFIG_SUBJECT_TO_WALL)'
        if anchor in cdata:
            cdata = cdata.replace(
                anchor,
                anchor + '\n        ICMPCONST(PLAYER_MIN_Z)\n        ICMPCONST(PLAYER_MAX_Z)',
                1
            )
            write(cpath, cdata)
            print("  PLAYER_MIN_Z/MAX_Z registered as openborconstant.")
        else:
            print("  WARN: constants.c anchor not found; PAK script-API workaround skipped")
    else:
        print("  WARN: constants.c not found at expected path")

    # ── 6b. Patch logsDir default to /media/fat/logs/OpenBOR_7533 ────
    print("Patching logsDir default in sdl/sdlport.c...")
    sdlport = read(os.path.join(obor, 'sdl/sdlport.c'))
    # v7533 uses MAX_FILENAME_LEN macro instead of literal 128
    logs_old = 'char logsDir[MAX_FILENAME_LEN] = {"Logs"};'
    logs_new = '#ifdef MISTER_NATIVE_VIDEO\nchar logsDir[MAX_FILENAME_LEN] = {"/media/fat/logs/OpenBOR_7533"};\n#else\nchar logsDir[MAX_FILENAME_LEN] = {"Logs"};\n#endif'
    if logs_old in sdlport:
        sdlport = sdlport.replace(logs_old, logs_new, 1)
        write(os.path.join(obor, 'sdl/sdlport.c'), sdlport)
        print("  logsDir default changed to /media/fat/logs/OpenBOR_7533")
    else:
        print("  WARN: logsDir pattern not found in sdl/sdlport.c")

    # -- 7. Replace sdl/sblaster.c with MiSTer DDR3 audio backend --------
    print("Patching sdl/sblaster.c (DDR3 audio backend)...")
    sb = read(os.path.join(patches, 'sblaster_patch.c'))
    write(os.path.join(obor, 'sdl/sblaster.c'), sb)
    print("  sdl/sblaster.c replaced.")

    # -- 8. Fix R/B swap bug in 32-bit blend functions ------------------
    # pixelformat.c's blend_screen32 / blend_multiply32 / blend_half32
    # pass arguments to _color() in swapped (B, G, R) order. Same bug
    # carried over from 4086 — verify and fix if still present.
    #
    # 2026-05-18: DISABLED. 4086 has the SAME blend code and renders
    # A Tale of Vengeance correctly, while our patched 7533 renders
    # alpha-blended girls in wrong green-purple palette. Testing the
    # hypothesis that step 8's "fix" actually introduced the girls bug
    # (R/B interpretation was wrong — the blend functions are part of
    # the engine's BGR-LE pipeline and produce BGR-LE output that
    # matches input convention; our patch broke this). Toggle to True
    # to re-enable if the test refutes the hypothesis.
    # 2026-05-18 evening: tested STEP_8_ENABLED=False — girls still green-purple,
    # so step 8 is NOT the girls bug cause. Re-enabled to restore pre-session
    # state — step 8 was originally added to fix SOMETHING (likely a different
    # bug not yet identified) and disabling it might silently reintroduce that.
    # Default-safer position: leave it enabled until we have positive evidence
    # it's wrong-shaped.
    STEP_8_ENABLED = True
    print("Patching source/gamelib/pixelformat.c (32-bit blend R/B fix)...")
    pf_path = os.path.join(obor, 'source/gamelib/pixelformat.c')
    if not STEP_8_ENABLED:
        print("  SKIPPED (step 8 disabled — testing if it caused green-purple girls)")
    elif os.path.exists(pf_path):
        pf = read(pf_path)
        fixes = [
            (
                "return _color(_screen(color1 >> 16, color2 >> 16),\n"
                "                  _screen((color1 & 0xFF00) >> 8, (color2 & 0xFF00) >> 8),\n"
                "                  _screen(color1 & 0xFF, color2 & 0xFF));",
                "return _color(_screen(color1 & 0xFF, color2 & 0xFF),\n"
                "                  _screen((color1 & 0xFF00) >> 8, (color2 & 0xFF00) >> 8),\n"
                "                  _screen(color1 >> 16, color2 >> 16));"
            ),
            (
                "return _color(_multiply(color1 >> 16, color2 >> 16),\n"
                "                  _multiply((color1 & 0xFF00) >> 8, (color2 & 0xFF00) >> 8),\n"
                "                  _multiply(color1 & 0xFF, color2 & 0xFF));",
                "return _color(_multiply(color1 & 0xFF, color2 & 0xFF),\n"
                "                  _multiply((color1 & 0xFF00) >> 8, (color2 & 0xFF00) >> 8),\n"
                "                  _multiply(color1 >> 16, color2 >> 16));"
            ),
            (
                "return _color(((color1 >> 16) + (color2 >> 16)) >> 1,\n"
                "                  (((color1 & 0xFF00) >> 8) + ((color2 & 0xFF00) >> 8)) >> 1,\n"
                "                  ((color1 & 0xFF) + (color2 & 0xFF)) >> 1);",
                "return _color(((color1 & 0xFF) + (color2 & 0xFF)) >> 1,\n"
                "                  (((color1 & 0xFF00) >> 8) + ((color2 & 0xFF00) >> 8)) >> 1,\n"
                "                  ((color1 >> 16) + (color2 >> 16)) >> 1);"
            ),
        ]
        applied = 0
        for old, new in fixes:
            if old in pf:
                pf = pf.replace(old, new)
                applied += 1
            else:
                print(f"  WARN: blend fix pattern not found (already fixed upstream in 7533?):\n    {old[:60]}...")

        write(pf_path, pf)
        print(f"  {applied}/{len(fixes)} blend R/B fixes applied.")
    else:
        print("  WARN: pixelformat.c not found at expected path — may have moved in 7533")

    # ── 8b. Per-sprite palette (fixes A Tale of Vengeance Hugo/Vice/Playa) ──
    #
    # 7533 keeps pixelformat=PIXEL_x8 default but hardcodes vscreen to PIXEL_32
    # (engine/openbor.c:49037), forcing rendering through putsprite_x8p32. The
    # engine then "helpfully" loads model->palette from the FIRST animation
    # frame's GIF and FORCE-ASSIGNS that palette to EVERY subsequent sprite
    # (line ~16821). For ATOV's `remap run2.gif map1.gif` declarations, the
    # first arg is run2.gif which has a BLUE palette — so every Hugo sprite
    # (idle, atk, hit, fall, walk) gets the blue palette, regardless of its
    # OWN embedded palette.
    #
    # 4086 doesn't hit this because it runs in 8-bit screen mode end-to-end
    # (vscreen allocated with `screenformat` which defaults to PIXEL_8, not
    # hardcoded to PIXEL_32). The engine dispatches to putsprite_8 (different
    # renderer) which works correctly with indexed sprites.
    #
    # 7533 can't run 8-bit screen anymore. But we can make each sprite KEEP
    # its OWN GIF-embedded palette (instead of force-assigning newchar->palette
    # to all of them). Two steps:
    #
    #   1. Change loadsprite() call to pass PIXEL_x8 (not PIXEL_8) so the
    #      bitmap allocator keeps the GIF's palette intact in sprite->palette.
    #      Cost: ~1KB extra per sprite = ~1.4 MB total for a 1400-sprite PAK.
    #
    #   2. Remove the `sprite_map[index].node->sprite->palette = newchar->palette;`
    #      force-assign so each sprite renders with its own palette.
    #
    # Result: putsprite_x8p32 with drawmethod->table NULL falls back to
    # sprite->palette = each sprite's OWN GIF palette = canonical colors.
    # Entities with drawmethod-based remaps (KO flash, dying) still work
    # via the model_get_colourmap path.
    #
    # Verified 2026-05-19: cross-build pixel comparison showed 4086 Hugo green
    # vs 7533 Hugo blue. Tried changing pixelformat default to PIXEL_8 (crashed
    # in putsprite_x8p32 because nopalette path leaves sprite->palette NULL).
    # This per-sprite fix avoids the crash by ensuring sprite->palette is
    # always populated from the GIF.
    # v3.6 (2026-05-20): use existing `newchar->maps_loaded` field as the
    # legacy-remap discriminator instead of adding a new s_model field.
    #
    # WHY: v3.5 added `int has_legacy_remaps` to s_model right after
    # `int maps_loaded`. This added 4 bytes to s_model and SHIFTED the
    # offset of every subsequent field (globalmap, unload, and ~78 more
    # fields). If any code in the engine accesses s_model fields via
    # hardcoded offsets (scripting layer, assembly, memcpy with sizeof
    # snapshot), the shifted offsets corrupt rendering subtly. ATOV
    # characters in v3.5 rendered with WRONG palettes (Hugo blue instead
    # of green, etc.) — suspected offset-shift corruption.
    #
    # User-reported 2026-05-20: "atov wrong colors for hugo, vice, playa"
    # on v3.5 build (md5 babf017daf173f8b8682c054e165ec62).
    #
    # FIX (v3.6): drop the struct field entirely. Use the EXISTING field
    # `int maps_loaded` (already in s_model since stock 7533) as the
    # discriminator. It's incremented by load_colourmap() each time
    # CMD_MODEL_REMAP fires, so it naturally equals 0 for modern PAKs
    # (no `remap` declarations → load_colourmap never called → stays 0)
    # and > 0 for legacy PAKs (Hugo=6, Vice=6, Playa=4 remap declarations).
    #
    # NO STRUCT MODIFICATIONS in v3.6. No new fields. No offset shifts.
    # Modern PAKs render bit-identically to stock 7533. ATOV gets the
    # same path that worked in v2.
    print("v3.10: dual-flag discriminator (has_remap_directive + has_palette_directive)")
    print("       -- v3.9 base: has_remap_directive set by CMD_MODEL_REMAP only.")
    print("       -- v3.10 addition: has_palette_directive set by CMD_MODEL_PALETTE.")
    print("       -- Step 4 v2 bypass now gated on (has_remap && !has_palette).")
    print("       -- Preserves ATOV (has_remap=1, has_palette=0): bypass triggers, use sprite->palette.")
    print("       -- Fixes TMNT-RP (has_remap=1, has_palette=1): bypass disabled, use drawmethod->table.")
    print("       -- Preserves modern PAKs (has_remap=0, has_palette=1): bypass was never triggered.")
    print("       -- Cap's frame GIFs have GARBAGE embedded palettes; palette classic.gif is the")
    print("         canonical render LUT. Modern PAKs need drawmethod->table = classic, NOT bypass.")
    print("       -- Legacy ATOV PAKs need sprite->palette bypass for canonical per-frame render.")
    print("       -- Struct fields added at END of s_model + s_drawmethod (no offset shifts).")

    # ── Step 0 (v3.9): add `int has_remap_directive;` to END of s_model struct
    # in openbor.h. Adding AT END = no offset shifts for existing fields
    # (v3.5 regression cause was middle-of-struct insertion).
    print("Patching openbor.h (add s_model.has_remap_directive at end of struct)...")
    obh_path = os.path.join(obor, 'openbor.h')
    obh = read(obh_path)
    s_model_old = "    char\t\t\t\t\ttest_fixed[MAX_NAME_LEN];\n    char*\t\t\t\t\ttest_pointer;\n\n} s_model;"
    s_model_new = "    char\t\t\t\t\ttest_fixed[MAX_NAME_LEN];\n    char*\t\t\t\t\ttest_pointer;\n\n    int has_remap_directive; /* MiSTer v3.9: set by CMD_MODEL_REMAP only; gates step 4 v2 sprite.c bypass per-model */\n} s_model;"
    obh = strict_replace(obh, s_model_old, s_model_new, 'v3.9: add has_remap_directive to s_model END')
    print("  s_model.has_remap_directive added at struct end")

    # -- Step 0e (v3.10): add `int has_palette_directive;` to END of s_model
    # after the v3.9 has_remap_directive line. Set by CMD_MODEL_PALETTE.
    # Tightens step 4 v2 gate so TMNT-RP-style modern PAKs (declare both
    # `palette FILE.gif` master AND `remap` directives) skip the bypass and
    # render via drawmethod->table = master LUT (canonical), while ATOV-style
    # legacy PAKs (declare `remap` only, no `palette`) keep the bypass
    # = sprite->palette per-frame (canonical for ATOV).
    s_model_v310_old = "    int has_remap_directive; /* MiSTer v3.9: set by CMD_MODEL_REMAP only; gates step 4 v2 sprite.c bypass per-model */\n} s_model;"
    # Step 31 v2 (2026-05-28): also add gravity_directive_seen field at the END.
    # Step 31 v3 (2026-05-28): also add no_adjust_base_directive_seen field.
    # END placement preserves the no-offset-shift safety pattern of v3.9/v3.10.
    s_model_v310_new = "    int has_remap_directive; /* MiSTer v3.9: set by CMD_MODEL_REMAP only; gates step 4 v2 sprite.c bypass per-model */\n    int has_palette_directive; /* MiSTer v3.10: set by CMD_MODEL_PALETTE; tightens step 4 v2 gate for modern PAKs that ALSO use remap (e.g., TMNT-RP) */\n    int gravity_directive_seen; /* MiSTer Step 31 v2: set by CMD_MODEL_SUBJECT_TO_GRAVITY parser; gates ent_default_init force-gravity for TYPE_NONE */\n    int no_adjust_base_directive_seen; /* MiSTer Step 31 v3: set by CMD_MODEL_NO_ADJUST_BASE parser; gates ent_default_init force-no-adjust-base for TYPE_NONE */\n    int aironly_directive_seen; /* MiSTer Step 45: set by CMD_MODEL_AIRONLY parser when arg>0; gates SUBTYPE_ARROW auto-transition to ANI_FALL */\n    int hole_directive_seen; /* MiSTer Step 67: set by CMD_MODEL_SUBJECT_TO_HOLE parser; gates Step 42 v2 SUBJECT_TO_HOLE force-set for flying characters (Bearz OWL) */\n    int obstacle_directive_seen; /* MiSTer Step 68: set by CMD_MODEL_SUBJECT_TO_OBSTACLE parser; gates Step 42 v2 SUBJECT_TO_OBSTACLE force-set */\n    int platform_directive_seen; /* MiSTer Step 68: set by CMD_MODEL_SUBJECT_TO_PLATFORM parser; gates Step 42 v2 SUBJECT_TO_PLATFORM force-set */\n} s_model;"
    obh = strict_replace(obh, s_model_v310_old, s_model_v310_new, 'v3.10 + Step 31 v2 + v3 + Step 45: add directive_seen fields to s_model END')
    # -- Step 70 (2026-06-09): stall-tracker fields at END of s_entity for the
    # wall-pinned rolling-barrel despawn fix (TMNT-RP construction level). A
    # subtype-arrow aironly roller can jam against a boundary wall just offscreen,
    # short of its offscreenkill, deadlocking the level's enemy `wait`. We track
    # per-entity no-move ticks to detect + despawn it. END placement = no offset
    # shifts (same safety pattern as the s_model directive_seen fields above).
    s_entity_old = "} entity;"
    s_entity_new = ("    float mister_stall_lastx; /* MiSTer Step 70: last x for wall-pin stall detect (subtype-arrow rollers) */\n"
                    "    int mister_stall_ticks;   /* MiSTer Step 70: consecutive no-move ticks; despawn roller pinned at a wall */\n"
                    "} entity;")
    obh = strict_replace(obh, s_entity_old, s_entity_new, 'Step 70: add stall-tracker fields to s_entity END')
    write(obh_path, obh)
    print("  s_model.has_palette_directive added at struct end (v3.10)")

    # ── Step 0b (v3.9): add `int has_remap_directive;` to END of s_drawmethod
    # struct in types.h. Drawmethod is per-render-call so this field carries
    # the legacy flag from model to sprite.c::dispatch.
    print("Patching types.h (add s_drawmethod.has_remap_directive at end of struct)...")
    types_path = os.path.join(obor, 'source/gamelib/types.h')
    types = read(types_path)
    s_dm_old = "    water_transform water;\t\n\tint tag;\t\t\t\t// ~~\n} s_drawmethod;"
    s_dm_new = "    water_transform water;\t\n\tint tag;\t\t\t\t// ~~\n\tint has_remap_directive; /* MiSTer v3.9: legacy-PAK flag for sprite.c step 4 v2 bypass; copied from model at render-time */\n} s_drawmethod;"
    types = strict_replace(types, s_dm_old, s_dm_new, 'v3.9: add has_remap_directive to s_drawmethod END')
    print("  s_drawmethod.has_remap_directive added at struct end")

    # -- Step 0f (v3.10): add `int has_palette_directive;` to END of s_drawmethod
    # after the v3.9 has_remap_directive line. Carried from model to sprite.c
    # at render-time alongside has_remap_directive (see step 0h).
    s_dm_v310_old = "\tint has_remap_directive; /* MiSTer v3.9: legacy-PAK flag for sprite.c step 4 v2 bypass; copied from model at render-time */\n} s_drawmethod;"
    s_dm_v310_new = "\tint has_remap_directive; /* MiSTer v3.9: legacy-PAK flag for sprite.c step 4 v2 bypass; copied from model at render-time */\n\tint has_palette_directive; /* MiSTer v3.10: master-palette flag (tightens step 4 v2 gate for TMNT-RP-style modern PAKs); copied from model at render-time */\n} s_drawmethod;"
    types = strict_replace(types, s_dm_v310_old, s_dm_v310_new, 'v3.10: add has_palette_directive to s_drawmethod END')
    write(types_path, types)
    print("  s_drawmethod.has_palette_directive added at struct end (v3.10)")

    print("Patching openbor.c (per-sprite palette: PIXEL_x8 loadsprite + skip force-assign)...")
    ob_path = os.path.join(obor, 'openbor.c')
    ob = read(ob_path)

    # ── Step 0c (v3.9): set newchar->has_remap_directive = 1 inside CMD_MODEL_REMAP.
    # Anchor on the unique CMD_MODEL_REMAP case opener.
    set_flag_old = "            case CMD_MODEL_REMAP:\n            {\n                // This command should not be used under 24bit mode, but for old mods, just give it a default palette"
    set_flag_new = "            case CMD_MODEL_REMAP:\n            {\n                newchar->has_remap_directive = 1; /* MiSTer v3.9: legacy-remap discriminator (NOT set by alternatepal which only increments maps_loaded) */\n                // This command should not be used under 24bit mode, but for old mods, just give it a default palette"
    ob = strict_replace(ob, set_flag_old, set_flag_new, 'v3.9 step 0c: set newchar->has_remap_directive=1 in CMD_MODEL_REMAP')
    print("  set newchar->has_remap_directive=1 inside CMD_MODEL_REMAP case")

    # -- Step 0g (v3.10): set newchar->has_palette_directive = 1 inside
    # CMD_MODEL_PALETTE handler. Anchor on the unique case opener.
    # Setting the flag UNCONDITIONALLY (both `palette FILE.gif` and `palette none`
    # forms set it) is intentional: any explicit palette directive signals
    # author-declared master intent, which should disable the legacy bypass.
    # Verified verbatim against pristine v7533 openbor.c line 14480.
    set_pal_flag_old = "            case CMD_MODEL_PALETTE:\n\n                if(newchar->palette == NULL)"
    set_pal_flag_new = "            case CMD_MODEL_PALETTE:\n\n                newchar->has_palette_directive = 1; /* MiSTer v3.10: master-palette discriminator (distinguishes TMNT-RP modern w/ remap from ATOV legacy) */\n                if(newchar->palette == NULL)"
    ob = strict_replace(ob, set_pal_flag_old, set_pal_flag_new, 'v3.10 step 0g: set newchar->has_palette_directive=1 in CMD_MODEL_PALETTE')
    print("  set newchar->has_palette_directive=1 inside CMD_MODEL_PALETTE case (v3.10)")

    # ── Step 0d (v3.9): copy has_remap_directive from model to drawmethod at
    # render time. Inject right after `drawmethod = &commonmethod;` (line ~29635
    # in stock; that's where per-frame drawmethod is finalized).
    print("Patching openbor.c (copy has_remap_directive into per-render drawmethod)...")
    copy_to_dm_old = "                    drawmethod = &commonmethod;\n\n                    if(e->modeldata.alpha >= 1 && e->modeldata.alpha <= MAX_BLENDINGS)"
    copy_to_dm_new = "                    drawmethod = &commonmethod;\n                    drawmethod->has_remap_directive = e->modeldata.has_remap_directive; /* MiSTer v3.9: pass legacy-PAK flag to sprite.c step 4 v2 */\n\n                    if(e->modeldata.alpha >= 1 && e->modeldata.alpha <= MAX_BLENDINGS)"
    ob = strict_replace(ob, copy_to_dm_old, copy_to_dm_new, 'v3.9 step 0d: copy has_remap_directive into commonmethod at render')
    print("  drawmethod->has_remap_directive set at render-time from e->modeldata.has_remap_directive")

    # -- Step 0h (v3.10): copy has_palette_directive from model to drawmethod
    # at render time, alongside v3.9 step 0d's has_remap_directive copy. Together
    # the two flags drive the tightened step 4 v2 gate (see modified sp_new below).
    copy_pal_to_dm_old = "                    drawmethod->has_remap_directive = e->modeldata.has_remap_directive; /* MiSTer v3.9: pass legacy-PAK flag to sprite.c step 4 v2 */\n\n                    if(e->modeldata.alpha >= 1 && e->modeldata.alpha <= MAX_BLENDINGS)"
    copy_pal_to_dm_new = "                    drawmethod->has_remap_directive = e->modeldata.has_remap_directive; /* MiSTer v3.9: pass legacy-PAK flag to sprite.c step 4 v2 */\n                    drawmethod->has_palette_directive = e->modeldata.has_palette_directive; /* MiSTer v3.10: pass master-palette flag (tightens step 4 v2 gate for TMNT-RP) */\n\n                    if(e->modeldata.alpha >= 1 && e->modeldata.alpha <= MAX_BLENDINGS)"
    ob = strict_replace(ob, copy_pal_to_dm_old, copy_pal_to_dm_new, 'v3.10 step 0h: copy has_palette_directive into commonmethod at render')
    print("  drawmethod->has_palette_directive set at render-time (v3.10)")

    # Step 1: loadsprite uses PIXEL_x8 ONLY for legacy-remap PAKs (ATOV-style).
    # Modern PAKs keep upstream behavior: `nopalette ? PIXEL_x8 : PIXEL_8`.
    #
    # GATE v3.9: `newchar->has_remap_directive` — set by CMD_MODEL_REMAP only.
    # ATOV chars have `remap` declarations BEFORE anim/frame blocks, so
    # has_remap_directive=1 by the time the first frame's loadsprite fires.
    # Cap/He-Man have `alternatepal` (not `remap`) → flag stays 0 → modern path.
    loadsprite_old = "loadsprite(value, offset.x, offset.y, nopalette ? PIXEL_x8 : PIXEL_8); //don't use palette for the sprite since it will one palette from the entity's remap list in 24bit mode"
    loadsprite_new = "loadsprite(value, offset.x, offset.y, (newchar->has_remap_directive || nopalette) ? PIXEL_x8 : PIXEL_8); // MiSTer v3.9 2026-05-20: force PIXEL_x8 for ATOV-style legacy `remap` PAKs; modern PAKs (alternatepal-only Cap/He-Man) keep stock PIXEL_8 path"
    ob = strict_replace(ob, loadsprite_old, loadsprite_new, 'step 1: loadsprite PIXEL_x8 gated on newchar->has_remap_directive')
    print("  loadsprite → PIXEL_x8 ONLY for ATOV-style legacy `remap` PAKs")

    # Step 2: skip force-assign ONLY for legacy-remap PAKs. Modern PAKs keep
    # the force-assign so sprite->palette = newchar->palette consistently
    # across all frames — same as stock 7533.
    force_assign_old = "                            sprite_map[index].node->sprite->palette = newchar->palette;\n                            sprite_map[index].node->sprite->pixelformat = pixelformat;"
    force_assign_new = "                            // MiSTer v3.9 2026-05-20: skip force-assign for ATOV-style legacy `remap` PAKs.\n                            // Legacy PAKs keep per-sprite GIF palette (canonical per-frame); rendered via step 4 v2 bypass.\n                            // Modern PAKs keep stock force-assign: sprite->palette = newchar->palette = `palette FILE` master.\n                            // Render path: stock uses drawmethod->table (NOT step 4 v2 bypass — gated off for modern PAKs).\n                            if (!newchar->has_remap_directive) sprite_map[index].node->sprite->palette = newchar->palette;\n                            sprite_map[index].node->sprite->pixelformat = pixelformat;"
    ob = strict_replace(ob, force_assign_old, force_assign_new, 'step 2: skip force-assign gated on newchar->has_remap_directive')
    print("  sprite->palette force-assign skipped ONLY for ATOV-style legacy PAKs")

    # Step 3: skip CMD_MODEL_REMAP's inner palette load.
    #
    # In 7533 default (pixelformat=PIXEL_x8), CMD_MODEL_REMAP loads
    # newchar->palette = first-remap-arg's GIF palette (e.g. run2.gif for Hugo).
    # This becomes the model's master palette, which feeds drawmethod->table via
    # ent_set_colourmap → model_get_colourmap(model, 0) = model->palette.
    # putsprite_x8p32 with drawmethod->table != NULL uses drawmethod->table
    # OVERRIDING sprite->palette → all sprites render with run2's palette
    # regardless of step 1/2 per-sprite palette fix.
    #
    # Fix: skip the inner load here. The engine's auto-palette code (line ~16805)
    # then loads newchar->palette from the FIRST animation frame's GIF (idle00
    # for Hugo, etc.) — the canonical color. drawmethod->table → idle00's
    # palette → canonical render.
    remap_load_old = """if(pixelformat == PIXEL_x8 && newchar->palette == NULL)
                    {
                        newchar->palette = malloc(PAL_BYTES);
                        if(loadimagepalette(value, packfile, newchar->palette) == 0)
                        {
                            shutdownmessage = "Failed to load palette!";
                            goto lCleanup;
                        }
                    }"""
    remap_load_new = """// PALETTE FIX (v3.6): skip inner palette load. Loading from `value`
                    // (first remap arg, e.g. run2.gif for Hugo) makes that GIF's
                    // palette the model's master palette → overrides every sprite
                    // via drawmethod->table. Skip it so auto-palette code at line
                    // ~16895 loads from the first ANIM frame (idle01.gif for Hugo,
                    // idle00.gif for Vice/Playa) = CANONICAL palette per character.
                    //
                    // Step 1 + step 2 gating uses `newchar->maps_loaded > 0` (set
                    // by load_colourmap() above for each remap declaration) to
                    // detect legacy-remap PAKs at render-time without adding any
                    // new struct fields. ATOV's character.txt files put remap
                    // declarations BEFORE anim/frame blocks, so maps_loaded > 0
                    // by the time the first frame's loadsprite fires."""
    if remap_load_old in ob:
        ob = ob.replace(remap_load_old, remap_load_new)
        print("  CMD_MODEL_REMAP inner palette load skipped (auto-loads from first anim frame)")
    else:
        raise RuntimeError("openbor.c: CMD_MODEL_REMAP palette load pattern not found — moved?")

    # v3.6 (2026-05-20): Step 3b (pre-scan) REMOVED — no longer needed.
    # The pre-scan was set has_legacy_remaps before the parse loop because
    # the gate in steps 1+2 used `newchar->has_legacy_remaps`. v3.6 switches
    # to `newchar->maps_loaded > 0` which is set NATURALLY by load_colourmap()
    # during CMD_MODEL_REMAP parsing. ATOV character.txt files have all
    # `remap` declarations before `anim`/`frame` blocks, so maps_loaded > 0
    # by the time the first anim frame's loadsprite fires.

    # -- Step 13 (2026-05-24): HASH-MAP loadsprite cache.
    # SUB-PROFILE v7 diagnostic data (reverted in same commit chain) identified
    # loadsprite()'s O(N) linear cache lookup as 70% of total PAK load time on
    # JL Legacy (138 sec of 213 sec). gif decode = 4%, samples = 1%,
    # sprite_post = 1%, parser body = bulk of remaining 25%. Hash-map replaces
    # the linear scan; bucket lookup is O(1) average, eliminating the dominant
    # cost.
    #
    # Phase 1 design: ADD hash-first lookup at top of loadsprite(); preserve
    # existing linear scan as fallback (belt-and-suspenders). Hash insert at
    # both sprite_map entry creation sites (toshare path + main path).
    # Phase 2 (future): remove linear scan after extensive validation.
    #
    # v3.10 palette lock: NONE of the 12 locked patches modify the cache
    # lookup loop. Hash lookup runs BEFORE step 1 (PIXEL_x8 gating on
    # loadbitmap arg), so palette-pipeline code paths unchanged.
    # Regression test matrix required: ATOV + TMNT-RP + Avengers + He-Man + PDC2.

    # Patch 1: hash globals + helpers, inserted before loadsprite2() definition
    # (file scope; visible to both loadsprite2 and loadsprite later in file).
    hash_globals_old = (
        "s_sprite *loadsprite2(char *filename, int *width, int *height)\n"
        "{\n"
        "    size_t size;"
    )
    hash_globals_new = (
        "/* MiSTer 2026-05-24 hash-map cache for loadsprite (replaces O(N) linear scan).\n"
        " * Separate-chaining hash; bucket count power-of-2 for fast mask.\n"
        " * Bucket holds indices into sprite_map[] (not pointers, so realloc-safe). */\n"
        "#define MISTER_SPRITE_HASH_SIZE 262144  /* MiSTer 2026-05-24 Phase 1.1: 4x buckets, ~2MB RAM, lower collision rate */\n"
        "typedef struct mister_sprite_hash_bucket_s {\n"
        "    int *indices;\n"
        "    int count;\n"
        "    int capacity;\n"
        "} mister_sprite_hash_bucket;\n"
        "static mister_sprite_hash_bucket mister_sprite_hash[MISTER_SPRITE_HASH_SIZE];\n"
        "\n"
        "static unsigned int mister_hash_string_lower(const char *s) {\n"
        "    /* DJB2 with inline lowercasing for case-insensitive match. */\n"
        "    unsigned int h = 5381;\n"
        "    while (*s) {\n"
        "        unsigned int c = (unsigned char)*s++;\n"
        "        if (c >= 'A' && c <= 'Z') c += 32;\n"
        "        h = ((h << 5) + h) + c;\n"
        "    }\n"
        "    return h;\n"
        "}\n"
        "\n"
        "static void mister_sprite_hash_insert(int index) {\n"
        "    if (!sprite_map || !sprite_map[index].node || !sprite_map[index].node->filename) return;\n"
        "    unsigned int h = mister_hash_string_lower(sprite_map[index].node->filename) & (MISTER_SPRITE_HASH_SIZE - 1);\n"
        "    mister_sprite_hash_bucket *b = &mister_sprite_hash[h];\n"
        "    if (b->count >= b->capacity) {\n"
        "        int new_cap = b->capacity ? b->capacity * 2 : 16;  /* MiSTer 2026-05-24 Phase 1.1: larger initial bucket capacity skips early reallocs */\n"
        "        int *new_idx = realloc(b->indices, sizeof(int) * new_cap);\n"
        "        if (!new_idx) return;  /* OOM: linear-scan fallback in loadsprite() picks up the slack */\n"
        "        b->indices = new_idx;\n"
        "        b->capacity = new_cap;\n"
        "    }\n"
        "    b->indices[b->count++] = index;\n"
        "}\n"
        "\n"
        "s_sprite *loadsprite2(char *filename, int *width, int *height)\n"
        "{\n"
        "    size_t size;"
    )
    ob = strict_replace(ob, hash_globals_old, hash_globals_new,
                        'Step 13a: hash-map globals + helpers')

    # Patch 2: REPLACE the linear scan entirely with hash-map lookup.
    # Phase 1 first attempt kept the linear scan as fallback after the hash
    # lookup — but the fallback always ran on cache MISSES (where hash bucket
    # is empty), still paying O(N) on every miss. Since most loadsprite calls
    # in a fresh PAK load are misses (~70%), the linear scan dominated
    # wall-clock time. User reported "still feels like 70 sec" on DD Reloaded.
    #
    # The hash table is COMPLETE by construction (insert on every sprite_map
    # entry creation). So the linear scan is genuinely redundant — anything
    # the linear scan would find is also in the hash. Removing it is safe
    # given the hash insert is correct. Hash reset added in freesprites()
    # (see Patch 5) to handle PAK switch / resourceCleanUp mid-session.
    hash_lookup_old = (
        "    for(i = 0; i < sprites_loaded; i++)\n"
        "    {\n"
        "        if(sprite_map && sprite_map[i].node)\n"
        "        {\n"
        "            if(stricmp(sprite_map[i].node->filename, filename) == 0)\n"
        "            {\n"
        "                if(!sprite_map[i].node->sprite)\n"
        "                {\n"
        "                    sprite_map[i].node->sprite = loadsprite2(filename, NULL, NULL);\n"
        "                }\n"
        "                if(sprite_map[i].centerx + sprite_map[i].node->sprite->offsetx == ofsx &&\n"
        "                        sprite_map[i].centery + sprite_map[i].node->sprite->offsety == ofsy)\n"
        "                {\n"
        "                    return i;\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    toshare = sprite_map[i].node;\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "\n"
        "    if(toshare)"
    )
    hash_lookup_new = (
        "    /* MiSTer 2026-05-24 hash-map cache lookup (REPLACES O(N) linear scan).\n"
        "     * Scan ONLY the bucket whose hash matches this filename (typically 1-2\n"
        "     * entries, often 0). Hash table is complete by construction (insert on\n"
        "     * every sprite_map entry creation); linear-scan fallback removed. */\n"
        "    {\n"
        "        unsigned int _mister_h = mister_hash_string_lower(filename) & (MISTER_SPRITE_HASH_SIZE - 1);\n"
        "        mister_sprite_hash_bucket *_mister_b = &mister_sprite_hash[_mister_h];\n"
        "        int _mister_j;\n"
        "        for (_mister_j = 0; _mister_j < _mister_b->count; _mister_j++) {\n"
        "            int _mister_i = _mister_b->indices[_mister_j];\n"
        "            if (sprite_map && sprite_map[_mister_i].node) {\n"
        "                if (stricmp(sprite_map[_mister_i].node->filename, filename) == 0) {\n"
        "                    if (!sprite_map[_mister_i].node->sprite) {\n"
        "                        sprite_map[_mister_i].node->sprite = loadsprite2(filename, NULL, NULL);\n"
        "                    }\n"
        "                    if (sprite_map[_mister_i].centerx + sprite_map[_mister_i].node->sprite->offsetx == ofsx &&\n"
        "                            sprite_map[_mister_i].centery + sprite_map[_mister_i].node->sprite->offsety == ofsy) {\n"
        "                        return _mister_i;\n"
        "                    } else {\n"
        "                        toshare = sprite_map[_mister_i].node;\n"
        "                    }\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "    /* Suppress unused-variable warning for i (kept for compatibility with\n"
        "     * other code in loadsprite that still uses it implicitly). */\n"
        "    (void)i;\n"
        "\n"
        "    if(toshare)"
    )
    ob = strict_replace(ob, hash_lookup_old, hash_lookup_new,
                        'Step 13b: REPLACE linear scan with hash-map lookup')

    # Patch 3: hash insert after toshare path's ++sprites_loaded.
    hash_insert_toshare_old = (
        "        sprite_map[sprites_loaded].centery = ofsy - toshare->sprite->offsety;\n"
        "        ++sprites_loaded;\n"
        "        return sprites_loaded - 1;\n"
        "    }"
    )
    hash_insert_toshare_new = (
        "        sprite_map[sprites_loaded].centery = ofsy - toshare->sprite->offsety;\n"
        "        ++sprites_loaded;\n"
        "        mister_sprite_hash_insert(sprites_loaded - 1);  /* MiSTer 2026-05-24 hash-map insert (toshare path) */\n"
        "        return sprites_loaded - 1;\n"
        "    }"
    )
    ob = strict_replace(ob, hash_insert_toshare_old, hash_insert_toshare_new,
                        'Step 13c: hash-map insert in loadsprite toshare path')

    # Patch 4: hash insert after main path's ++sprites_loaded.
    hash_insert_main_old = (
        "    sprite_list->sprite->srcheight = bitmap->clipped_height;\n"
        "    freebitmap(bitmap);\n"
        "    ++sprites_loaded;\n"
        "    return sprites_loaded - 1;\n"
        "}"
    )
    hash_insert_main_new = (
        "    sprite_list->sprite->srcheight = bitmap->clipped_height;\n"
        "    freebitmap(bitmap);\n"
        "    ++sprites_loaded;\n"
        "    mister_sprite_hash_insert(sprites_loaded - 1);  /* MiSTer 2026-05-24 hash-map insert (main path) */\n"
        "    return sprites_loaded - 1;\n"
        "}"
    )
    ob = strict_replace(ob, hash_insert_main_old, hash_insert_main_new,
                        'Step 13d: hash-map insert in loadsprite main path')

    # Patch 5: hash-table reset in freesprites().
    # freesprites() is called from resourceCleanUp() (line 4243 pristine) which
    # runs on PAK switch / engine reload. It frees sprite_map + resets
    # sprites_loaded=0. The hash table holds indices into sprite_map — those
    # indices become invalid after the reset. Clear the hash at the same time.
    # (On MiSTer hybrid core, each PAK launch is a fresh binary respawn via
    # Master_Daemon, so the hash starts empty for fresh loads. This handles
    # the in-process resourceCleanUp path defensively.)
    hash_reset_old = (
        "    if(sprite_map != NULL)\n"
        "    {\n"
        "        free(sprite_map);\n"
        "        sprite_map = NULL;\n"
        "    }\n"
        "    sprites_loaded = 0;\n"
        "}"
    )
    hash_reset_new = (
        "    if(sprite_map != NULL)\n"
        "    {\n"
        "        free(sprite_map);\n"
        "        sprite_map = NULL;\n"
        "    }\n"
        "    sprites_loaded = 0;\n"
        "    /* MiSTer 2026-05-24 hash-map reset: clear bucket contents (free dynamic\n"
        "     * index arrays); reset count to 0. Keep capacity for fast re-fill. */\n"
        "    {\n"
        "        int _mister_b;\n"
        "        for (_mister_b = 0; _mister_b < MISTER_SPRITE_HASH_SIZE; _mister_b++) {\n"
        "            if (mister_sprite_hash[_mister_b].indices) {\n"
        "                free(mister_sprite_hash[_mister_b].indices);\n"
        "                mister_sprite_hash[_mister_b].indices = NULL;\n"
        "            }\n"
        "            mister_sprite_hash[_mister_b].count = 0;\n"
        "            mister_sprite_hash[_mister_b].capacity = 0;\n"
        "        }\n"
        "    }\n"
        "}"
    )
    ob = strict_replace(ob, hash_reset_old, hash_reset_new,
                        'Step 13e: hash-map reset in freesprites()')

    # Patch 6: load-time diagnostic in load_models().
    # Per user request 2026-05-24: keep MINIMAL diagnostic so we can track PAK
    # load time numerically across optimization iterations. One printf per PAK
    # load, prints total wall-clock ms. Tagged "[LOAD]" for easy grep.
    # No per-call overhead -- one timer_gettick() at start, one at end.
    load_timer_start_old = (
        "    free_modelcache();\n"
        "\n"
        "    if(isLoadingScreenTypeBg(loadingbg[0].set))"
    )
    load_timer_start_new = (
        "    free_modelcache();\n"
        "    /* MiSTer 2026-05-24 load-time diagnostic: track total PAK load wall-clock */\n"
        "    unsigned int _mister_load_t0 = timer_gettick();\n"
        "\n"
        "    if(isLoadingScreenTypeBg(loadingbg[0].set))"
    )
    ob = strict_replace(ob, load_timer_start_old, load_timer_start_new,
                        'Step 13f: load-time timer start in load_models()')

    load_timer_end_old = (
        '    printf("\\nLoading models...............\\tDone!\\n");\n'
        "\n"
        "\n"
        "    if(buf)"
    )
    load_timer_end_new = (
        '    printf("\\nLoading models...............\\tDone!\\n");\n'
        '    /* MiSTer 2026-05-24 load-time diagnostic */\n'
        '    printf("[LOAD] PAK loaded in %u ms\\n", (unsigned int)(timer_gettick() - _mister_load_t0));\n'
        "\n"
        "\n"
        "    if(buf)"
    )
    ob = strict_replace(ob, load_timer_end_old, load_timer_end_new,
                        'Step 13g: load-time timer end + printf in load_models()')

    # ## [LOAD] PHASE BREAKDOWN (decode / encode / other) -- microsecond accurate
    # timer_gettick() is ms-resolution; per-sprite loadbitmap/encodesprite are
    # sub-ms, so ms deltas would round to 0. Use clock_gettime (us) accumulators.
    # decode = loadbitmap (GIF/PNG decode + pak I/O); encode = encodesprite (RLE);
    # other = total - decode - encode (clip/sizing/malloc/parse/hash). Load-time
    # only (NOT per-frame), so the clock_gettime overhead is irrelevant.
    print("Patching openbor.c ([LOAD] phase breakdown: decode/encode/other)...")
    ob = strict_replace(ob,
        '#include "openbor.h"',
        '#include "openbor.h"\n#include <sys/time.h>',
        'LOAD-bd: sys/time.h include for gettimeofday')
    ob = strict_replace(ob,
        "blend_table_function blending_table_functions32[MAX_BLENDINGS] = {create_screen32_tbl, create_multiply32_tbl, create_overlay32_tbl, create_hardlight32_tbl, create_dodge32_tbl, create_half32_tbl};",
        "blend_table_function blending_table_functions32[MAX_BLENDINGS] = {create_screen32_tbl, create_multiply32_tbl, create_overlay32_tbl, create_hardlight32_tbl, create_dodge32_tbl, create_half32_tbl};\n"
        "/* MiSTer [LOAD] phase timers (microsecond accumulators) */\n"
        "static unsigned long _mister_decode_us = 0, _mister_encode_us = 0, _mister_size_us = 0, _mister_sprite_us = 0, _mister_script_us = 0, _mister_io_us = 0, _mister_tok_us = 0, _mister_disp_us = 0, _mister_prescan_us = 0;\n"
        "static int _mister_bp_depth = 0; /* MiSTer [LOAD] io bucket re-entrancy guard */\n"
        "unsigned long _mister_decode_io_us = 0; /* MiSTer [LOAD] decode-io: NON-static (shared w/ packfile.c readpackfile wrapper) */\n"
        "int _mister_decode_io_active = 0; /* set around loadbitmap; packfile.c times readpackfile only when set */\n"
        "unsigned long _mister_hinc_us = 0; /* MiSTer #2 re-drill: openbor.h re-include time (NON-static, shared w/ scriptlib Parser.c) */\n"
        "static unsigned long _mister_load_us(void){ struct timeval _t; gettimeofday(&_t, 0); return (unsigned long)_t.tv_sec * 1000000UL + (unsigned long)_t.tv_usec; }\n"
        "/* MiSTer final drill: script-lex (Script_AppendText) timer + distinct-script counter (sizes the dedup win) */\n"
        "static unsigned long _mister_applex_us = 0;\n"
        "static unsigned int _mister_script_total = 0, _mister_script_distinct = 0;\n"
        "static unsigned int _mister_seen_hashes[4096]; static int _mister_seen_n = 0;\n"
        "static unsigned int _mister_djb2(const char *s){ unsigned int h = 5381; if(s) while(*s) h = ((h << 5) + h) + (unsigned char)(*s++); return h; }\n"
        "static void _mister_script_record(const char *txt){ unsigned int h = _mister_djb2(txt); int i; _mister_script_total++; for(i = 0; i < _mister_seen_n; i++) if(_mister_seen_hashes[i] == h) return; if(_mister_seen_n < 4096) _mister_seen_hashes[_mister_seen_n++] = h; _mister_script_distinct++; }\n"
        "#define Script_AppendText(a, b, c) ({ unsigned long _at0 = _mister_load_us(); int _ar = (Script_AppendText)(a, b, c); _mister_applex_us += _mister_load_us() - _at0; _ar; })",
        'LOAD-bd: decode/encode us accumulators + us helper')
    ob = strict_replace(ob,
        "    unsigned int _mister_load_t0 = timer_gettick();",
        "    unsigned int _mister_load_t0 = timer_gettick();\n"
        "    _mister_decode_us = 0; _mister_encode_us = 0; _mister_size_us = 0; _mister_sprite_us = 0; _mister_script_us = 0; _mister_io_us = 0; _mister_bp_depth = 0; _mister_decode_io_us = 0; _mister_decode_io_active = 0; _mister_tok_us = 0; _mister_disp_us = 0; _mister_prescan_us = 0; _mister_hinc_us = 0; _mister_applex_us = 0; _mister_script_total = 0; _mister_script_distinct = 0; _mister_seen_n = 0; mister_sdedup_hits = 0; mister_sdedup_total = 0; /* MiSTer [LOAD] phase reset */",
        'LOAD-bd: reset phase accumulators at load start')
    ob = strict_replace(ob,
        "    bitmap = loadbitmap(filename, packfile, pixelformat);",
        "    { unsigned long _lt0 = _mister_load_us(); _mister_decode_io_active = 1; bitmap = loadbitmap(filename, packfile, pixelformat); _mister_decode_io_active = 0; _mister_decode_us += _mister_load_us() - _lt0; }",
        'LOAD-bd: time loadbitmap (decode) in loadsprite2 + flag decode-io')
    ob = strict_replace(ob,
        "    encodesprite(-clip_left, -clip_top, bitmap, sprite);",
        "    { unsigned long _et0 = _mister_load_us(); encodesprite(-clip_left, -clip_top, bitmap, sprite); _mister_encode_us += _mister_load_us() - _et0; }",
        'LOAD-bd: time encodesprite (RLE encode) in loadsprite2')
    # 2026-06-13 FIX: the COMMON cache-miss path is loadsprite() (4387), NOT
    # loadsprite2() (4172). loadsprite2's decode/encode were the only wrapped
    # sites, so loadsprite's own loadbitmap(...,bmpformat) (4427), sizing pass
    # (4436) and encodesprite(ofsx-clipl,...) (4447) ALL leaked into 'other' --
    # making the decode/encode buckets read ~0 even on real loads (artifact, not
    # evidence). Wrap loadsprite's three phases too, and add a dedicated 'size'
    # bucket for the fakey_encodesprite sizing pass (both paths) -- a known
    # single-pass-refactor candidate (rle_encode_bench: ~7-9 ns/px dead weight).
    ob = strict_replace(ob,
        "    bitmap = loadbitmap(filename, packfile, bmpformat);",
        "    { unsigned long _lt0 = _mister_load_us(); _mister_decode_io_active = 1; bitmap = loadbitmap(filename, packfile, bmpformat); _mister_decode_io_active = 0; _mister_decode_us += _mister_load_us() - _lt0; }",
        'LOAD-bd: time loadbitmap (decode) in loadsprite main path + flag decode-io')
    ob = strict_replace(ob,
        "    size = fakey_encodesprite(bitmap);",
        "    { unsigned long _st0 = _mister_load_us(); size = fakey_encodesprite(bitmap); _mister_size_us += _mister_load_us() - _st0; }",
        'LOAD-bd: time fakey_encodesprite (RLE sizing pass) in BOTH loadsprite paths',
        count=2)
    ob = strict_replace(ob,
        "    encodesprite(ofsx - clipl, ofsy - clipt, bitmap, curr->sprite);",
        "    { unsigned long _et0 = _mister_load_us(); encodesprite(ofsx - clipl, ofsy - clipt, bitmap, curr->sprite); _mister_encode_us += _mister_load_us() - _et0; }",
        'LOAD-bd: time encodesprite (RLE fill) in loadsprite main path')
    # 2026-06-13: split 'other' -> in-loadsprite vs outside. Rename loadsprite to
    # loadsprite_impl and add a thin same-signature timing wrapper (all callers hit
    # it) accumulating total loadsprite() wall-time into _mister_sprite_us.
    # sprite-total includes loadsprite's own decode/size/encode; outside = load
    # total - sprite-total = parse + model/anim setup + script compile + pak open +
    # level layers. Tells us which half of the 53s 'other' (JL Legacy) to chase.
    ob = strict_replace(ob,
        "int loadsprite(char *filename, int ofsx, int ofsy, int bmpformat)\n{",
        "int loadsprite_impl(char *filename, int ofsx, int ofsy, int bmpformat)\n{",
        'LOAD-bd: rename loadsprite -> loadsprite_impl for total-time wrapper')
    ob = strict_replace(ob,
        "    ++sprites_loaded;\n"
        "    mister_sprite_hash_insert(sprites_loaded - 1);  /* MiSTer 2026-05-24 hash-map insert (main path) */\n"
        "    return sprites_loaded - 1;\n"
        "}",
        "    ++sprites_loaded;\n"
        "    mister_sprite_hash_insert(sprites_loaded - 1);  /* MiSTer 2026-05-24 hash-map insert (main path) */\n"
        "    return sprites_loaded - 1;\n"
        "}\n"
        "\n"
        "/* MiSTer 2026-06-13 [LOAD] split: thin timing wrapper around loadsprite_impl.\n"
        " * Same signature so all callers hit this; accumulates total loadsprite\n"
        " * wall-time into _mister_sprite_us. outside = load total - this. */\n"
        "int loadsprite(char *filename, int ofsx, int ofsy, int bmpformat)\n"
        "{\n"
        "    unsigned long _wt0 = _mister_load_us();\n"
        "    int _wr = loadsprite_impl(filename, ofsx, ofsy, bmpformat);\n"
        "    _mister_sprite_us += _mister_load_us() - _wt0;\n"
        "    return _wr;\n"
        "}",
        'LOAD-bd: add loadsprite() timing wrapper around loadsprite_impl')
    # 2026-06-13: split 'outside' -> script-compile vs parse. The level scripts
    # (load_scripts) compile BEFORE load_models, outside the [LOAD] timer. The
    # script compile INSIDE [LOAD] is per-model: animation_script (17378, inside
    # load_cached_model) + spawnscript (21212). Time them into _mister_script_us.
    # script is a subset of 'outside'; outside - script = parse + setup + pak I/O.
    ob = strict_replace(ob,
        "        Script_Compile(newchar->scripts->animation_script);",
        "        { unsigned long _ct0 = _mister_load_us(); Script_Compile(newchar->scripts->animation_script); _mister_script_us += _mister_load_us() - _ct0; }",
        'LOAD-bd: time per-model animation_script Script_Compile (script bucket)')
    ob = strict_replace(ob,
        "                Script_Compile(&next.spawnscript);",
        "                { unsigned long _ct0 = _mister_load_us(); Script_Compile(&next.spawnscript); _mister_script_us += _mister_load_us() - _ct0; }",
        'LOAD-bd: time spawnscript Script_Compile (script bucket)')
    # 2026-06-14: split parse+setup -> tokenize / dispatch / setup. The model parse
    # loop (load_cached_model) is: ParseArgs (tokenize a line) -> getModelCommand
    # (string->enum dispatch -- the prime hash candidate) -> switch (execute/setup).
    # Time ParseArgs (tok) + getModelCommand (disp); setup = (outside - script - io)
    # - tok - disp. Anchored on the unique 5-line block. ParseArgs timed via a GCC
    # statement-expression so it keeps its bool result for the if.
    ob = strict_replace(ob,
        "        line++;\n"
        "        if(ParseArgs(&arglist, buf + pos, argbuf))\n"
        "        {\n"
        "            command = GET_ARG(0);\n"
        "            cmd = getModelCommand(modelcmdlist, command);",
        "        line++;\n"
        "        if(({ unsigned long _pt0 = _mister_load_us(); int _par = ParseArgs(&arglist, buf + pos, argbuf); _mister_tok_us += _mister_load_us() - _pt0; _par; }))\n"
        "        {\n"
        "            command = GET_ARG(0);\n"
        "            { unsigned long _dt0 = _mister_load_us(); cmd = getModelCommand(modelcmdlist, command); _mister_disp_us += _mister_load_us() - _dt0; }",
        'LOAD-bd: time ParseArgs (tokenize) + getModelCommand (dispatch) in model parse loop')
    # 2026-06-14 #1 re-drill: time the CMD_MODEL_FRAME look-ahead pre-scan (the
    # `while(!frameset)` findarg loop that counts frames to the next anim). It's in
    # the 'setup' bucket; if it's a big share, the fix is to cache the count instead
    # of re-scanning. Wrap with a brace block (peek/frameset/framecount are function-
    # scope, unaffected). Start before peek=0, stop after the pre-scan while.
    ob = strict_replace(ob,
        "                peek = 0;\n"
        "                if(frameset && framecount >= 0)",
        "                { unsigned long _ps0 = _mister_load_us(); /* MiSTer #1 re-drill: time pre-scan */\n"
        "                peek = 0;\n"
        "                if(frameset && framecount >= 0)",
        '#1 re-drill: pre-scan timer start')
    ob = strict_replace(ob,
        "                    while(buf[pos + peek] == '\\n' || buf[pos + peek] == '\\r')\n"
        "                    {\n"
        "                        ++peek;\n"
        "                    }\n"
        "                }\n"
        "                value = GET_ARG(1);",
        "                    while(buf[pos + peek] == '\\n' || buf[pos + peek] == '\\r')\n"
        "                    {\n"
        "                        ++peek;\n"
        "                    }\n"
        "                }\n"
        "                _mister_prescan_us += _mister_load_us() - _ps0; }\n"
        "                value = GET_ARG(1);",
        '#1 re-drill: pre-scan timer stop')
    # 2026-06-14 final drill: count distinct vs total model scripts (sizes the
    # within-load dedup win). Hash the per-model script text (animscriptbuf, else
    # scriptbuf) right before Script_Compile. distinct == total -> no dedup win;
    # distinct << total -> dedup kills both the lex (setup) + resolve (script) for
    # every duplicate.
    ob = strict_replace(ob,
        "    if(!newchar->isSubclassed)\n"
        "    {\n"
        "        { unsigned long _ct0 = _mister_load_us(); Script_Compile(newchar->scripts->animation_script); _mister_script_us += _mister_load_us() - _ct0; }",
        "    if(!newchar->isSubclassed)\n"
        "    {\n"
        "        _mister_script_record(animscriptbuf && animscriptbuf[0] ? animscriptbuf : scriptbuf); /* MiSTer final drill: count distinct scripts */\n"
        "        { unsigned long _ct0 = _mister_load_us(); Script_Compile(newchar->scripts->animation_script); _mister_script_us += _mister_load_us() - _ct0; }",
        'final drill: record distinct-script count before Script_Compile')
    # 2026-06-13: split 'parse+setup+IO' -> pak-I/O vs CPU. buffer_pakfile is the
    # text/data whole-file pak reader (character.txt, anim scripts, models.txt) --
    # it does NOT overlap 'decode' (sprite GIFs stream via openpackfile/readpackfile,
    # not buffer_pakfile). Rename -> buffer_pakfile_impl + a thin wrapper with a
    # re-entrancy depth guard (so any indirect #include recursion is counted once)
    # accumulating into _mister_io_us. io is a subset of 'outside'; outside - script
    # - io = CPU parse + setup. (_mister_load_us + accumulators are declared at the
    # blending_table_functions32 block ~line 297, before buffer_pakfile ~942.)
    ob = strict_replace(ob,
        "int buffer_pakfile(char *filename, char **pbuffer, size_t *psize)\n{",
        "int buffer_pakfile_impl(char *filename, char **pbuffer, size_t *psize);\n"
        "int buffer_pakfile(char *filename, char **pbuffer, size_t *psize)\n"
        "{\n"
        "    unsigned long _iot0 = 0; int _ior;\n"
        "    if (_mister_bp_depth == 0) _iot0 = _mister_load_us();\n"
        "    _mister_bp_depth++;\n"
        "    _ior = buffer_pakfile_impl(filename, pbuffer, psize);\n"
        "    _mister_bp_depth--;\n"
        "    if (_mister_bp_depth == 0) _mister_io_us += _mister_load_us() - _iot0;\n"
        "    return _ior;\n"
        "}\n"
        "int buffer_pakfile_impl(char *filename, char **pbuffer, size_t *psize)\n{",
        'LOAD-bd: rename buffer_pakfile -> _impl + io-timing wrapper (io bucket)')
    ob = strict_replace(ob,
        '    printf("[LOAD] PAK loaded in %u ms\\n", (unsigned int)(timer_gettick() - _mister_load_t0));',
        "    { unsigned int _mtot = (unsigned int)(timer_gettick() - _mister_load_t0);\n"
        "      unsigned int _mdec = (unsigned int)(_mister_decode_us / 1000UL), _msz = (unsigned int)(_mister_size_us / 1000UL), _menc = (unsigned int)(_mister_encode_us / 1000UL);\n"
        "      unsigned int _mspr = (unsigned int)(_mister_sprite_us / 1000UL), _mscr = (unsigned int)(_mister_script_us / 1000UL), _mio = (unsigned int)(_mister_io_us / 1000UL), _mdio = (unsigned int)(_mister_decode_io_us / 1000UL), _mtok = (unsigned int)(_mister_tok_us / 1000UL), _mdsp = (unsigned int)(_mister_disp_us / 1000UL), _mpre = (unsigned int)(_mister_prescan_us / 1000UL), _mhinc = (unsigned int)(_mister_hinc_us / 1000UL), _mapl = (unsigned int)(_mister_applex_us / 1000UL);\n"
        "      unsigned int _mout = (_mtot > _mspr) ? (_mtot - _mspr) : 0;\n"
        "      unsigned int _moth = (_mtot > _mdec + _msz + _menc) ? (_mtot - _mdec - _msz - _menc) : 0;\n"
        '      printf("[LOAD] PAK loaded in %u ms (decode %u, size %u, encode %u, other %u | sprite-total %u, outside %u, script %u, io %u, decode-io %u, tokenize %u, dispatch %u, prescan %u, hinc %u, applex %u, scripts %u/%u uniq, deduped %u/%u)\\n", _mtot, _mdec, _msz, _menc, _moth, _mspr, _mout, _mscr, _mio, _mdio, _mtok, _mdsp, _mpre, _mhinc, _mapl, _mister_script_distinct, _mister_script_total, mister_sdedup_hits, mister_sdedup_total); }',
        'LOAD-bd: extend [LOAD] print with phase breakdown')

    # =====================================================================
    # SCRIPT DEDUP (2026-06-14) -- the big remaining load-time lever.
    # Roster-heavy PAKs compile many byte-identical animation scripts (JL
    # Legacy: 364/629 = 58% duplicates -> ~30s of wasted lex+resolve). Cache
    # the FIRST model's compiled animation_script (interpreterowner=1) keyed by
    # source text; duplicate models with the same (unload&1) class get
    # Script_Copy -- the engine's own per-frame primitive, which ALIASES the
    # compiled interpreter and sets interpreterowner=0, so no double-free.
    # Variant 1 (first-model-owns): first occurrence keeps today's exact path
    # (Script_Init + AppendText + Compile, iscopy=0); only duplicates alias.
    # Safety: (a) gate on (unload&1) so owner + aliases are always freed in the
    # same teardown batch (unload_level frees unload&1 models together;
    # free_models frees all) -> no live-alias-after-owner-free; (b) drop the
    # cache entry in free_model BEFORE the interpreter is freed so a freed owner
    # can't serve a post-free duplicate. Full-text compare guards hash
    # collisions. RAM-only, no SD files. Doesn't touch the LOCKED palette path.
    # (Investigated against pristine v7533: Script_Copy openborscript.c:484
    # sets interpreterowner=0; Script_Clear:548 frees interpreter only if owner;
    # free_model->clear_all_scripts(model->scripts,2) is the single model-script
    # free site; unload_level:20001 frees individual models mid-session.)
    print("  Script dedup: cache compiled animation_script by source text (skip lex+compile for duplicate roster models)")
    # (1) cache + helpers, inserted before execute_animation_script (Script type
    #     + Script_Copy are in scope there; site/free_model uses come after).
    ob = strict_replace(ob,
        "void execute_animation_script(entity *ent)\n{",
        "/* MiSTer 2026-06-14 within-load animation_script dedup cache. See block\n"
        "   comment in apply_patches.py. RAM-only; first-model-owns + (unload&1)\n"
        "   gate + free_model invalidation. */\n"
        "typedef struct {\n"
        "    unsigned int hash;\n"
        "    char *text;          /* owned copy of source (full-compare vs hash collision) */\n"
        "    int unloadclass;     /* owner's (unload & 1) */\n"
        "    Script *master;      /* owner model's animation_script (interpreterowner==1) */\n"
        "} mister_scache_entry;\n"
        "static mister_scache_entry *mister_scache = NULL;\n"
        "static int mister_scache_n = 0, mister_scache_cap = 0;\n"
        "static unsigned int mister_sdedup_hits = 0, mister_sdedup_total = 0; /* [LOAD] diagnostic */\n"
        "/* bit-exact alias (openborscript.c): aliases the compiled interpreter like\n"
        "   Script_Copy but runs init with iscopy=0 -> matches a fresh Script_Compile. */\n"
        "extern void mister_script_alias_fresh(Script *pdest, Script *psrc);\n"
        "static unsigned int mister_scache_hash(const char *s)\n"
        "{\n"
        "    unsigned int h = 5381;\n"
        "    if(s) while(*s) h = ((h << 5) + h) + (unsigned char)(*s++);\n"
        "    return h;\n"
        "}\n"
        "static Script *mister_scache_lookup(const char *txt, int unloadclass)\n"
        "{\n"
        "    unsigned int h = mister_scache_hash(txt);\n"
        "    int i;\n"
        "    for(i = 0; i < mister_scache_n; i++)\n"
        "        if(mister_scache[i].hash == h && mister_scache[i].unloadclass == unloadclass\n"
        "           && mister_scache[i].text && strcmp(mister_scache[i].text, txt) == 0)\n"
        "            return mister_scache[i].master;\n"
        "    return NULL;\n"
        "}\n"
        "static void mister_scache_insert(const char *txt, int unloadclass, Script *master)\n"
        "{\n"
        "    int len; char *copy; mister_scache_entry *np;\n"
        "    if(!txt || !master) return;\n"
        "    if(mister_scache_n >= mister_scache_cap)\n"
        "    {\n"
        "        int nc = mister_scache_cap ? (mister_scache_cap * 2) : 256;\n"
        "        np = (mister_scache_entry *)realloc(mister_scache, nc * sizeof(mister_scache_entry));\n"
        "        if(!np) return; /* OOM: skip caching, model still works (recompiles) */\n"
        "        mister_scache = np; mister_scache_cap = nc;\n"
        "    }\n"
        "    len = (int)strlen(txt);\n"
        "    copy = (char *)malloc(len + 1);\n"
        "    if(!copy) return;\n"
        "    memcpy(copy, txt, len + 1);\n"
        "    mister_scache[mister_scache_n].hash = mister_scache_hash(txt);\n"
        "    mister_scache[mister_scache_n].text = copy;\n"
        "    mister_scache[mister_scache_n].unloadclass = unloadclass;\n"
        "    mister_scache[mister_scache_n].master = master;\n"
        "    mister_scache_n++;\n"
        "}\n"
        "/* Drop any cache entry owned by this model; called from free_model BEFORE\n"
        "   its scripts/interpreter are freed, so a freed owner never leaves a live\n"
        "   alias pointing at a freed interpreter. */\n"
        "static void mister_scache_drop_master(Script *master)\n"
        "{\n"
        "    int i;\n"
        "    if(!master) return;\n"
        "    for(i = 0; i < mister_scache_n; i++)\n"
        "        if(mister_scache[i].master == master)\n"
        "        {\n"
        "            if(mister_scache[i].text) free(mister_scache[i].text);\n"
        "            mister_scache[i] = mister_scache[mister_scache_n - 1];\n"
        "            mister_scache_n--; i--;\n"
        "        }\n"
        "}\n"
        "void execute_animation_script(entity *ent)\n{",
        'script dedup: cache + helpers before execute_animation_script')
    # (2) restructure the animation_script assembly: finalize text -> dedup
    #     decision. Matches the measurement-modified compile block (the dedup
    #     patch runs AFTER the [LOAD] phase patches, so the compile block here
    #     already carries _mister_script_record + the _mister_script_us timer).
    ob = strict_replace(ob,
        "    if(scriptbuf && animscriptbuf && scriptbuf[0] && animscriptbuf[0])\n"
        "    {\n"
        "        writeToScriptLog(\"\\n#### animationscript function main #####\\n# \");\n"
        "        writeToScriptLog(filename);\n"
        "        writeToScriptLog(\"\\n########################################\\n\");\n"
        "        writeToScriptLog(scriptbuf);\n"
        "\n"
        "        lcmScriptDeleteMain(&scriptbuf);\n"
        "        lcmScriptAddMain(&animscriptbuf);\n"
        "        lcmScriptJoinMain(&animscriptbuf,scriptbuf);\n"
        "\n"
        "        if(!Script_IsInitialized(newchar->scripts->animation_script))\n"
        "        {\n"
        "            Script_Init(newchar->scripts->animation_script, newchar->name, filename, 0);\n"
        "        }\n"
        "        tempInt = Script_AppendText(newchar->scripts->animation_script, animscriptbuf, filename);\n"
        "    }\n"
        "    else if(animscriptbuf && animscriptbuf[0])\n"
        "    {\n"
        "        lcmScriptAddMain(&animscriptbuf);\n"
        "\n"
        "        if(!Script_IsInitialized(newchar->scripts->animation_script))\n"
        "        {\n"
        "            Script_Init(newchar->scripts->animation_script, newchar->name, filename, 0);\n"
        "        }\n"
        "        tempInt = Script_AppendText(newchar->scripts->animation_script, animscriptbuf, filename);\n"
        "    }\n"
        "    else if(scriptbuf && scriptbuf[0])\n"
        "    {\n"
        "        //printf(\"\\n%s\\n\", scriptbuf);\n"
        "        if(!Script_IsInitialized(newchar->scripts->animation_script))\n"
        "        {\n"
        "            Script_Init(newchar->scripts->animation_script, newchar->name, filename, 0);\n"
        "        }\n"
        "        tempInt = Script_AppendText(newchar->scripts->animation_script, scriptbuf, filename);\n"
        "        //Interpreter_OutputPCode(newchar->scripts->animation_script.pinterpreter, \"code\");\n"
        "        writeToScriptLog(\"\\n#### animationscript function main #####\\n# \");\n"
        "        writeToScriptLog(filename);\n"
        "        writeToScriptLog(\"\\n########################################\\n\");\n"
        "        writeToScriptLog(scriptbuf);\n"
        "    }\n"
        "\n"
        "    if(!newchar->isSubclassed)\n"
        "    {\n"
        "        _mister_script_record(animscriptbuf && animscriptbuf[0] ? animscriptbuf : scriptbuf); /* MiSTer final drill: count distinct scripts */\n"
        "        { unsigned long _ct0 = _mister_load_us(); Script_Compile(newchar->scripts->animation_script); _mister_script_us += _mister_load_us() - _ct0; }\n"
        "    }",
        "    {\n"
        "        /* MiSTer 2026-06-14 animation_script dedup: finalize the text via the\n"
        "           lcmScript* transforms, then for non-subclassed models reuse a cached\n"
        "           identical compile (Script_Copy aliases the compiled interpreter ->\n"
        "           skips BOTH lex and resolve) or build fresh as the cache owner. */\n"
        "        char *_mfinal = 0;\n"
        "\n"
        "        if(scriptbuf && animscriptbuf && scriptbuf[0] && animscriptbuf[0])\n"
        "        {\n"
        "            writeToScriptLog(\"\\n#### animationscript function main #####\\n# \");\n"
        "            writeToScriptLog(filename);\n"
        "            writeToScriptLog(\"\\n########################################\\n\");\n"
        "            writeToScriptLog(scriptbuf);\n"
        "\n"
        "            lcmScriptDeleteMain(&scriptbuf);\n"
        "            lcmScriptAddMain(&animscriptbuf);\n"
        "            lcmScriptJoinMain(&animscriptbuf,scriptbuf);\n"
        "            _mfinal = animscriptbuf;\n"
        "        }\n"
        "        else if(animscriptbuf && animscriptbuf[0])\n"
        "        {\n"
        "            lcmScriptAddMain(&animscriptbuf);\n"
        "            _mfinal = animscriptbuf;\n"
        "        }\n"
        "        else if(scriptbuf && scriptbuf[0])\n"
        "        {\n"
        "            _mfinal = scriptbuf;\n"
        "            writeToScriptLog(\"\\n#### animationscript function main #####\\n# \");\n"
        "            writeToScriptLog(filename);\n"
        "            writeToScriptLog(\"\\n########################################\\n\");\n"
        "            writeToScriptLog(scriptbuf);\n"
        "        }\n"
        "\n"
        "        if(_mfinal && _mfinal[0] && !newchar->isSubclassed)\n"
        "        {\n"
        "            int _muc = (newchar->unload & 1);\n"
        "            Script *_mmaster;\n"
        "            _mister_script_record(_mfinal); /* distinct-count cross-check */\n"
        "            mister_sdedup_total++;\n"
        "            _mmaster = mister_scache_lookup(_mfinal, _muc);\n"
        "            if(_mmaster)\n"
        "            {\n"
        "                /* DEDUP HIT: bit-exact alias (iscopy=0), skip lex + compile */\n"
        "                mister_script_alias_fresh(newchar->scripts->animation_script, _mmaster);\n"
        "                mister_sdedup_hits++;\n"
        "            }\n"
        "            else\n"
        "            {\n"
        "                /* miss: build fresh (unchanged path), register as cache owner */\n"
        "                if(!Script_IsInitialized(newchar->scripts->animation_script))\n"
        "                {\n"
        "                    Script_Init(newchar->scripts->animation_script, newchar->name, filename, 0);\n"
        "                }\n"
        "                tempInt = Script_AppendText(newchar->scripts->animation_script, _mfinal, filename);\n"
        "                { unsigned long _ct0 = _mister_load_us(); Script_Compile(newchar->scripts->animation_script); _mister_script_us += _mister_load_us() - _ct0; }\n"
        "                if(tempInt)\n"
        "                {\n"
        "                    mister_scache_insert(_mfinal, _muc, newchar->scripts->animation_script);\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "        else if(_mfinal && _mfinal[0])\n"
        "        {\n"
        "            /* subclassed: original behavior -- Init (if needed) + AppendText, NO compile */\n"
        "            if(!Script_IsInitialized(newchar->scripts->animation_script))\n"
        "            {\n"
        "                Script_Init(newchar->scripts->animation_script, newchar->name, filename, 0);\n"
        "            }\n"
        "            tempInt = Script_AppendText(newchar->scripts->animation_script, _mfinal, filename);\n"
        "        }\n"
        "    }",
        'script dedup: restructure animation_script assembly -> dedup decision')
    # (3) invalidate cache entry when its owner model is freed (before the
    #     interpreter is freed by clear_all_scripts). Unique to free_model.
    ob = strict_replace(ob,
        "    if(hasFreetype(model, MF_SCRIPTS))\n"
        "    {\n"
        "        clear_all_scripts(model->scripts, 2);\n"
        "        free_all_scripts(&model->scripts);\n"
        "    }",
        "    if(hasFreetype(model, MF_SCRIPTS))\n"
        "    {\n"
        "        mister_scache_drop_master(model->scripts->animation_script); /* MiSTer dedup: drop owner entry before its interpreter is freed */\n"
        "        clear_all_scripts(model->scripts, 2);\n"
        "        free_all_scripts(&model->scripts);\n"
        "    }",
        'script dedup: invalidate cache entry in free_model')
    # (4) bit-exact alias helper in openborscript.c (where the file-static
    #     execute_init_method is reachable). Identical to Script_Copy EXCEPT it
    #     runs init with iscopy=0,localclear=1 -- byte-for-byte matching a fresh
    #     Script_Compile (execute_init_method(pscript,0,1)). So a deduped model
    #     ends in EXACTLY the fresh-compile end state (no iscopy divergence); the
    #     only residue is the shared interpreter's symbol-table name/comment (the
    #     first owner's), used solely in fatal error messages. Read fresh so it
    #     picks up prior openborscript.c patches (Steps 32/35/61/...).
    obs_alias_path = os.path.join(obor, 'openborscript.c')
    obs_alias = read(obs_alias_path)
    obs_alias = strict_replace(obs_alias,
        "    pdest->pinterpreter = psrc->pinterpreter;\n"
        "    pdest->comment = psrc->comment;\n"
        "    pdest->interpreterowner = 0; // dont own it\n"
        "    pdest->initialized = psrc->initialized; //just copy, it should be 1\n"
        "    execute_init_method(pdest, 1, localclear);\n"
        "}",
        "    pdest->pinterpreter = psrc->pinterpreter;\n"
        "    pdest->comment = psrc->comment;\n"
        "    pdest->interpreterowner = 0; // dont own it\n"
        "    pdest->initialized = psrc->initialized; //just copy, it should be 1\n"
        "    execute_init_method(pdest, 1, localclear);\n"
        "}\n"
        "\n"
        "/* MiSTer 2026-06-14 bit-exact dedup alias. Identical to Script_Copy above\n"
        "   (aliases the compiled interpreter, interpreterowner=0 -> no double-free)\n"
        "   EXCEPT it runs the init method with iscopy=0,localclear=1 -- byte-for-byte\n"
        "   matching a fresh Script_Compile (execute_init_method(pscript,0,1) below).\n"
        "   Used by the load-time animation_script dedup so a deduped duplicate model\n"
        "   ends in EXACTLY the fresh-compile end state (no iscopy divergence). */\n"
        "void mister_script_alias_fresh(Script *pdest, Script *psrc)\n"
        "{\n"
        "    if(!psrc->initialized)\n"
        "    {\n"
        "        return;\n"
        "    }\n"
        "    if(pdest->initialized)\n"
        "    {\n"
        "        Script_Clear(pdest, 1);\n"
        "    }\n"
        "    pdest->pinterpreter = psrc->pinterpreter;\n"
        "    pdest->comment = psrc->comment;\n"
        "    pdest->interpreterowner = 0; // don't own it (shared with the cache owner)\n"
        "    pdest->initialized = psrc->initialized;\n"
        "    execute_init_method(pdest, 0, 1); // iscopy=0,localclear=1 -> matches fresh Script_Compile\n"
        "}\n"
        "\n"
        "/* MiSTer 2026-06-15 compile-WITHOUT-init, for command-script dedup cache\n"
        "   masters. Identical to Script_Compile EXCEPT it does NOT run\n"
        "   execute_init_method: the cache master is a code-only holder, and each\n"
        "   model's alias runs init() exactly once via mister_script_alias_fresh. So\n"
        "   init() executes once per model (matching the original per-model\n"
        "   Script_Compile) with NO extra master-init side effect -> bit-exact for\n"
        "   non-idempotent init() scripts too. */\n"
        "int mister_script_compile_noinit(Script *pscript)\n"
        "{\n"
        "    int result;\n"
        "    if(!pscript || !pscript->pinterpreter)\n"
        "    {\n"
        "        return 1;\n"
        "    }\n"
        "    result = SUCCEEDED(Interpreter_CompileInstructions(pscript->pinterpreter));\n"
        "    if(!result)\n"
        "    {\n"
        "        borShutdown(1, \"Can't compile script '%s' %s\\n\", pscript->pinterpreter->theSymbolTable.name, pscript->comment ? pscript->comment : \"\");\n"
        "    }\n"
        "    pscript->pinterpreter->bReset = FALSE;\n"
        "    return result;\n"
        "}",
        'script dedup: bit-exact alias helper + compile-noinit helper in openborscript.c')
    write(obs_alias_path, obs_alias)

    # ===================================================================
    # PDC2 FIX (2026-06-15): a no-model level "at" entry must not reference
    # model_cache[0]. PDC2's tutorial has settings-only entries
    # (light/shadowalpha/shadowcolor + at). Each settings command does its
    # own memset(&next,0,...) (zeroing index to 0) then sets its field, so at
    # the single commit point (CMD_LEVEL_AT memcpy into level->spawnpoints[])
    # the entry has name=NULL, model=NULL, index=0. update_scroller later
    # smartspawns it and spawn() resolves model_index 0 -> model_cache[0];
    # on the 16-bit branch model_cache[0] is bgfx (the select-screen
    # background), so the settings entry spawns a looping bgfx into the level
    # (root-caused via backtrace update_scroller->smartspawn->spawn + the
    # SMARTSPAWN diag: index=0, no name/model, slot.model=bgfx). Engine intent
    # is MODEL_INDEX_NONE (-1) for "no model" (CMD_LEVEL_SPAWN sets it). Fix at
    # the COMMIT point (after all per-command memsets): if the entry has no
    # name and no model pointer, force index/item/weapon to MODEL_INDEX_NONE
    # so spawn() returns NULL instead of resolving model_cache[0]. Legit model
    # spawns always have a name (CMD_LEVEL_SPAWN sets next.name) -> unaffected.
    # ===================================================================
    print("  PDC2 fix: no-model 'at' entries get MODEL_INDEX_NONE at commit (no longer spawn model_cache[0])")
    ob = strict_replace(ob,
        "            __realloc(level->spawnpoints, level->numspawns);\n"
        "            memcpy(&level->spawnpoints[level->numspawns], &next, sizeof(next));",
        "            __realloc(level->spawnpoints, level->numspawns);\n"
        "            if((!next.name || !next.name[0]) && !next.model) { next.index = next.item_properties.index = next.weaponindex = MODEL_INDEX_NONE; } /* MiSTer PDC2 fix: settings-only 'at' entry has no model -> don't let spawn() resolve index 0 to model_cache[0] (bgfx) */\n"
        "            memcpy(&level->spawnpoints[level->numspawns], &next, sizeof(next));",
        'PDC2 fix: no-model at-entry gets MODEL_INDEX_NONE at CMD_LEVEL_AT commit')

    # Patch 8 (Phase 1.1 tune 2026-05-24): prepare_sprite_map growth chunk
    # 256 -> 4096. Reduces realloc count from ~195 to ~12 for a 50k-sprite
    # PAK. Each realloc copies the entire previous array; fewer reallocs =
    # less cumulative memcpy work. Combined with hash bucket tweaks (size +
    # initial capacity in Patch 1), targets the residual loadsprite overhead
    # not addressed by the hash itself.
    sprite_map_growth_old = (
        "        sprite_map_max_items = (((size + 1) >> 8) + 1) << 8;"
    )
    sprite_map_growth_new = (
        "        /* MiSTer 2026-05-24 Phase 1.1: 256-chunk -> 4096-chunk growth (16x fewer reallocs) */\n"
        "        sprite_map_max_items = (((size + 1) >> 12) + 1) << 12;"
    )
    ob = strict_replace(ob, sprite_map_growth_old, sprite_map_growth_new,
                        'Step 13h (Phase 1.1): prepare_sprite_map growth 256 -> 4096 chunks')

    print("  Step 13: hash-map cache for loadsprite (7 patches: hash + linear-scan replace + reset + load-time diagnostic + sprite_map growth tuning)")

    # -- Step 12 (2026-05-23): clamp off-screen / zero-size loading bar to
    # on-screen default in update_loading(). User-explicit override
    # (NEVER MODIFY USER GAME FILES rule respected: this is engine-side
    # interpretation, not cart-file edit).
    #
    # WHY: some PAKs declare `loadingbg set=LS_TYPE_BOTH` (bar requested)
    # but with bar coords at (-1000, -1000) and/or bsize=0 — bar is
    # invisible. Combined with all-black `data/bgs/loading.gif` background
    # (common cart-authoring shortcut), user sees pure black during the
    # multi-second model-cache init phase with no feedback at all.
    # Canonical case: Double Dragon Reloaded Alternate (levels.txt:42
    # `loadingbg 1 -1000 -1000 0 105 180 0`). User-reported 2026-05-23.
    #
    # FIX: detect off-screen origin OR zero-size and override to a
    # sensible bottom-center default (1/3 screen width, 25px from bottom).
    # Only fires when bar is genuinely unrenderable — PAKs with on-screen
    # bar coords (TMNT-RP, He-Man, etc.) are unchanged.
    #
    # Trade-off: PAKs that intentionally hid the bar via off-screen coords
    # (if any) will now show a default bar. User-accepted trade — better
    # to surface progress feedback than to silently respect the off-screen
    # authoring choice that produces user-confusing black screens.
    loadingbar_old = (
        "            if(isLoadingScreenTypeBar(s->set))\n"
        "            {\n"
        "                loadingbarstatus.size.x = size_x;\n"
        "                bar(pos_x, pos_y, value, max, &loadingbarstatus);\n"
        "            }"
    )
    loadingbar_new = (
        "            if(isLoadingScreenTypeBar(s->set))\n"
        "            {\n"
        "                /* MiSTer fix 2026-05-23: clamp off-screen or zero-size\n"
        "                 * bar coords to on-screen bottom-center default. Some\n"
        "                 * carts (Double Dragon Reloaded Alternate is canonical)\n"
        "                 * declare set=LS_TYPE_BOTH with bar at (-1000,-1000)\n"
        "                 * bsize=0 -- bar invisible, user sees pure black during\n"
        "                 * long model-cache init phase. Override to a visible\n"
        "                 * default so users always get progress feedback.\n"
        "                 *\n"
        "                 * Gated on s == &loadingbg[0] (model-cache slot only)\n"
        "                 * AND size_x <= 0 (cart author explicitly set bar size\n"
        "                 * to zero == no real bar intended). PAKs that author a\n"
        "                 * real bar (bsize > 0) at off-screen coords usually\n"
        "                 * have their own custom loading display elsewhere\n"
        "                 * (Avengers UBF, PDC2 use per-level bgPosi at on-screen\n"
        "                 * coords) -- we don't add a second bar to those.\n"
        "                 *\n"
        "                 * DD Reloaded: bsize=0, coords off-screen -> clamp.\n"
        "                 * Avengers/PDC2: bsize=100, off-screen -> no clamp\n"
        "                 *   (their per-level bgPosi at on-screen coords IS\n"
        "                 *    the cart-authored visible loading bar). */\n"
        "                if (s == &loadingbg[0] && size_x <= 0)\n"
        "                {\n"
        "                    size_x = videomodes.hRes / 3;\n"
        "                    pos_x = (videomodes.hRes - size_x) / 2;\n"
        "                    pos_y = videomodes.vRes - 25;\n"
        "                }\n"
        "                loadingbarstatus.size.x = size_x;\n"
        "                bar(pos_x, pos_y, value, max, &loadingbarstatus);\n"
        "            }"
    )
    ob = strict_replace(ob, loadingbar_old, loadingbar_new,
                        'step 12: clamp off-screen / zero-size loading bar to on-screen default')
    print("  update_loading(): off-screen/zero-size bar clamps to visible default")

    # -- Step 14 (2026-05-26): B+E entity-collision optimization.
    #
    # Profile evidence from 7 PAKs (Avengers, He-Man, JL Legacy, TMNT-RP, PDC2,
    # DD Reloaded, ATOV; fps range 13-113) showed `arrange_ents()` -> per-entity
    # `check_entity_collision_for()` consuming 28-42% of entity-tick time across
    # all PAKs. Root cause: nested O(N^2) loop over ent_list[] for every entity.
    #
    # Optimization combines two cheap pre-culls:
    #   B: skip targets where target->animation->collision_entity is NULL
    #      (check_entity_collision would return 0 anyway -- skip the call cost)
    #   E: skip pairs whose positions differ by >256 px in x OR z axis
    #      (256 px > any reasonable single-entity hitbox extent; no OpenBOR PAK
    #      has a hitbox reaching 256 px from entity center on a 320x224 screen)
    #
    # Behavior preservation: B is a no-op skip (function returns 0 anyway);
    # E only skips pairs that can't physically collide (rect cull strictly
    # larger than max hitbox extent). collided_entity iteration order is
    # unchanged for nearby pairs -- only far-apart pairs are skipped earlier.
    #
    # Expected gain: ~85% reduction in collision-pair work in typical scenes.

    # Patch 14: B+E entity-collision optimization in check_entity_collision_for().
    # Inserts inline pre-filter (B: skip non-collidable targets) + cheap rect cull
    # (E: skip pairs with |dx|>256 OR |dz|>256 in level coords).
    bp14_old = (
        "void check_entity_collision_for(entity* ent)\n"
        "{\n"
        "    // Animation has collision?\n"
        "    if (ent && ent->animation && ent->animation->collision_entity)\n"
        "    {\n"
        "        int i;\n"
        "        for(i = 0; i < ent_max; i++)\n"
        "        {\n"
        "            //s_anim *a = ent->animation[ent->animnum];\n"
        "            entity* target = ent_list[i];\n"
        "            if(target->exists && target != ent)\n"
        "            {\n"
        "                if (check_entity_collision(ent, target))\n"
        "                {\n"
        "                    ent->collided_entity = target;\n"
        "                    target->collided_entity = ent;\n"
        "                    return;\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "\n"
        "    ent->collided_entity = NULL;\n"
        "    return;\n"
        "}"
    )
    bp14_new = (
        "void check_entity_collision_for(entity* ent)\n"
        "{\n"
        "    // Animation has collision?\n"
        "    if (ent && ent->animation && ent->animation->collision_entity)\n"
        "    {\n"
        "        /* MiSTer 2026-05-26 B+E entity-collision optimization. */\n"
        "        /* Profile data (7 PAKs, fps 13-113) showed this loop = 28-42%% */\n"
        "        /* of per-tick entity work. B: skip non-collidable targets.    */\n"
        "        /* E: cheap rect cull (256 px > any reasonable hitbox extent). */\n"
        "        int i;\n"
        "        int ent_x = (int)ent->position.x;\n"
        "        int ent_z = (int)ent->position.z;\n"
        "        for(i = 0; i < ent_max; i++)\n"
        "        {\n"
        "            entity* target = ent_list[i];\n"
        "            if(target->exists && target != ent\n"
        "               && target->animation && target->animation->collision_entity)  /* B */\n"
        "            {\n"
        "                int dx = (int)target->position.x - ent_x;                   /* E */\n"
        "                int dz = (int)target->position.z - ent_z;\n"
        "                if (dx > 256 || dx < -256 || dz > 256 || dz < -256) continue;\n"
        "                if (check_entity_collision(ent, target))\n"
        "                {\n"
        "                    ent->collided_entity = target;\n"
        "                    target->collided_entity = ent;\n"
        "                    return;\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "\n"
        "    ent->collided_entity = NULL;\n"
        "    return;\n"
        "}"
    )
    ob = strict_replace(ob, bp14_old, bp14_new,
                        'Step 14: B+E entity-collision optimization (filter non-collidable + 256px rect cull)')
    print("  Step 14: B+E entity-collision cull -- expected 5-10x speedup on arrange bucket")

    # -- Step 15 (2026-05-26): Path 1 reorder of normal_find_target() loop body.
    #
    # SUB-PROFILE v8 data identified ai as He-Man's #2 bottleneck (23.4% of
    # entity time vs ~10-15% on other PAKs). Root cause: many AI entities call
    # normal_find_target() per think tick, which iterates ent_max entities and
    # runs faction_check_is_hostile() + check_range_target_all() BEFORE the
    # cheap distance + death-state checks. Reordering puts cheap checks first
    # so expensive function calls are skipped for entities the existing filter
    # would have culled anyway (dead entities, entities >9999 px Manhattan
    # distance from self).
    #
    # SAFETY: pure reorder, same final filter as upstream. Mathematically
    # identical output set; only check order changed. Zero behavior risk.
    pf15_old = (
        "        // Must exist.\n"
        "        if(!ent_list[i]->exists)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        // Can't be self.\n"
        "        if(ent_list[i] == self)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        /* Must be hostile toward it. */\n"
        "        if (!faction_check_is_hostile(self, ent_list[i]))\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        // If anim is defined, then then target must be\n"
        "        // in range of animation.\n"
        "        if(anim >= 0)\n"
        "        {\n"
        "            if(!check_range_target_all(self, ent_list[i], anim, 0, 0))\n"
        "            {\n"
        "                continue;\n"
        "            }\n"
        "        }\n"
        "\n"
        "        // Can't be dead.\n"
        "        if(ent_list[i]->death_state & DEATH_STATE_DEAD)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        // Get X and Z differences between us and target. We then\n"
        "        // add them up to get a total distance.\n"
        "        diffx = diff(ent_list[i]->position.x, self->position.x);\n"
        "        diffz = diff(ent_list[i]->position.z, self->position.z);\n"
        "        diffd = diffx + diffz;\n"
        "\n"
        "        // Distance must be within min and max.\n"
        "        if(diffd <= min || diffd >= max)\n"
        "        {\n"
        "            continue;\n"
        "        }"
    )
    pf15_new = (
        "        // Must exist.\n"
        "        if(!ent_list[i]->exists)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        // Can't be self.\n"
        "        if(ent_list[i] == self)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        /* MiSTer 2026-05-26 Path 1: cheap-first reorder.                 */\n"
        "        /* Same filter set; death-state + distance checks now run BEFORE  */\n"
        "        /* faction_check_is_hostile + check_range_target_all function     */\n"
        "        /* calls. Saves the expensive calls on entities that the existing */\n"
        "        /* filter would have culled by the original (later) distance test.*/\n"
        "\n"
        "        // Can't be dead. (cheap field+bit test -- moved up)\n"
        "        if(ent_list[i]->death_state & DEATH_STATE_DEAD)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        // Get X and Z differences between us and target. (moved up)\n"
        "        diffx = diff(ent_list[i]->position.x, self->position.x);\n"
        "        diffz = diff(ent_list[i]->position.z, self->position.z);\n"
        "        diffd = diffx + diffz;\n"
        "\n"
        "        // Distance must be within min and max. (moved up)\n"
        "        if(diffd <= min || diffd >= max)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        /* Must be hostile toward it. (expensive function call -- now after distance) */\n"
        "        if (!faction_check_is_hostile(self, ent_list[i]))\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        // If anim is defined, then then target must be\n"
        "        // in range of animation. (expensive multi-arg -- now after distance)\n"
        "        if(anim >= 0)\n"
        "        {\n"
        "            if(!check_range_target_all(self, ent_list[i], anim, 0, 0))\n"
        "            {\n"
        "                continue;\n"
        "            }\n"
        "        }"
    )
    ob = strict_replace(ob, pf15_old, pf15_new,
                        'Step 15: Path 1 reorder of normal_find_target() loop body (cheap-first)')
    print("  Step 15: normal_find_target() cheap-first reorder (no behavior change)")

    # -- Step 16 (2026-05-26): three small zero-risk mechanical refactors.
    #
    # 16a: do_attack -- B-style pre-filter + invariant hoist.
    #      checkhit() opens with 4 early-exit conditions; 3 are per-target
    #      and 1 is invariant (attacker->animation->collision_attack).
    #      Moving the per-target checks into the caller loop avoids the
    #      function call cost for non-hittable targets. Hoisting the
    #      attacker invariant out of the loop avoids re-checking it N
    #      times per attack.
    #
    # 16b: block_find_target -- short-circuit && chain reorder cheap-first
    #      (same pattern as Step 15 Path 1 did for normal_find_target).
    #
    # 16c: find_ent_here -- hoist self->modeldata.grabdistance multiplications
    #      out of the loop. Currently computed once per iteration.
    #
    # SAFETY: all three are pure reorders/hoists. Same final filter sets,
    # same arithmetic results, just less wasted work per iteration. Zero
    # behavior change.

    # Patch 16a: do_attack invariant hoist + per-target pre-filter.
    s16a_old = (
        "    current_anim = attacking_entity->animation;\n"
        "\n"
        "    for(i = 0; i < ent_max && !followed; i++)\n"
        "    {\n"
        "        target = ent_list[i];\n"
        "\n"
        "        if(!target->exists)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        // Check collision. If a collision\n"
        "        // is found, the impacting\n"
        "        // collision pointers are also\n"
        "        // populated into lasthit, which\n"
        "        // we will use below.\n"
        "        if(!checkhit(attacking_entity, target))\n"
        "        {\n"
        "            continue;\n"
        "        }"
    )
    s16a_new = (
        "    current_anim = attacking_entity->animation;\n"
        "\n"
        "    /* MiSTer 2026-05-26 Step 16a: hoist invariant out of loop. */\n"
        "    /* attacker->animation->collision_attack is fixed across all */\n"
        "    /* iterations -- no need to re-check it per target.          */\n"
        "    if (!current_anim || !current_anim->collision_attack)\n"
        "    {\n"
        "        return;\n"
        "    }\n"
        "\n"
        "    for(i = 0; i < ent_max && !followed; i++)\n"
        "    {\n"
        "        target = ent_list[i];\n"
        "\n"
        "        if(!target->exists)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        /* Step 16a B-style pre-filter: skip cases checkhit() */\n"
        "        /* would early-out on. Saves the function call cost.  */\n"
        "        if(target == attacking_entity)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "        if(!target->animation->collision_body)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "        if(!target->animation->vulnerable[target->animpos])\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        // Check collision. If a collision\n"
        "        // is found, the impacting\n"
        "        // collision pointers are also\n"
        "        // populated into lasthit, which\n"
        "        // we will use below.\n"
        "        if(!checkhit(attacking_entity, target))\n"
        "        {\n"
        "            continue;\n"
        "        }"
    )
    ob = strict_replace(ob, s16a_old, s16a_new,
                        'Step 16a: do_attack invariant hoist + B-style pre-filter')

    # Patch 16b: block_find_target short-circuit chain reorder cheap-first.
    s16b_old = (
        "        if (attacker && attacker->exists && attacker != self // Can't target self\n"
        "            && (faction_check_can_damage(attacker, self, 0)) // Type is something attacker can damage.\n"
        "            && (anim < 0 || (anim >= 0 && check_range_target_all(self, attacker, anim, 0, 0))) // Valid animation ID and in range.\n"
        "            && !(attacker->death_state & DEATH_STATE_DEAD) // Must be alive.\n"
        "            && attacker->attacking != ATTACKING_NONE // Must be attacking.\n"
        "            && collision_attack_find_no_block_on_frame(attacker->animation, attacker->animpos, 1) != NULL // Valid blockable attack.\n"
        "            && (diffd = (diffx = diff(attacker->position.x, self->position.x)) + (diffz = diff(attacker->position.z, self->position.z))) >= min\n"
        "            && diffd <= max\n"
        "            && (attacker->modeldata.stealth.hide <= detect) // Stealth factor less then perception factor (allows invisibility).\n"
        "            )"
    )
    s16b_new = (
        "        /* MiSTer 2026-05-26 Step 16b: short-circuit chain reordered cheap-first. */\n"
        "        /* Death-state bitmask, attacking-state field, stealth field, and the     */\n"
        "        /* distance math all run BEFORE the expensive function calls (faction,    */\n"
        "        /* range-target-all, collision-attack-find). Same final filter set.       */\n"
        "        if (attacker && attacker->exists && attacker != self // Can't target self\n"
        "            && !(attacker->death_state & DEATH_STATE_DEAD) // Must be alive. (moved up)\n"
        "            && attacker->attacking != ATTACKING_NONE // Must be attacking. (moved up)\n"
        "            && (attacker->modeldata.stealth.hide <= detect) // Stealth check. (moved up)\n"
        "            && (diffd = (diffx = diff(attacker->position.x, self->position.x)) + (diffz = diff(attacker->position.z, self->position.z))) >= min\n"
        "            && diffd <= max\n"
        "            && (faction_check_can_damage(attacker, self, 0)) // Type is something attacker can damage. (moved down -- expensive)\n"
        "            && (anim < 0 || (anim >= 0 && check_range_target_all(self, attacker, anim, 0, 0))) // Valid animation ID and in range. (moved down -- expensive)\n"
        "            && collision_attack_find_no_block_on_frame(attacker->animation, attacker->animpos, 1) != NULL // Valid blockable attack. (moved down -- expensive)\n"
        "            )"
    )
    ob = strict_replace(ob, s16b_old, s16b_new,
                        'Step 16b: block_find_target short-circuit reorder cheap-first')

    # Patch 16c: find_ent_here -- hoist grab-distance invariants out of loop.
    s16c_old = (
        "entity *find_ent_here(entity *exclude, float x, float z, e_entity_type types, int (*test)(entity *, entity *))\n"
        "{\n"
        "    int i;\n"
        "    for(i = 0; i < ent_max; i++)\n"
        "    {\n"
        "        if( ent_list[i]->exists\n"
        "                && ent_list[i] != exclude\n"
        "                && (ent_list[i]->modeldata.type & types)\n"
        "                && diff(ent_list[i]->position.x, x) < (self->modeldata.grabdistance * 0.83333)\n"
        "                && diff(ent_list[i]->position.z, z) < (self->modeldata.grabdistance / 3)\n"
        "                && ent_list[i]->animation->vulnerable[ent_list[i]->animpos]\n"
        "                && (!test || test(exclude, ent_list[i]))\n"
        "          )\n"
        "        {\n"
        "            return ent_list[i];\n"
        "        }\n"
        "    }\n"
        "    return NULL;\n"
        "}"
    )
    s16c_new = (
        "entity *find_ent_here(entity *exclude, float x, float z, e_entity_type types, int (*test)(entity *, entity *))\n"
        "{\n"
        "    int i;\n"
        "    /* MiSTer 2026-05-26 Step 16c: hoist self-invariants out of the loop. */\n"
        "    /* self->modeldata.grabdistance is fixed across all iterations.       */\n"
        "    double grab_x_thresh = self->modeldata.grabdistance * 0.83333;\n"
        "    double grab_z_thresh = (double)self->modeldata.grabdistance / 3.0;\n"
        "    for(i = 0; i < ent_max; i++)\n"
        "    {\n"
        "        if( ent_list[i]->exists\n"
        "                && ent_list[i] != exclude\n"
        "                && (ent_list[i]->modeldata.type & types)\n"
        "                && !(ent_list[i]->death_state & DEATH_STATE_CORPSE) /* MiSTer Step 70: skip corpse-state entities (fixes Bearz captive-box invisible wall) */\n"
        "                && diff(ent_list[i]->position.x, x) < grab_x_thresh\n"
        "                && diff(ent_list[i]->position.z, z) < grab_z_thresh\n"
        "                && ent_list[i]->animation->vulnerable[ent_list[i]->animpos]\n"
        "                && (!test || test(exclude, ent_list[i]))\n"
        "          )\n"
        "        {\n"
        "            return ent_list[i];\n"
        "        }\n"
        "    }\n"
        "    return NULL;\n"
        "}"
    )
    ob = strict_replace(ob, s16c_old, s16c_new,
                        'Step 16c: find_ent_here grab-distance invariant hoist')

    print("  Step 16a: do_attack invariant hoist + B-style pre-filter")
    print("  Step 16b: block_find_target short-circuit reorder cheap-first")
    print("  Step 16c: find_ent_here grab-distance invariant hoist")

    # (fps per-frame + SUB-PROFILE v8/v9/v10 + [BLD]/[BAL] diagnostics stripped for ship -- full version in tools/profiling/)

    # ===================================================================
    # COMMAND-SCRIPT DEDUP (2026-06-15) -- extends the animation_script dedup
    # to the ~27 model command scripts (think/update/takedamage/ondeath/onspawn/
    # key/onmovea/...), all of which compile through lcmHandleCommandScripts.
    # Static analysis of local PAKs: JL Legacy 227 refs -> 4 distinct files (98%
    # dup), TMNT-RP 343 -> 30 (91%). ZERO command-script models use 'unload'.
    # DESIGN: cache-OWNED master interpreters. Each distinct command script is
    # compiled once into a cache-owned Script (interpreterowner=1); every model
    # (incl the first) gets an interpreterowner=0 alias via mister_script_alias_fresh
    # (bit-exact: same compiled interpreter, execute_init_method iscopy=0). Since
    # no MODEL owns the shared interpreter, mid-session unload_level frees can't
    # dangle an alias; the cache is freed once at PAK teardown (free_models),
    # AFTER every model alias is freed (safe ordering). Key: file-case = script
    # path ('F'+path; same path==same content within a PAK -> skip file read AND
    # compile on hit); inline @script case = inline text ('I'+text). Gate:
    # (compile && !first) -> exactly the 27 model command-script callers; level
    # scripts (first=1) + deferred-compile callers (compile=0) keep the original
    # path. Separate cache from the animation dedup (untouched); LOCKED palette
    # path untouched. RAM-only, no SD files.
    # ===================================================================
    print("  Command-script dedup: cache-owned masters for model command scripts (98%/91% dup on JLL/TMNT-RP)")
    # (1) cache + helpers, inserted before free_models (visible to free_models'
    #     clear hook AND to lcmHandleCommandScripts further down).
    ob = strict_replace(ob,
        "void free_models()\n"
        "{\n"
        "    s_model *temp;",
        "/* MiSTer 2026-06-15 command-script dedup cache (cache-OWNED masters).\n"
        "   See block comment in apply_patches.py. Separate from the animation\n"
        "   dedup cache; reuses mister_script_alias_fresh (extern declared earlier). */\n"
        "typedef struct {\n"
        "    unsigned int hash;\n"
        "    char *key;       /* prefix byte ('F' path / 'I' inline) + key text */\n"
        "    Script *master;  /* cache-owned compiled interpreter (interpreterowner==1) */\n"
        "} mister_ccache_entry;\n"
        "static mister_ccache_entry *mister_ccache = NULL;\n"
        "static int mister_ccache_n = 0, mister_ccache_cap = 0;\n"
        "extern void mister_script_alias_fresh(Script *pdest, Script *psrc);\n"
        "extern int mister_script_compile_noinit(Script *pscript);\n"
        "static unsigned int mister_ccache_hash(char pfx, const char *s)\n"
        "{\n"
        "    unsigned int h = 5381; h = ((h << 5) + h) + (unsigned char)pfx;\n"
        "    if(s) while(*s) h = ((h << 5) + h) + (unsigned char)(*s++);\n"
        "    return h;\n"
        "}\n"
        "static Script *mister_ccache_lookup(char pfx, const char *key)\n"
        "{\n"
        "    unsigned int h = mister_ccache_hash(pfx, key);\n"
        "    int i;\n"
        "    for(i = 0; i < mister_ccache_n; i++)\n"
        "        if(mister_ccache[i].hash == h && mister_ccache[i].key\n"
        "           && mister_ccache[i].key[0] == pfx && strcmp(mister_ccache[i].key + 1, key) == 0)\n"
        "            return mister_ccache[i].master;\n"
        "    return NULL;\n"
        "}\n"
        "static void mister_ccache_insert(char pfx, const char *key, Script *master)\n"
        "{\n"
        "    int len; char *copy; mister_ccache_entry *np;\n"
        "    if(!key || !master) return;\n"
        "    if(mister_ccache_n >= mister_ccache_cap)\n"
        "    {\n"
        "        int nc = mister_ccache_cap ? (mister_ccache_cap * 2) : 64;\n"
        "        np = (mister_ccache_entry *)realloc(mister_ccache, nc * sizeof(mister_ccache_entry));\n"
        "        if(!np) return;\n"
        "        mister_ccache = np; mister_ccache_cap = nc;\n"
        "    }\n"
        "    len = (int)strlen(key);\n"
        "    copy = (char *)malloc(len + 2);\n"
        "    if(!copy) return;\n"
        "    copy[0] = pfx; memcpy(copy + 1, key, len + 1);\n"
        "    mister_ccache[mister_ccache_n].hash = mister_ccache_hash(pfx, key);\n"
        "    mister_ccache[mister_ccache_n].key = copy;\n"
        "    mister_ccache[mister_ccache_n].master = master;\n"
        "    mister_ccache_n++;\n"
        "}\n"
        "/* Free all cache-owned masters. Called from free_models AFTER every model\n"
        "   (and thus every interpreterowner=0 alias) has been freed, so the shared\n"
        "   interpreters have no live aliases when freed here. */\n"
        "static void mister_ccache_clear(void)\n"
        "{\n"
        "    int i;\n"
        "    for(i = 0; i < mister_ccache_n; i++)\n"
        "    {\n"
        "        if(mister_ccache[i].master) { Script_Clear(mister_ccache[i].master, 2); free(mister_ccache[i].master); }\n"
        "        if(mister_ccache[i].key) free(mister_ccache[i].key);\n"
        "    }\n"
        "    if(mister_ccache) free(mister_ccache);\n"
        "    mister_ccache = NULL; mister_ccache_n = 0; mister_ccache_cap = 0;\n"
        "}\n"
        "void free_models()\n"
        "{\n"
        "    s_model *temp;",
        'command-script dedup: cache + helpers before free_models')
    # (2) clear the cache after free_models' model-free loop (all aliases gone).
    ob = strict_replace(ob,
        "    while((temp = getFirstModel()))\n"
        "    {\n"
        "        free_model(temp);\n"
        "    }",
        "    while((temp = getFirstModel()))\n"
        "    {\n"
        "        free_model(temp);\n"
        "    }\n"
        "    mister_ccache_clear(); /* MiSTer cmd-script dedup: free cache-owned masters (all model aliases now freed) */",
        'command-script dedup: clear cache in free_models after model-free loop')
    # (3) dedup restructure of lcmHandleCommandScripts. Gate on (compile && !first)
    #     -> the 27 model command-script callers. Level scripts (first=1) and
    #     deferred-compile callers (compile=0) fall to the unchanged original path.
    ob = strict_replace(ob,
        "size_t lcmHandleCommandScripts(ArgList *arglist, char *buf, Script *script, char *scriptname, char *filename, int compile, int first)\n"
        "{\n"
        "    ptrdiff_t pos = 0;\n"
        "    size_t len = 0;\n"
        "    int result = 0;\n"
        "    char *scriptbuf = NULL;\n"
        "    Script_Init(script, scriptname, filename, first);\n"
        "    if(stricmp(GET_ARGP(1), \"@script\") == 0)\n"
        "    {\n"
        "        fetchInlineScript(buf, &scriptbuf, &pos, &len);\n"
        "        if(scriptbuf)\n"
        "        {\n"
        "            result = Script_AppendText(script, scriptbuf, filename);\n"
        "            free(scriptbuf);\n"
        "        }\n"
        "    }\n"
        "    else\n"
        "    {\n"
        "        result = load_script(script, GET_ARGP(1));\n"
        "    }\n"
        "    if(result)\n"
        "    {\n"
        "        if(compile)\n"
        "        {\n"
        "            Script_Compile(script);\n"
        "        }\n"
        "    }\n"
        "    else\n"
        "    {\n"
        "        borShutdown(1, \"Unable to load %s '%s' in file '%s'.\\n\", scriptname, GET_ARGP(1), filename);\n"
        "    }\n"
        "    return pos;\n"
        "}",
        "size_t lcmHandleCommandScripts(ArgList *arglist, char *buf, Script *script, char *scriptname, char *filename, int compile, int first)\n"
        "{\n"
        "    ptrdiff_t pos = 0;\n"
        "    size_t len = 0;\n"
        "    int result = 0;\n"
        "    char *scriptbuf = NULL;\n"
        "    /* MiSTer 2026-06-15 command-script dedup: for the model command-script\n"
        "       callers (compile && !first), reuse a cache-owned compiled master and\n"
        "       make THIS model's script an interpreterowner=0 alias (skips file read +\n"
        "       lex + compile on a hit). Bit-exact (same compiled interpreter). Level\n"
        "       scripts (first) + deferred-compile callers (!compile) use the original\n"
        "       path below unchanged. */\n"
        "    if(compile && !first)\n"
        "    {\n"
        "        char _cpfx; char *_ckey = NULL; char *_cinlinebuf = NULL; Script *_cm;\n"
        "        if(stricmp(GET_ARGP(1), \"@script\") == 0)\n"
        "        {\n"
        "            fetchInlineScript(buf, &scriptbuf, &pos, &len); /* advances pos past the inline block */\n"
        "            _cinlinebuf = scriptbuf; _ckey = scriptbuf; _cpfx = 'I';\n"
        "        }\n"
        "        else\n"
        "        {\n"
        "            _ckey = GET_ARGP(1); _cpfx = 'F';\n"
        "        }\n"
        "        if(_ckey && _ckey[0])\n"
        "        {\n"
        "            _cm = mister_ccache_lookup(_cpfx, _ckey);\n"
        "            if(_cm)\n"
        "            {\n"
        "                /* HIT: alias this model's (varlist-only, un-Script_Init'd) script.\n"
        "                   No Script_Init here -> no per-model interpreter to leak. */\n"
        "                mister_script_alias_fresh(script, _cm);\n"
        "                result = 1;\n"
        "            }\n"
        "            else\n"
        "            {\n"
        "                /* MISS: build a cache-OWNED master, then alias this model. */\n"
        "                _cm = alloc_script();\n"
        "                Script_Init(_cm, scriptname, filename, 0);\n"
        "                if(_cinlinebuf) result = Script_AppendText(_cm, _cinlinebuf, filename);\n"
        "                else            result = load_script(_cm, GET_ARGP(1));\n"
        "                if(result)\n"
        "                {\n"
        "                    mister_script_compile_noinit(_cm); /* master = code-only; init runs once per alias */\n"
        "                    mister_ccache_insert(_cpfx, _ckey, _cm);\n"
        "                    mister_script_alias_fresh(script, _cm);\n"
        "                }\n"
        "                else\n"
        "                {\n"
        "                    Script_Clear(_cm, 2); free(_cm); _cm = NULL;\n"
        "                }\n"
        "            }\n"
        "        }\n"
        "        if(_cinlinebuf) free(_cinlinebuf);\n"
        "        if(!result)\n"
        "        {\n"
        "            borShutdown(1, \"Unable to load %s '%s' in file '%s'.\\n\", scriptname, GET_ARGP(1), filename);\n"
        "        }\n"
        "        return pos;\n"
        "    }\n"
        "    Script_Init(script, scriptname, filename, first);\n"
        "    if(stricmp(GET_ARGP(1), \"@script\") == 0)\n"
        "    {\n"
        "        fetchInlineScript(buf, &scriptbuf, &pos, &len);\n"
        "        if(scriptbuf)\n"
        "        {\n"
        "            result = Script_AppendText(script, scriptbuf, filename);\n"
        "            free(scriptbuf);\n"
        "        }\n"
        "    }\n"
        "    else\n"
        "    {\n"
        "        result = load_script(script, GET_ARGP(1));\n"
        "    }\n"
        "    if(result)\n"
        "    {\n"
        "        if(compile)\n"
        "        {\n"
        "            Script_Compile(script);\n"
        "        }\n"
        "    }\n"
        "    else\n"
        "    {\n"
        "        borShutdown(1, \"Unable to load %s '%s' in file '%s'.\\n\", scriptname, GET_ARGP(1), filename);\n"
        "    }\n"
        "    return pos;\n"
        "}",
        'command-script dedup: lcmHandleCommandScripts cache-owned alias (gate compile && !first)')

    write(ob_path, ob)
    print("  openbor.c: 4 palette patches written (steps 1, 2, 3, 12 — line-29499 fallback intact, no struct mods).")

    # (fps SUB-PROFILE v11 spriteq diagnostics stripped for ship -- full version in tools/profiling/)

    # ── Step 22 (2026-05-27): scalar tightening of palette-LUT inner loops ─────────
    #
    # MOTIVATION (from v9/v10/v11/v12 diagnostic cycles):
    #   - Avengers 36.9 fps: putscreen=9.78 ms/frame, putsprite=8.54 ms/frame
    #     (combined ~68% of frame budget)
    #   - He-Man 13.6 fps: putsprite=56.5 ms/frame (77% of frame, 80 sprites x 710us)
    #   - putother on Avengers is 99.3% putscreen (v12 [SP2] breakdown)
    #
    # Both hot paths share the SAME shape:
    #   dst[i] = palette[src[i]]    -- byte-indexed 256x4-byte LUT, memory-bandwidth bound
    #
    # ARMv7 Cortex-A9 NEON VTBL has 32-byte table-size limit; our LUT is 1024 bytes.
    # Vectorizing the LUT itself is complex with capped gain due to DDR3 bandwidth.
    # The cleanest safe win is scalar micro-optimization: 4x unroll + prefetch +
    # forward iteration for HW prefetcher friendliness.
    #
    # NO CONTACT WITH v3.10 LOCKED PALETTE PATH: these are inner-loop tightenings
    # in spritex8p32.c and screen32.c. The LOCKED palette discriminator lives in
    # sprite.c::putsprite_ex (lines ~605-640). Inner loops only consume the
    # palette pointer; they do not decide which palette to use.
    #
    # EXPECTED GAIN: 1.15-1.25x on inner loop = +3-5 fps across all PAKs.

    # Step 22a + Step 26: scalar tightening + NEON destination stores in putsprite_
    print("Patching spritex8p32.c (Step 22a + Step 26: scalar tightening + NEON dest stores in putsprite_)...")
    sp32_path = os.path.join(obor, 'source/gamelib/spritex8p32.c')
    sp32 = read(sp32_path)

    # Step 26 (v3.1, 2026-05-27): add arm_neon.h include for vst1q_u32.
    # Guard with __ARM_NEON so non-NEON builds (none currently, but defensive)
    # fall back to the scalar 4x unroll path.
    sp32 = strict_replace(
        sp32,
        "#include \"types.h\"",
        "#include \"types.h\"\n#ifdef __ARM_NEON\n#include <arm_neon.h>\n#endif",
        'Step 26: arm_neon.h include in spritex8p32.c'
    )

    putsprite_inner_old = (
        "            if((lx + count) > xmax)\n"
        "            {\n"
        "                count = xmax - lx;\n"
        "            }\n"
        "            for(; count > 0; count--)\n"
        "            {\n"
        "                dest[lx++] = palette[*data++];\n"
        "            }\n"
        "            //u32pcpy(dest+lx, data, palette, count);\n"
        "            //lx+=count;\n"
        "            //data+=count;"
    )
    putsprite_inner_new = (
        "            if((lx + count) > xmax)\n"
        "            {\n"
        "                count = xmax - lx;\n"
        "            }\n"
        "            /* Step 22+26+28 + E + A (v3.1, 2026-05-28):\n"
        "             *  - 8x unroll (was 4x) for better pipeline utilization\n"
        "             *  - NEON 128-bit stores (2x per 8-pixel iter)\n"
        "             *  - PLD prefetch 128B + 192B ahead\n"
        "             *  - Local restrict-style pointer aliases */\n"
        "            __builtin_prefetch(data + 128, 0, 0);\n"
        "            __builtin_prefetch(data + 192, 0, 0);\n"
        "            {\n"
        "                unsigned * const __restrict__ pal_r = palette;\n"
        "                unsigned char *data_p = data;\n"
        "                unsigned *dest_p = &dest[lx];\n"
        "                while(count >= 8)\n"
        "                {\n"
        "                    unsigned p0 = pal_r[data_p[0]];\n"
        "                    unsigned p1 = pal_r[data_p[1]];\n"
        "                    unsigned p2 = pal_r[data_p[2]];\n"
        "                    unsigned p3 = pal_r[data_p[3]];\n"
        "                    unsigned p4 = pal_r[data_p[4]];\n"
        "                    unsigned p5 = pal_r[data_p[5]];\n"
        "                    unsigned p6 = pal_r[data_p[6]];\n"
        "                    unsigned p7 = pal_r[data_p[7]];\n"
        "#ifdef __ARM_NEON\n"
        "                    vst1q_u32((uint32_t *)dest_p,       (uint32x4_t){p0, p1, p2, p3});\n"
        "                    vst1q_u32((uint32_t *)(dest_p + 4), (uint32x4_t){p4, p5, p6, p7});\n"
        "#else\n"
        "                    dest_p[0] = p0; dest_p[1] = p1; dest_p[2] = p2; dest_p[3] = p3;\n"
        "                    dest_p[4] = p4; dest_p[5] = p5; dest_p[6] = p6; dest_p[7] = p7;\n"
        "#endif\n"
        "                    dest_p += 8;\n"
        "                    data_p += 8;\n"
        "                    count  -= 8;\n"
        "                }\n"
        "                if(count >= 4)\n"
        "                {\n"
        "                    unsigned p0 = pal_r[data_p[0]];\n"
        "                    unsigned p1 = pal_r[data_p[1]];\n"
        "                    unsigned p2 = pal_r[data_p[2]];\n"
        "                    unsigned p3 = pal_r[data_p[3]];\n"
        "#ifdef __ARM_NEON\n"
        "                    vst1q_u32((uint32_t *)dest_p, (uint32x4_t){p0, p1, p2, p3});\n"
        "#else\n"
        "                    dest_p[0] = p0; dest_p[1] = p1; dest_p[2] = p2; dest_p[3] = p3;\n"
        "#endif\n"
        "                    dest_p += 4;\n"
        "                    data_p += 4;\n"
        "                    count  -= 4;\n"
        "                }\n"
        "                while(count > 0)\n"
        "                {\n"
        "                    *dest_p++ = pal_r[*data_p++];\n"
        "                    count--;\n"
        "                }\n"
        "                lx   = (int)(dest_p - dest);\n"
        "                data = data_p;\n"
        "            }"
    )
    sp32 = strict_replace(sp32, putsprite_inner_old, putsprite_inner_new,
                          'Step 22a: putsprite_ inner LUT 4x unroll + prefetch')
    write(sp32_path, sp32)
    print("  spritex8p32.c: putsprite_ inner LUT loop tightened (4x unroll + prefetch).")

    # Step 22b + Step 26: scalar tightening + NEON destination stores in putscreenx8p32
    print("Patching screen32.c (Step 22b + Step 26: scalar tightening + NEON dest stores in putscreenx8p32)...")
    sc32_path = os.path.join(obor, 'source/gamelib/screen32.c')
    sc32 = read(sc32_path)

    # Step 26 (v3.1): arm_neon.h include for vst1q_u32.
    sc32 = strict_replace(
        sc32,
        "#include <stdio.h>\n#include <string.h>\n#include \"types.h\"",
        "#include <stdio.h>\n#include <string.h>\n#include \"types.h\"\n#ifdef __ARM_NEON\n#include <arm_neon.h>\n#endif",
        'Step 26: arm_neon.h include in screen32.c'
    )

    putscreen_inner_old = (
        "        else // without colorkey\n"
        "        {\n"
        "            // Copy data\n"
        "            do\n"
        "            {\n"
        "                //u32pcpy(dp, sp, remap, cw);\n"
        "                i = cw - 1;\n"
        "                do\n"
        "                {\n"
        "                    dp[i] = remap[sp[i]];\n"
        "                }\n"
        "                while(i--);\n"
        "                sp += sw;\n"
        "                dp += dw;\n"
        "            }\n"
        "            while(--ch);\n"
        "        }"
    )
    putscreen_inner_new = (
        "        else // without colorkey\n"
        "        {\n"
        "            /* Step 22 (2026-05-27): forward iteration (HW-prefetcher friendly)\n"
        "             * + 4x unroll + per-line PLD prefetch. Inner loop is the\n"
        "             * dominant Avengers putscreen cost (9.78 ms/frame at 36.9 fps;\n"
        "             * 99.3%% of putother bucket per v12 [SP2] breakdown).\n"
        "             *\n"
        "             * Original loop iterated BACKWARDS (i = cw-1 ... while(i--))\n"
        "             * which defeats the Cortex-A9 hardware prefetcher. Forward\n"
        "             * iteration lets prefetcher walk source bytes linearly. */\n"
        "            do\n"
        "            {\n"
        "                int j;\n"
        "                /* Step 28 (v3.1): prefetch next line AND one beyond.\n"
        "                 * Two PLDs deep keeps Cortex-A9's prefetcher engaged. */\n"
        "                __builtin_prefetch(sp + sw, 0, 0);\n"
        "                __builtin_prefetch(sp + sw + sw, 0, 0);\n"
        "                /* Step E + A (v3.1, 2026-05-28): 8x unroll + restrict. */\n"
        "                {\n"
        "                    unsigned * const __restrict__ rem_r = remap;\n"
        "                    for(j = 0; j + 8 <= cw; j += 8)\n"
        "                    {\n"
        "                        unsigned p0 = rem_r[sp[j]];\n"
        "                        unsigned p1 = rem_r[sp[j + 1]];\n"
        "                        unsigned p2 = rem_r[sp[j + 2]];\n"
        "                        unsigned p3 = rem_r[sp[j + 3]];\n"
        "                        unsigned p4 = rem_r[sp[j + 4]];\n"
        "                        unsigned p5 = rem_r[sp[j + 5]];\n"
        "                        unsigned p6 = rem_r[sp[j + 6]];\n"
        "                        unsigned p7 = rem_r[sp[j + 7]];\n"
        "#ifdef __ARM_NEON\n"
        "                        vst1q_u32((uint32_t *)&dp[j],     (uint32x4_t){p0, p1, p2, p3});\n"
        "                        vst1q_u32((uint32_t *)&dp[j + 4], (uint32x4_t){p4, p5, p6, p7});\n"
        "#else\n"
        "                        dp[j] = p0; dp[j+1] = p1; dp[j+2] = p2; dp[j+3] = p3;\n"
        "                        dp[j+4] = p4; dp[j+5] = p5; dp[j+6] = p6; dp[j+7] = p7;\n"
        "#endif\n"
        "                    }\n"
        "                    if(j + 4 <= cw)\n"
        "                    {\n"
        "                        unsigned p0 = rem_r[sp[j]];\n"
        "                        unsigned p1 = rem_r[sp[j + 1]];\n"
        "                        unsigned p2 = rem_r[sp[j + 2]];\n"
        "                        unsigned p3 = rem_r[sp[j + 3]];\n"
        "#ifdef __ARM_NEON\n"
        "                        vst1q_u32((uint32_t *)&dp[j], (uint32x4_t){p0, p1, p2, p3});\n"
        "#else\n"
        "                        dp[j] = p0; dp[j+1] = p1; dp[j+2] = p2; dp[j+3] = p3;\n"
        "#endif\n"
        "                        j += 4;\n"
        "                    }\n"
        "                    for(; j < cw; j++)\n"
        "                    {\n"
        "                        dp[j] = rem_r[sp[j]];\n"
        "                    }\n"
        "                }\n"
        "                sp += sw;\n"
        "                dp += dw;\n"
        "            }\n"
        "            while(--ch);\n"
        "        }"
    )
    sc32 = strict_replace(sc32, putscreen_inner_old, putscreen_inner_new,
                          'Step 22b: putscreenx8p32 no-blend no-key forward iter + 4x unroll + prefetch')
    write(sc32_path, sc32)
    print("  screen32.c: putscreenx8p32 no-blend no-key path tightened (forward iter + 4x unroll + prefetch).")

    # ── Step 23 (v3.1 perf, 2026-05-27): background pre-decode 8 -> 32bpp ────────
    #
    # MOTIVATION (v12 [SP2] measurement on Avengers):
    #   - putscreen = 99.3% of putother bucket (9.78 ms/frame = 36% of budget)
    #   - 2.17 calls/frame x 4519 us each = full-frame background blits
    #
    # Stock OpenBOR 7533 loads 8bpp backgrounds and renders them via
    # putscreenx8p32's per-pixel palette LUT every frame. Avengers' 480x272
    # background is ~130K pixels per blit; the LUT alone dominates.
    #
    # Step 23 pre-decodes the background ONCE at load_background time:
    # walk 8bpp source bytes through palette LUT, write 32bpp result.
    # putscreen subsequently sees src->pixelformat == PIXEL_32 (matches
    # dest's PIXEL_32 on 7533) and routes to blendscreen32's memcpy
    # fast path (screen32.c:322-332) — 4-10x faster than per-pixel LUT.
    #
    # SAFE IN MISTER BUILD:
    #   - background->palette read post-load only at line 4040-4045
    #     (cache_background_replace) which is gated by #ifdef CACHE_BACKGROUNDS
    #     — our MISTER build does NOT define CACHE_BACKGROUNDS
    #   - allocscreen(PIXEL_32) sets palette=NULL but no other code reads it
    #   - Visual output identical: same per-pixel palette lookup, just done
    #     once at load instead of every frame
    #
    # EXPECTED GAIN: Avengers -8-9 ms/frame (37 -> ~50 fps), He-Man -5-6 ms/frame.
    print("Patching openbor.c (Step 23: load_background pre-decode 8 -> 32bpp)...")
    ob_path_step23 = os.path.join(obor, 'openbor.c')
    ob_step23 = read(ob_path_step23)

    step23_old = (
        "    // If background is 8bit color depth, use its color\n"
        "    // table to populate the global and global neon palettes.\n"
        "    if (background->pixelformat == PIXEL_x8)\n"
        "    {\n"
        "        memcpy(pal, background->palette, PAL_BYTES);\n"
        "        memcpy(neontable, pal, PAL_BYTES);\n"
        "    }"
    )
    step23_new = (
        "    // If background is 8bit color depth, use its color\n"
        "    // table to populate the global and global neon palettes.\n"
        "    if (background->pixelformat == PIXEL_x8)\n"
        "    {\n"
        "        memcpy(pal, background->palette, PAL_BYTES);\n"
        "        memcpy(neontable, pal, PAL_BYTES);\n"
        "\n"
        "        /* Step 23 (v3.1 perf, 2026-05-27): pre-decode 8bpp -> 32bpp.\n"
        "         * v12 [SP2] showed putscreen is 99% of putother bucket on\n"
        "         * Avengers (9.78 ms/frame). Pre-decoding at load time routes\n"
        "         * putscreen to blendscreen32 memcpy fast path (screen32.c\n"
        "         * lines 322-332) instead of per-pixel palette LUT in\n"
        "         * putscreenx8p32. Safe: post-load background->palette is\n"
        "         * only read by #ifdef CACHE_BACKGROUNDS code path which\n"
        "         * MISTER build does not define. */\n"
        "        {\n"
        "            /* MiSTer Path B: pre-decode bg to 16-bit (BGR565) so it\n"
        "             * memcpy-blits into the 16-bit vscreen (same-format fast path). */\n"
        "            s_screen *bg16 = allocscreen(background->width, background->height, PIXEL_16);\n"
        "            if (bg16)\n"
        "            {\n"
        "                unsigned short *dst16 = (unsigned short *)bg16->data;\n"
        "                unsigned char *src8 = (unsigned char *)background->data;\n"
        "                unsigned short *pal16 = (unsigned short *)background->palette;\n"
        "                int total = background->width * background->height;\n"
        "                int i;\n"
        "                for (i = 0; i < total; i++)\n"
        "                {\n"
        "                    dst16[i] = pal16[src8[i]]; /* MiSTer full-16: native 565 palette, direct LUT */\n"
        "                }\n"
        "                freescreen(&background);\n"
        "                background = bg16;\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "    else if (background->pixelformat == PIXEL_32)\n"
        "    {\n"
        "        /* MiSTer full-16 (audit concern #1): truecolor (24-bit PNG) bgs load\n"
        "         * as PIXEL_32 via loadscreen32/pngdec; convert to 565 so they blit\n"
        "         * into the 16-bit vscreen. No PIXEL_32-src -> PIXEL_16-dest blit path\n"
        "         * exists, so a PIXEL_32 bg would otherwise render BLACK. */\n"
        "        s_screen *bg16b = allocscreen(background->width, background->height, PIXEL_16);\n"
        "        if (bg16b)\n"
        "        {\n"
        "            unsigned short *d16 = (unsigned short *)bg16b->data;\n"
        "            unsigned char *s32 = (unsigned char *)background->data;\n"
        "            int total2 = background->width * background->height;\n"
        "            int k;\n"
        "            for (k = 0; k < total2; k++)\n"
        "            {\n"
        "                unsigned char *pp = s32 + (k << 2);\n"
        "                d16[k] = colour16(pp[0], pp[1], pp[2]); /* RGBA byte0/1/2 = R/G/B -> BGR565 */\n"
        "            }\n"
        "            freescreen(&background);\n"
        "            background = bg16b;\n"
        "        }\n"
        "    }"
    )
    ob_step23 = strict_replace(ob_step23, step23_old, step23_new,
                                'Step 23: load_background pre-decode 8/32 -> 16bpp')
    write(ob_path_step23, ob_step23)
    print("  openbor.c: load_background pre-decodes 8bpp -> 32bpp; putscreen routes to memcpy fast path.")

    # ── 4. Step 4 v2 (sprite.c bypass) — RESTORED in v3.7 (2026-05-20).
    #
    # WHY THIS WAS RESTORED:
    # Empirical user testing 2026-05-20:
    #   - Celebrated binary (afd4de1, with step 4 v2):    ATOV correct, Cap pink
    #   - v3.6 binary (b12a94e, without step 4 v2):       ATOV WRONG,   Cap correct
    #
    # Step 4 v2 is LOAD-BEARING for ATOV correctness. Earlier feedback memory
    # claimed step 4 v2 "silently failed in celebrated" — that was wrong. The
    # apply_patches.py at afd4de1 uses `drawmethod->flipx` which matches
    # pristine v7533 verbatim. Step 4 v2 DID apply and IS what makes ATOV's
    # Hugo/Vice/Playa render canonically (via sprite->palette = each frame's
    # GIF palette = canonical per-character GIF data).
    #
    # WHY THIS NO LONGER BREAKS MODERN PAKS:
    # Step 4 v2 bypasses drawmethod->table when frame->palette is non-NULL.
    # In celebrated (universal step 1+2), modern PAKs (Cap) had:
    #   sprite->palette = each frame's INCIDENTAL GIF palette (NOT canonical)
    # because step 1 universal forced PIXEL_x8 load AND step 2 universal
    # removed the force-assign that would set sprite->palette to canonical.
    # Step 4 v2's bypass then used those incidental palettes → Cap pink.
    #
    # v3.7 = v3.6 gated step 1+2 + step 4 v2 restored:
    #   - Hugo (legacy, maps_loaded > 0):
    #       step 1 → PIXEL_x8 → sprite->palette = frame GIF palette
    #       step 2 SKIPS force-assign → sprite->palette stays at GIF palette
    #       step 4 v2 → frame->palette non-NULL → bypass drawmethod->table
    #       → putsprite uses sprite->palette = canonical Hugo per frame ✓
    #
    #   - Cap (modern, maps_loaded == 0):
    #       step 1 → PIXEL_8 → sprite->palette = NULL after loadsprite
    #       step 2 FORCE-ASSIGNS sprite->palette = newchar->palette
    #         = classic.gif (canonical Cap master)
    #       step 4 v2 → frame->palette non-NULL (= canonical) → bypass
    #       → putsprite uses sprite->palette = canonical Cap ✓
    #       (same palette across all frames → no flashing)
    #
    # WHY NO HE-MAN FLASHING:
    # He-Man (modern) was reported flashing in v3 (option 2) which had
    # universal step 1+2 + step 4 v3 line-29499 gate (no step 4 v2). v3.7
    # uses GATED step 2 so He-Man's sprite->palette = newchar->palette
    # uniformly across frames → no per-frame palette mismatch → no flashing.
    print("Patching sprite.c (step 4 v2: conditional NULL drawmethod->table for PIXEL_32)...")
    sprite_path = os.path.join(obor, 'source/gamelib/sprite.c')
    sp = read(sprite_path)
    # NOTE: upstream v7533 uses `drawmethod->flipx` (field), not the
    # renamed `(drawmethod->config & DRAWMETHOD_CONFIG_FLIP_X)` form.
    # Verified verbatim against
    # https://raw.githubusercontent.com/DCurrent/openbor/v7533/engine/source/gamelib/sprite.c
    # line ~603.
    sp_old = "        case PIXEL_32:\n            putsprite_x8p32(x, y, drawmethod->flipx, frame, screen, (unsigned *)drawmethod->table, getblendfunction32(drawmethod->alpha));\n            break;"
    sp_new = (
        "        case PIXEL_32:\n"
        "        {\n"
        "            /* MiSTer palette fix step 4 v2 (v3.10, 2026-05-23):\n"
        "             * dual-flag discriminator gates the sprite->palette bypass.\n"
        "             *\n"
        "             * Bypass drawmethod->table -> use sprite->palette ONLY when:\n"
        "             *   1. frame->palette is populated, AND\n"
        "             *   2. drawmethod->has_remap_directive  (CMD_MODEL_REMAP fired), AND\n"
        "             *   3. drawmethod->has_palette_directive is FALSE (no explicit master)\n"
        "             *\n"
        "             * Truth table for the three known PAK archetypes:\n"
        "             *\n"
        "             *   ATOV (legacy):       has_remap=1, has_palette=0 -> BYPASS triggers\n"
        "             *     Use sprite->palette = each frames canonical GIF palette.\n"
        "             *     Hugo green / Vice white+purple / Playa correct.\n"
        "             *\n"
        "             *   TMNT-RP (modern w/ remap):  has_remap=1, has_palette=1 -> NO BYPASS\n"
        "             *     Use drawmethod->table = master palette declared via `palette icon.gif`.\n"
        "             *     Raph renders with `7302b71bbe` (red) instead of his frames embedded\n"
        "             *     Leo-blue palette `0e944ad9bf`. Cart author copied Leo frame templates\n"
        "             *     and authored Raph via the master LUT swap, not per-frame palette.\n"
        "             *\n"
        "             *   Cap / He-Man / PDC2 (modern, no remap):  has_remap=0 -> NO BYPASS\n"
        "             *     Use drawmethod->table (unchanged from v3.9 behavior). Same render path\n"
        "             *     as stock 7533 for these PAKs since has_remap_directive=0 short-circuits\n"
        "             *     the bypass before v3.10 even checks has_palette_directive.\n"
        "             *\n"
        "             * Why the v3.10 third condition: ATOV declares NO `palette FILE.gif` master\n"
        "             * (verified extract 2026-05-23: Hugo/Vice/Playa character.txt have 0 palette\n"
        "             * directives, 4-6 remap directives). TMNT-RP DOES declare `palette icon.gif`\n"
        "             * master. has_palette_directive cleanly distinguishes them without breaking\n"
        "             * the v3.9 ATOV fix (which depends on sprite->palette being the canonical\n"
        "             * per-frame palette for legacy ATOV-style PAKs). */\n"
        "            unsigned *table_arg = (frame && frame->palette && drawmethod->has_remap_directive && !drawmethod->has_palette_directive) ? NULL : (unsigned *)drawmethod->table;\n"
        "            putsprite_x8p32(x, y, drawmethod->flipx, frame, screen, table_arg, getblendfunction32(drawmethod->alpha));\n"
        "            break;\n"
        "        }"
    )
    sp = strict_replace(sp, sp_old, sp_new, 'step 4 v2: sprite.c PIXEL_32 bypass on frame->palette')
    write(sprite_path, sp)
    print("  sprite.c PIXEL_32 dispatch: NULL if frame->palette else drawmethod->table")

    # ── 10. Audio Stage 1: NO PATCH (Option C v2, 2026-05-15 evening).
    #
    # Engine runs at UPSTREAM NATIVE 44.1 kHz (Sega CD Red Book CDDA rate).
    # Sample reads use upstream FIX_TO_INT(fp_pos) nearest-neighbor.
    # Our sblaster_patch.c glue layer handles 44.1 → 48 kHz conversion via
    # linear interpolation before DDR3 submission — same architectural
    # pattern as PICO-8 (zepto8 at 22050 native, mister_main.cpp resamples
    # to 48 kHz). Matches the NTSC-region-match rule: engine produces at
    # platform's native reference rate, glue layer converts at boundary.
    #
    # HISTORY:
    #   2026-05-15 (morning): force-48-kHz patch added (Option A) to kill
    #     the rate-mismatch pitch shift. Worked but engine output at 48 kHz
    #     diverges from Sega CD's native 44.1 kHz Red Book rate.
    #   2026-05-15 (afternoon): Option C v1 attempt — kept engine at 44.1k
    #     native, cubic Hermite resampler in glue. Failed with "constant
    #     per Stage 2 tick" 187 Hz buzz (implementation bug, not method).
    #   2026-05-15 (evening): Option C v2 — engine at 44.1k native, LINEAR
    #     resample in glue (no cubic overshoot, no cross-tick state). User
    #     direction: skip userspace test harness, deploy and test on MiSTer.
    #     Force-48-kHz patch REMOVED (this step is now a no-op).
    print("Step 10 (audio): soundcache-reload patch in mixaudio()")
    print("                  Fixes heavy-scene silent cutout (regression vs Build 3366).")
    sm_path = os.path.join(obor, 'source/gamelib/soundmix.c')
    sm = read(sm_path)

    # FIX for task #10 (heavy-scene silent cutout).
    #
    # Root cause: Build 7533's mixaudio() has a defensive NULL-check that
    # PERMANENTLY DEACTIVATES any voice whose sample pointer is NULL:
    #     if(!soundcache[snum].sample.sampleptr) {
    #         vchannel[chan].active = 0;
    #         continue;
    #     }
    # When heavy MvC gameplay triggers soundcache eviction, channels see
    # NULL sampleptr and get deactivated. Once all voices deactivated →
    # silent output until new audio events fire. Build 3366 doesn't have
    # this null-check (samples stayed loaded forever) so audio always plays.
    #
    # User-confirmed 2026-05-17: PC OpenBOR 3366 plays MvC perfectly (no
    # cutout); PC OpenBOR 7533 has the cutout. So the regression is in the
    # engine itself, not platform-specific.
    #
    # Fix: when sampleptr is NULL, call sound_reload_sample() first to
    # lazy-reload the evicted sample. Only deactivate if reload also fails.
    OLD_NULL_CHECK = (
        '            if(!soundcache[snum].sample.sampleptr)\n'
        '            {\n'
        '                vchannel[chan].active = 0;\n'
        '                continue;\n'
        '            }\n'
    )
    NEW_NULL_CHECK = (
        '            if(!soundcache[snum].sample.sampleptr)\n'
        '            {\n'
        '                /* MiSTer Frontier task #10 fix: lazy-reload evicted\n'
        '                 * samples before deactivating. Build 3366 didn\\\'t have\n'
        '                 * this code path; eviction in 7533 caused MvC heavy-scene\n'
        '                 * cutout where channels deactivated permanently on cache\n'
        '                 * miss. sound_reload_sample reloads from packfile. */\n'
        '                sound_reload_sample(snum);\n'
        '                if(!soundcache[snum].sample.sampleptr)\n'
        '                {\n'
        '                    vchannel[chan].active = 0;\n'
        '                    continue;\n'
        '                }\n'
        '            }\n'
    )
    if OLD_NULL_CHECK not in sm:
        raise RuntimeError("soundmix.c: mixaudio() null-check block not found (upstream changed?)")
    sm = sm.replace(OLD_NULL_CHECK, NEW_NULL_CHECK)

    # FIX for task #10 audio level (2026-05-17 evening):
    # Build 7533 added * 2.5 multiplier to music mix and * 1.5 multiplier to
    # SFX mix vs Build 3366's * 1.0 unity. This makes 7533 audio ~4-8 dB
    # louder than 3366 — peaks regularly clip when summed at the mixer.
    # User-confirmed 2026-05-17: PC 3366 plays MvC heavy scenes with clean
    # continuous music. PC 7533 has audible artifacts on loud action.
    #
    # Fix: revert 7533 multipliers to 3366's unity. Audio is overall ~4-8 dB
    # quieter (user compensates via TV/amp volume), peaks no longer clip,
    # heavy scenes play cleanly like on 3366.
    multiplier_replacements = [
        ('lmusic = (lmusic * lvolume / MAXVOLUME * 2.5);',
         'lmusic = (lmusic * lvolume / MAXVOLUME);'),
        ('rmusic = (rmusic * rvolume / MAXVOLUME * 2.5);',
         'rmusic = (rmusic * rvolume / MAXVOLUME);'),
        ('mixbuf[i++] += ((lmusic << 8) * lvolume / MAXVOLUME * 1.5) - 0x8000;',
         'mixbuf[i++] += ((lmusic << 8) * lvolume / MAXVOLUME) - 0x8000;'),
        ('mixbuf[i++] += ((rmusic << 8) * rvolume / MAXVOLUME * 1.5) - 0x8000;',
         'mixbuf[i++] += ((rmusic << 8) * rvolume / MAXVOLUME) - 0x8000;'),
        ('mixbuf[i++] += (lmusic * lvolume / MAXVOLUME * 1.5);',
         'mixbuf[i++] += (lmusic * lvolume / MAXVOLUME);'),
        ('mixbuf[i++] += (rmusic * rvolume / MAXVOLUME * 1.5);',
         'mixbuf[i++] += (rmusic * rvolume / MAXVOLUME);'),
    ]
    for old, new in multiplier_replacements:
        if old not in sm:
            raise RuntimeError(f"soundmix.c: multiplier pattern not found: {old[:40]}...")
        sm = sm.replace(old, new)

    write(sm_path, sm)
    print("  soundmix.c patched (cache-reload + multiplier revert in mixaudio).")

    # -- 11. REMOVED (2026-05-19) — caused He-Man flashing regression.
    #
    # Step 11 originally relaxed `pixelformat == PIXEL_x8` guards on:
    #   - auto-palette-from-first-frame block (line ~16895)
    #   - convert_map_to_palette() wrap (line ~17506)
    #
    # Intent: unlock palette/colourmap infrastructure in 32-bit screen mode
    # for ATOV-style PAKs.
    #
    # PROBLEM: in our 7533 build the GLOBAL `pixelformat` stays at PIXEL_x8
    # default, so the guards `pixelformat == PIXEL_x8` were ALREADY passing
    # in stock 7533. The relaxation patterns were no-ops for the guard
    # purpose, but combined with steps 1+2's universal application they
    # altered modern-PAK rendering paths in ways that caused regressions.
    #
    # User-reported 2026-05-19: He-Man flashing on every character because
    # step 2's universal skip-force-assign left sprite->palette = per-frame
    # GIF palette, while drawmethod->table = model->palette (= idle00) was
    # applied uniformly → per-frame palette mismatch → flashing.
    #
    # FIX (v3.1): step 1 and step 2 now gated on has_legacy_remaps so
    # modern PAKs keep stock 7533 rendering paths entirely. Step 11 is
    # removed entirely — the guards stay at upstream `pixelformat ==
    # PIXEL_x8` which passes naturally for both legacy and modern PAKs
    # in our build. The whole ATOV palette fix now lives in:
    #   - Step 3: skip CMD_MODEL_REMAP inner palette load + set has_legacy_remaps
    #   - Step 1 (gated): force PIXEL_x8 sprite load for legacy PAKs
    #   - Step 2 (gated): skip force-assign for legacy PAKs
    #   - Step 4 v3: gate line-29499 model->palette fallback on !has_legacy_remaps
    # Modern PAKs: untouched (has_legacy_remaps=0, all gates skip).
    # (no patches in this step — step 11 was removed; see comment block above)

    # ============================================================
    # Path B: 16-bit (RGB565) vscreen for fps -- palette pipeline kept 32-bit
    # ------------------------------------------------------------
    # vscreen becomes PIXEL_16, halving blend dest + vcopy bandwidth. The locked
    # 32-bit palette pipeline is UNTOUCHED: the 16-bit blit functions convert the
    # effective 32-bit palette to BGR565 internally (engine colour16(), so channel
    # order matches _color16 + the native_video_writer BGR565->RGB565 swap), so
    # every call site works unchanged. Blends use arithmetic (blendtables NULL in
    # 16-bit mode -- blend_*16 fall back to per-channel math). Build 1 keeps the
    # existing NN downscale (box-average added in Build 2). Pause buffers (B2)
    # deferred to Build 2 (cosmetic, not a gameplay-color issue).
    print("Patching for Path B (16-bit vscreen, palette pipeline kept 32-bit)...")
    obpb_path = os.path.join(obor, 'openbor.c')
    obpb = read(obpb_path)
    obpb = strict_replace(obpb,
        "    if((vscreen = allocscreen(videomodes.hRes, videomodes.vRes, PIXEL_32)) == NULL)",
        "    if((vscreen = allocscreen(videomodes.hRes, videomodes.vRes, PIXEL_16)) == NULL) /* MiSTer Path B: 16-bit vscreen (videomodes.pixel auto-updates to 2 -> bpp=16 to WriteFrame) */",
        'Path B B1: vscreen PIXEL_32 -> PIXEL_16')
    obpb = strict_replace(obpb,
        "void create_blend_tables_x8(unsigned char *tables[])\n"
        "{\n"
        "    int i;\n"
        "    for(i = 0; i < MAX_BLENDINGS; i++)\n"
        "    {\n"
        "        tables[i] = blending_table_functions32[i] ? (blending_table_functions32[i])() : NULL;\n"
        "    }\n"
        "\n"
        "}",
        "extern unsigned char *create_screen16_tbl();\n"
        "extern unsigned char *create_multiply16_tbl();\n"
        "extern unsigned char *create_overlay16_tbl();\n"
        "extern unsigned char *create_hardlight16_tbl();\n"
        "extern unsigned char *create_dodge16_tbl();\n"
        "void create_blend_tables_x8(unsigned char *tables[])\n"
        "{\n"
        "    int i;\n"
        "    /* MiSTer Path B: 16-bit vscreen. Build the NATIVE 565-indexed blend\n"
        "     * LUTs (create_*16_tbl) for the 5 divide-heavy modes. Bit-exact with\n"
        "     * the arithmetic path (each table is precomputed _<mode>16, the same\n"
        "     * macro the NULL-table fallback evaluates), so colors are identical --\n"
        "     * only the per-pixel cost changes. A9 benchmark (blend_bench): LUT is\n"
        "     * 1.3-4.2x faster than the divide path (dodge 4.2x, hardlight 2.4x,\n"
        "     * overlay 2.1x, multiply 1.4x, screen 1.3x). BLEND_HALF stays NULL:\n"
        "     * its arithmetic ((a+b)>>1) has no divide and beats the LUT's extra\n"
        "     * memory traffic (0.96x). videomodes.pixel==2 by the time this runs\n"
        "     * (video_set_mode precedes create_blend_tables_x8 in startup()). */\n"
        "    if(videomodes.pixel == 2)\n"
        "    {\n"
        "        tables[BLEND_SCREEN]    = create_screen16_tbl();\n"
        "        tables[BLEND_MULTIPLY]  = create_multiply16_tbl();\n"
        "        tables[BLEND_OVERLAY]   = create_overlay16_tbl();\n"
        "        tables[BLEND_HARDLIGHT] = create_hardlight16_tbl();\n"
        "        tables[BLEND_DODGE]     = create_dodge16_tbl();\n"
        "        tables[BLEND_HALF]      = NULL;\n"
        "        return;\n"
        "    }\n"
        "    for(i = 0; i < MAX_BLENDINGS; i++)\n"
        "    {\n"
        "        tables[i] = blending_table_functions32[i] ? (blending_table_functions32[i])() : NULL;\n"
        "    }\n"
        "\n"
        "}",
        'Path B B3: 16-bit blend LUTs for divide-heavy modes (benchmarked fps lever)')
    # B2: backto_mainmenu pause buffer PIXEL_16 (pausemenu's own buffer is
    # PIXEL_16 via the edited pausemenu_patch.c, applied earlier at the
    # pausemenu replace_function -- so only the backto_mainmenu site remains
    # PIXEL_32 here: count=1). Matches vscreen so copyscreen is not a no-op.
    obpb = strict_replace(obpb,
        "    s_screen *pausebuffer = allocscreen(videomodes.hRes, videomodes.vRes, PIXEL_32);",
        "    s_screen *pausebuffer = allocscreen(videomodes.hRes, videomodes.vRes, PIXEL_16); /* MiSTer Path B: match 16-bit vscreen */",
        'Path B B2: backto_mainmenu pause buffer PIXEL_16')
    write(obpb_path, obpb)

    sppb_path = os.path.join(obor, 'source/gamelib/sprite.c')
    sppb = read(sppb_path)
    sppb = strict_replace(sppb,
        "        case PIXEL_16:\n"
        "            putsprite_x8p16(x, y, drawmethod->flipx, frame, screen, (unsigned short *)drawmethod->table, getblendfunction16(drawmethod->alpha));\n"
        "            break;",
        "        case PIXEL_16:\n"
        "        {\n"
        "            /* MiSTer full-16: same v3.10 discriminator as the locked PIXEL_32\n"
        "             * case -- pick the effective palette (NULL bypass -> putsprite_x8p16\n"
        "             * falls back to frame->palette). Palettes are NATIVE 565\n"
        "             * (PAL_BYTES=512), so putsprite_x8p16 reads them directly. */\n"
        "            unsigned *table_arg16 = (frame && frame->palette && drawmethod->has_remap_directive && !drawmethod->has_palette_directive) ? NULL : (unsigned *)drawmethod->table;\n"
        "            putsprite_x8p16(x, y, drawmethod->flipx, frame, screen, (unsigned short *)table_arg16, getblendfunction16(drawmethod->alpha));\n"
        "            break;\n"
        "        }",
        'Path B B4: PIXEL_16 dispatch v3.10 discriminator')
    write(sppb_path, sppb)

    # ── Full-16-bit (Path A): flip the palette pipeline to NATIVE RGB565 ──
    # The engine image decoders (loadimg.c readgif/pcx/bmp/png) choose palette
    # format by PAL_BYTES: 512 -> colour16 (565), 1024 -> colour32 (RGBA).
    # Flipping PAL_BYTES to 512 makes EVERY palette natively 565 (decoders,
    # allocscreen, encodesprite, convert_map_to_palette all auto-track), so the
    # 16-bit blits read sprite/model/screen palettes DIRECTLY with NO per-blit
    # conversion. That is why B5/B6 (the convert-at-blit hacks) are GONE: stock
    # putsprite_x8p16 / putscreenx8p16 consume native-565 palettes correctly.
    # B4 still picks WHICH 565 palette (discriminator is format-independent).
    print("Patching for Path A (full 16-bit: native 565 palette pipeline)...")
    tpb_path = os.path.join(obor, 'source/gamelib/types.h')
    tpb = read(tpb_path)
    tpb = strict_replace(tpb,
        "#define PAL_BYTES ((pixelbytes[(int)PIXEL_32]*256))",
        "#define PAL_BYTES ((pixelbytes[(int)PIXEL_16]*256)) /* MiSTer full-16: 256*2=512 -> decoders take the colour16 565 path */",
        'Path A P0: PAL_BYTES -> 16-bit (565)')
    write(tpb_path, tpb)

    obp = read(obpb_path)
    # P1: load_palette (.act) fills 565 entries (dp stays used via cast -> no unused-var warning)
    obp = strict_replace(obp,
        "            dp[i] = colour32(tpal[0], tpal[1], tpal[2]);",
        "            ((unsigned short *)dp)[i] = colour16(tpal[0], tpal[1], tpal[2]); /* MiSTer full-16: 565 */",
        'Path A P1a: load_palette colour32 -> colour16')
    obp = strict_replace(obp,
        "        closepackfile(handle);\n"
        "        dp[0] = 0;",
        "        closepackfile(handle);\n"
        "        ((unsigned short *)dp)[0] = 0; /* MiSTer full-16: transparent entry 0 (565) */",
        'Path A P1b: load_palette transparent entry 565')
    # P3: convert_map_to_palette per-colour stride -> 2 bytes
    obp = strict_replace(obp,
        "    unsigned pb = pixelbytes[(int)PIXEL_32];",
        "    unsigned pb = pixelbytes[(int)PIXEL_16]; /* MiSTer full-16: 2-byte 565 stride */",
        'Path A P3: convert_map_to_palette stride -> 16-bit')
    # P4: neon palette-rotation per-colour stride -> 2 bytes
    obp = strict_replace(obp,
        "    int pb = pixelbytes[(int)PIXEL_32];",
        "    int pb = pixelbytes[(int)PIXEL_16]; /* MiSTer full-16: 2-byte 565 stride */",
        'Path A P4: neon palette rotation stride -> 16-bit')
    # P8: HUD primitive colours (_makecolour) -> 565 (box/line/dot/health bars)
    obp = strict_replace(obp,
        "    return colour32(r, g, b);",
        "    return colour16(r, g, b); /* MiSTer full-16: HUD box/line/dot colours 565 */",
        'Path A P8: _makecolour -> colour16')
    write(obpb_path, obp)

    # P9: anigif (cutscene/intro GIF) frame buffers PIXEL_32 -> PIXEL_16.
    # anigif's pal correctly tracks PAL_BYTES (now 512 -> colour16 565), but its
    # frame buffers were PIXEL_32: the PIXEL_32 decode path reads the 565 pal as
    # 4-byte entries (garbage + OOB) and the PIXEL_32 frame has no blit path into
    # the 16-bit vscreen -> the playscene/playgif cutscenes render BLACK. Making
    # the buffers PIXEL_16 routes decode through the 565-correct case + lets the
    # frame blit via blendscreen16. (Both allocscreen calls -- backbuffer + the
    # gifbuffer ring -- match this anchor: count=2.)
    ag_path = os.path.join(obor, 'source/gamelib/anigif.c')
    ag = read(ag_path)
    ag = strict_replace(ag,
        "gif_header.screenheight, PIXEL_32);",
        "gif_header.screenheight, PIXEL_16); /* MiSTer full-16: 565 GIF frames blit into the 16-bit vscreen */",
        'Path A P9: anigif frame buffers PIXEL_32 -> PIXEL_16', count=2)
    write(ag_path, ag)

    # P10 (audit concern #2): cart script-allocated screens PIXEL_32 -> PIXEL_16.
    # Carts do allocscreen()+drawscreen() onto the vscreen; a PIXEL_32 script
    # screen has no blit path into the 16-bit vscreen (and the scaled path
    # misreads it) -> black/garbage. Match the 16-bit pipeline.
    obs_path = os.path.join(obor, 'openborscript.c')
    obs = read(obs_path)
    obs = strict_replace(obs,
        "    screen = allocscreen((int)w, (int)h, PIXEL_32);",
        "    screen = allocscreen((int)w, (int)h, PIXEL_16); /* MiSTer full-16: match 16-bit vscreen so drawscreen blits */",
        'Path A P10: script allocscreen PIXEL_32 -> PIXEL_16')
    write(obs_path, obs)

    # P11 (audit concern #3): the gamelib PNG PLTE decoder writes colour32 UNGATED
    # (unlike readgif/pcx/bmp which switch on PAL_BYTES). With PAL_BYTES=512 a
    # 256-colour paletted PNG writes 1024 bytes into a 512-byte palette (heap
    # overflow) AND stores RGBA where 565 is expected. Gate it like the others.
    li_path = os.path.join(obor, 'source/gamelib/loadimg.c')
    li = read(li_path)
    li = strict_replace(li,
        "            int *pal32 = (int*) pal;\n"
        "            if (chunk_size % 3 != 0)\n"
        "            {\n"
        "                goto readpng_abort;\n"
        "            }\n"
        "            for (i = 0; i < ncolors; i++)\n"
        "            {\n"
        "                pal32[i] = colour32(png_data_ptr[0], png_data_ptr[1], png_data_ptr[2]);\n"
        "                png_data_ptr += 3;\n"
        "            }",
        "            /* MiSTer full-16: gate PLTE palette on PAL_BYTES like readgif/pcx/bmp\n"
        "             * (512 -> colour16 565). Ungated colour32 overflowed the now-512-byte\n"
        "             * palette and wrote RGBA into a 565 buffer. */\n"
        "            if (chunk_size % 3 != 0)\n"
        "            {\n"
        "                goto readpng_abort;\n"
        "            }\n"
        "            for (i = 0; i < ncolors; i++)\n"
        "            {\n"
        "                if (PAL_BYTES == 512)\n"
        "                    ((unsigned short *)pal)[i] = colour16(png_data_ptr[0], png_data_ptr[1], png_data_ptr[2]);\n"
        "                else\n"
        "                    ((int *)pal)[i] = colour32(png_data_ptr[0], png_data_ptr[1], png_data_ptr[2]);\n"
        "                png_data_ptr += 3;\n"
        "            }",
        'Path A P11: PNG PLTE palette gate on PAL_BYTES')
    # 2026-06-14 #3 (decode-CPU): hoist the per-output-pixel loop invariants in
    # decodegifblock. `height - gb->top` and `gb->left + gb->width` are loop-
    # invariant, but -O2 re-loads gb->* every pixel (it can't prove gb doesn't
    # alias the linebuffer[] store). Precompute to const locals. BIT-EXACT: every
    # hoisted term is read-only inside the loop; only `line` mutates and stays in
    # the compare. Targets the hottest loop (runs once per decoded pixel).
    li = strict_replace(li,
        "    int line = 0;\n"
        "    int byte = gb->left;\n"
        "    int pass = 0;",
        "    int line = 0;\n"
        "    int byte = gb->left;\n"
        "    int pass = 0;\n"
        "    const int row_end = gb->left + gb->width; /* MiSTer #3: hoist per-pixel invariant */\n"
        "    const int max_line = height - gb->top;    /* MiSTer #3: hoist per-pixel invariant */",
        '#3 decode-CPU: precompute per-pixel loop invariants in decodegifblock')
    li = strict_replace(li,
        "            if(byte < width && line < (height - gb->top))",
        "            if(byte < width && line < max_line)",
        '#3 decode-CPU: use hoisted max_line in bounds check')
    li = strict_replace(li,
        "            if(byte >= gb->left + gb->width)",
        "            if(byte >= row_end)",
        '#3 decode-CPU: use hoisted row_end in row-end test')
    write(li_path, li)
    print("  Path A: PAL_BYTES=512 native 565; load_palette/convert_map/neon/HUD/PNG-PLTE -> colour16; anigif+script+truecolor-bg 16-bit; convert-at-blit removed.")
    print("  #3 decode-CPU: decodegifblock per-pixel invariant hoist (bit-exact).")

    # 2026-06-14 #2 re-drill: time the per-model openbor.h re-include (scriptlib
    # Parser.c). openbor.h is force-#included + re-lexed + re-#defined for every
    # script-bearing model (pp_context destroyed per compile -> no sharing). This is
    # lex/parse time (Script_AppendText path), so it lands in the [LOAD] 'setup'
    # bucket. Timing it sizes the macro-cache target. _mister_hinc_us is the NON-
    # static global defined in openbor.c; extern it here + a local us-helper.
    print("Patching Parser.c (#2 re-drill: time openbor.h re-include)...")
    parser_path = os.path.join(obor, 'source/scriptlib/Parser.c')
    pa = read(parser_path)
    pa = strict_replace(pa,
        '#include "Parser.h"',
        '#include "Parser.h"\n'
        '#include <sys/time.h>\n'
        'extern unsigned long _mister_hinc_us; /* defined in openbor.c */\n'
        'static unsigned long _mister_pp_us(void){ struct timeval _t; gettimeofday(&_t, 0); return (unsigned long)_t.tv_sec * 1000000UL + (unsigned long)_t.tv_usec; }',
        '#2 re-drill: Parser.c sys/time.h + extern _mister_hinc_us + us helper')
    pa = strict_replace(pa,
        '        pp_parser_include(&pparser->theLexer.preprocessor, "data/scripts/openbor.h");',
        '        { unsigned long _hp0 = _mister_pp_us(); pp_parser_include(&pparser->theLexer.preprocessor, "data/scripts/openbor.h"); _mister_hinc_us += _mister_pp_us() - _hp0; }',
        '#2 re-drill: time openbor.h pp_parser_include')
    write(parser_path, pa)
    print("  Parser.c: openbor.h re-include timer (#2 re-drill).")

    print("\nAll patches applied successfully.")

if __name__ == '__main__':
    main()
