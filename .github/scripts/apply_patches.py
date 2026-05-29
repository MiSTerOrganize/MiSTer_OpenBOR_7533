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
    write(pf_path, pf)
    print("  packfile.c: CACHEBLOCKS=255 + readahead=65536 (paired filecache speedup)")

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
    s_model_v310_new = "    int has_remap_directive; /* MiSTer v3.9: set by CMD_MODEL_REMAP only; gates step 4 v2 sprite.c bypass per-model */\n    int has_palette_directive; /* MiSTer v3.10: set by CMD_MODEL_PALETTE; tightens step 4 v2 gate for modern PAKs that ALSO use remap (e.g., TMNT-RP) */\n    int gravity_directive_seen; /* MiSTer Step 31 v2: set by CMD_MODEL_SUBJECT_TO_GRAVITY parser; gates ent_default_init force-gravity for TYPE_NONE */\n    int no_adjust_base_directive_seen; /* MiSTer Step 31 v3: set by CMD_MODEL_NO_ADJUST_BASE parser; gates ent_default_init force-no-adjust-base for TYPE_NONE */\n} s_model;"
    obh = strict_replace(obh, s_model_v310_old, s_model_v310_new, 'v3.10 + Step 31 v2 + v3: add directive_seen fields to s_model END')
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

    # -- Step 17 (2026-05-26): RE-INTRODUCED FPS profile + SUB-PROFILE v8.
    # Goal: measure post-Step 14/15/16 fps lift across the 7-PAK regression set
    # against pre-optimization baseline. SUB-PROFILE v8 retained to verify the
    # arrange bucket actually dropped to ~5% (was 28-42% pre-Step 14).
    #
    # Same 8 patches as previously (5 FPS + 3 SUB). Markers present -> CI gate
    # fires -> binary stays off main as workflow artifact for manual deploy.
    # REVERT after measurement cycle completes.

    # -- TEMPORARY PER-FRAME PROFILE 2026-05-25 (DIAG -- REVERT AFTER MEASURED).
    # Same approach as load-time SUB-PROFILE v2-v7 but for per-FRAME work.
    # Goal: identify which engine subsystem dominates per-frame CPU time on
    # CPU-bound PAKs (Avengers UBF ~30 fps, He-Man ~33 fps at stock 800 MHz).
    #
    # 5 patches:
    #   13i: file-scope globals (frame counter, cumulative subsystem timers)
    #   13j: entity timer wrapping while(_time < newtime) tick loop
    #   13k: render timer wrapping display_ents() call
    #   13l: script timer wrapping execute_updatedscripts() call
    #   13m: FPS counter + periodic printf at top of if(ingame==1 && !_pause) block
    #
    # Output: [FPS] N.N avg (frames=N entity=Nms render=Nms script=Nms other=Nms
    # interval=Nms) every ~5 sec during actual gameplay. Diagnostic auto-skips
    # title screens / menus / pause (gated on ingame==1 && !_pause).
    #
    # REVERT after one measurement cycle, same as load-time profile work.

    # Patch 13i: file-scope globals before update() definition.
    # NOTE: previous attempt put globals before playlevel() but the FPS code
    # (while loop, display_ents call, etc.) is actually in update() which
    # is defined EARLIER in the file than playlevel. C requires declaration
    # before use. Globals now placed before update() (line 45669 pristine).
    fps_globals_old = (
        "void update(int ingame, int usevwait)\n"
        "{\n"
        "    int i = 0;\n"
        "    int p_keys = 0;"
    )
    fps_globals_new = (
        "/* MiSTer 2026-05-25 TEMPORARY per-frame profile diagnostic. */\n"
        "static unsigned int _mister_fps_frames = 0;\n"
        "static unsigned int _mister_fps_t_last_print = 0;\n"
        "static unsigned int _mister_fps_entity_ms = 0;\n"
        "static unsigned int _mister_fps_render_ms = 0;\n"
        "static unsigned int _mister_fps_script_ms = 0;\n"
        "/* SUB-PROFILE v8 globals are declared earlier (before update_ents) -- */\n"
        "/* see TEMPORARY SUB-PROFILE v8 patch (REVERT AFTER MEASURED). */\n"
        "/* MiSTer 2026-05-27 TEMPORARY SUB-PROFILE v9 (REVERT AFTER MEASURED): */\n"
        "/* outer-loop instrumentation for the 'other' bucket on JL Legacy. */\n"
        "static unsigned int _mister_o9_input_ms = 0;\n"
        "static unsigned int _mister_o9_keysc_ms = 0;\n"
        "static unsigned int _mister_o9_vwait_ms = 0;\n"
        "static unsigned int _mister_o9_vcopy_ms = 0;\n"
        "static unsigned int _mister_o9_audio_ms = 0;\n"
        "/* MiSTer 2026-05-27 TEMPORARY SUB-PROFILE v10 (REVERT AFTER MEASURED): */\n"
        "/* spriteq_draw timer to confirm the ~6 ms/frame unmeasured rem. */\n"
        "static unsigned int _mister_o10_spriteq_ms = 0;\n"
        "/* MiSTer 2026-05-27 TEMPORARY SUB-PROFILE v11 (REVERT AFTER MEASURED): */\n"
        "/* spriteq_draw internal breakdown: identify which putsprite variant */\n"
        "/* dominates on wide-source PAKs. Non-static so spriteq.c can extern. */\n"
        "unsigned int _mister_o11_sort_ms = 0;\n"
        "unsigned int _mister_o11_putsprite_ms = 0;\n"
        "unsigned int _mister_o11_putsprite_count = 0;\n"
        "unsigned int _mister_o11_putother_ms = 0;\n"
        "/* MiSTer 2026-05-27 TEMPORARY SUB-PROFILE v12 (REVERT AFTER MEASURED): */\n"
        "/* Split putother into putscreen / putpixel / putline / putbox to find */\n"
        "/* which dispatch dominates on Avengers (putother is 49% of spriteq). */\n"
        "unsigned int _mister_o12_putscreen_ms = 0;\n"
        "unsigned int _mister_o12_putscreen_count = 0;\n"
        "unsigned int _mister_o12_putpixel_ms = 0;\n"
        "unsigned int _mister_o12_putline_ms = 0;\n"
        "unsigned int _mister_o12_putbox_ms = 0;\n"
        "unsigned int _mister_o12_putbox_count = 0;\n"
        "\n"
        "void update(int ingame, int usevwait)\n"
        "{\n"
        "    int i = 0;\n"
        "    int p_keys = 0;"
    )
    ob = strict_replace(ob, fps_globals_old, fps_globals_new,
                        'Step 13i: per-frame profile globals before update()')

    # Patch 13j: entity timer start (before while(_time < newtime)).
    fps_entity_start_old = "        while(_time < newtime)"
    fps_entity_start_new = (
        "        unsigned int _mister_ent_t0 = timer_gettick();  /* TEMP profile */\n"
        "        while(_time < newtime)"
    )
    ob = strict_replace(ob, fps_entity_start_old, fps_entity_start_new,
                        'Step 13j: entity timer start before tick loop')

    # Patch 13k: entity timer end (after ++_time; }) -- same scope as start.
    fps_entity_end_old = (
        "            ++_time;\n"
        "        }"
    )
    fps_entity_end_new = (
        "            ++_time;\n"
        "        }\n"
        "        _mister_fps_entity_ms += timer_gettick() - _mister_ent_t0;  /* TEMP profile */"
    )
    ob = strict_replace(ob, fps_entity_end_old, fps_entity_end_new,
                        'Step 13k: entity timer end after tick loop')

    # Patch 13l: render timer wrapping display_ents() call.
    fps_render_old = (
        "    if(ingame == 1 || check_in_screen())\n"
        "        if(!_pause)\n"
        "        {\n"
        "            display_ents();\n"
        "        }"
    )
    fps_render_new = (
        "    if(ingame == 1 || check_in_screen())\n"
        "        if(!_pause)\n"
        "        {\n"
        "            unsigned int _mister_rnd_t0 = timer_gettick();  /* TEMP profile */\n"
        "            display_ents();\n"
        "            _mister_fps_render_ms += timer_gettick() - _mister_rnd_t0;  /* TEMP profile */\n"
        "        }"
    )
    ob = strict_replace(ob, fps_render_old, fps_render_new,
                        'Step 13l: render timer around display_ents()')

    # Patch 13m: script timer wrapping execute_updatedscripts() call.
    fps_script_old = (
        "    if(ingame == 1 || alwaysupdate)\n"
        "    {\n"
        "        execute_updatedscripts();\n"
        "    }"
    )
    fps_script_new = (
        "    if(ingame == 1 || alwaysupdate)\n"
        "    {\n"
        "        unsigned int _mister_scr_t0 = timer_gettick();  /* TEMP profile */\n"
        "        execute_updatedscripts();\n"
        "        _mister_fps_script_ms += timer_gettick() - _mister_scr_t0;  /* TEMP profile */\n"
        "    }"
    )
    ob = strict_replace(ob, fps_script_old, fps_script_new,
                        'Step 13m: script timer around execute_updatedscripts()')

    # Patch 13n: FPS counter + periodic printf at top of if(ingame==1 && !_pause).
    # Block fires once per render frame ONLY when in gameplay AND not paused --
    # title/intro/menus/pause are auto-skipped. Logs once per ~5 sec.
    fps_print_old = (
        "    if(ingame == 1 && !_pause)\n"
        "    {\n"
        "        draw_scrolled_bg();"
    )
    fps_print_new = (
        "    if(ingame == 1 && !_pause)\n"
        "    {\n"
        "        /* MiSTer 2026-05-25 TEMP per-frame profile: log [FPS] N.N every ~5 sec */\n"
        "        {\n"
        "            unsigned int _now_ms = timer_gettick();\n"
        "            if (_mister_fps_t_last_print == 0) _mister_fps_t_last_print = _now_ms;\n"
        "            _mister_fps_frames++;\n"
        "            if (_now_ms - _mister_fps_t_last_print >= 5000) {\n"
        "                unsigned int interval = _now_ms - _mister_fps_t_last_print;\n"
        "                unsigned int fps_x10 = (_mister_fps_frames * 10000u) / interval;\n"
        "                unsigned int sub_sum = _mister_fps_entity_ms + _mister_fps_render_ms + _mister_fps_script_ms;\n"
        "                unsigned int other_ms = (interval > sub_sum) ? (interval - sub_sum) : 0u;\n"
        "                printf(\"[FPS] %u.%u avg (frames=%u entity=%ums render=%ums script=%ums other=%ums interval=%ums)\\n\",\n"
        "                       fps_x10 / 10u, fps_x10 % 10u,\n"
        "                       _mister_fps_frames,\n"
        "                       _mister_fps_entity_ms,\n"
        "                       _mister_fps_render_ms,\n"
        "                       _mister_fps_script_ms,\n"
        "                       other_ms,\n"
        "                       interval);\n"
        "                /* SUB-PROFILE v8 — REVERT AFTER MEASURED — entity-internal breakdown. */\n"
        "                printf(\"[SUB] entity=%ums = script=%ums + ai=%ums + anim=%ums + coll=%ums + arrange=%ums\\n\",\n"
        "                       _mister_fps_entity_ms,\n"
        "                       _mister_se_script_ms,\n"
        "                       _mister_se_ai_ms,\n"
        "                       _mister_se_anim_ms,\n"
        "                       _mister_se_coll_ms,\n"
        "                       _mister_se_arr_ms);\n"
        "                /* SUB-PROFILE v9+v10 — REVERT AFTER MEASURED — outer-loop breakdown. */\n"
        "                printf(\"[OTH] input=%ums keysc=%ums vwait=%ums vcopy=%ums audio=%ums spriteq=%ums\\n\",\n"
        "                       _mister_o9_input_ms,\n"
        "                       _mister_o9_keysc_ms,\n"
        "                       _mister_o9_vwait_ms,\n"
        "                       _mister_o9_vcopy_ms,\n"
        "                       _mister_o9_audio_ms,\n"
        "                       _mister_o10_spriteq_ms);\n"
        "                /* SUB-PROFILE v11 — REVERT AFTER MEASURED — spriteq internal. */\n"
        "                printf(\"[SPQ] sort=%ums putsprite=%ums (%u calls) putother=%ums\\n\",\n"
        "                       _mister_o11_sort_ms,\n"
        "                       _mister_o11_putsprite_ms,\n"
        "                       _mister_o11_putsprite_count,\n"
        "                       _mister_o11_putother_ms);\n"
        "                /* SUB-PROFILE v12 — REVERT AFTER MEASURED — putother breakdown. */\n"
        "                printf(\"[SP2] putscreen=%ums (%u calls) putpixel=%ums putline=%ums putbox=%ums (%u calls)\\n\",\n"
        "                       _mister_o12_putscreen_ms,\n"
        "                       _mister_o12_putscreen_count,\n"
        "                       _mister_o12_putpixel_ms,\n"
        "                       _mister_o12_putline_ms,\n"
        "                       _mister_o12_putbox_ms,\n"
        "                       _mister_o12_putbox_count);\n"
        "                _mister_fps_frames = 0;\n"
        "                _mister_fps_entity_ms = 0;\n"
        "                _mister_fps_render_ms = 0;\n"
        "                _mister_fps_script_ms = 0;\n"
        "                _mister_se_script_ms = 0;\n"
        "                _mister_se_ai_ms = 0;\n"
        "                _mister_se_anim_ms = 0;\n"
        "                _mister_se_coll_ms = 0;\n"
        "                _mister_se_arr_ms = 0;\n"
        "                _mister_o9_input_ms = 0;\n"
        "                _mister_o9_keysc_ms = 0;\n"
        "                _mister_o9_vwait_ms = 0;\n"
        "                _mister_o9_vcopy_ms = 0;\n"
        "                _mister_o9_audio_ms = 0;\n"
        "                _mister_o10_spriteq_ms = 0;\n"
        "                _mister_o11_sort_ms = 0;\n"
        "                _mister_o11_putsprite_ms = 0;\n"
        "                _mister_o11_putsprite_count = 0;\n"
        "                _mister_o11_putother_ms = 0;\n"
        "                _mister_o12_putscreen_ms = 0;\n"
        "                _mister_o12_putscreen_count = 0;\n"
        "                _mister_o12_putpixel_ms = 0;\n"
        "                _mister_o12_putline_ms = 0;\n"
        "                _mister_o12_putbox_ms = 0;\n"
        "                _mister_o12_putbox_count = 0;\n"
        "                _mister_fps_t_last_print = _now_ms;\n"
        "            }\n"
        "        }\n"
        "        draw_scrolled_bg();"
    )
    ob = strict_replace(ob, fps_print_old, fps_print_new,
                        'Step 13n: per-frame profile counter + periodic [FPS] printf')

    # -- TEMPORARY SUB-PROFILE v8 2026-05-26 (REVERT AFTER MEASURED).
    # Break down the entity bucket (which dominates per-frame CPU on Avengers
    # at ~482 ms per 5-sec window in the 30-35 fps band) into the 5 sub-system
    # calls inside update_ents(): execute_updateentity_script / check_ai /
    # update_animation / check_attack / arrange_ents. Goal: identify which
    # sub-system to optimize.
    #
    # 6 patches (13n2 + 13o-13s).
    # Output line: [SUB] entity=Nms = script=N + ai=N + anim=N + coll=N + arrange=N

    # Patch 13n2: SUB-PROFILE v8 globals BEFORE update_ents().
    # NOTE: update_ents() is defined at ~line 29247 pristine, update() at ~45669.
    # Globals MUST be declared before the FIRST consumer = before update_ents().
    # The FPS-profile globals (patch 13i) sit before update() and only feed update()
    # itself; SUB-PROFILE v8 globals are read by update_ents() (the [SUB] timers)
    # AND written by the printf inside update() (the reset block after [SUB] printf),
    # so the SUB-PROFILE v8 globals are placed earlier in the file.
    se_globals_old = "void update_ents()\n{\n    int i;"
    se_globals_new = (
        "/* MiSTer 2026-05-26 TEMPORARY SUB-PROFILE v8 (REVERT AFTER MEASURED). */\n"
        "/* Per-frame breakdown INSIDE update_ents() -- script/ai/anim/coll/arrange. */\n"
        "unsigned int _mister_se_script_ms = 0;\n"
        "unsigned int _mister_se_ai_ms = 0;\n"
        "unsigned int _mister_se_anim_ms = 0;\n"
        "unsigned int _mister_se_coll_ms = 0;\n"
        "unsigned int _mister_se_arr_ms = 0;\n"
        "\n"
        "void update_ents()\n"
        "{\n"
        "    int i;"
    )
    ob = strict_replace(ob, se_globals_old, se_globals_new,
                        'Step 13n2: SUB-PROFILE v8 globals before update_ents()')

    # Patch 13o: time execute_updateentity_script(self) per entity.
    se_script_old = (
        "                execute_updateentity_script(self);// execute a script\n"
        "                if(!self->exists)\n"
        "                {\n"
        "                    continue;\n"
        "                }\n"
        "                check_ai();// check ai"
    )
    se_script_new = (
        "                {\n"
        "                    unsigned int _se_t0 = timer_gettick();  /* TEMP SUB-PROFILE v8 */\n"
        "                    execute_updateentity_script(self);// execute a script\n"
        "                    _mister_se_script_ms += timer_gettick() - _se_t0;\n"
        "                }\n"
        "                if(!self->exists)\n"
        "                {\n"
        "                    continue;\n"
        "                }\n"
        "                {\n"
        "                    unsigned int _se_t0 = timer_gettick();  /* TEMP SUB-PROFILE v8 */\n"
        "                    check_ai();// check ai\n"
        "                    _mister_se_ai_ms += timer_gettick() - _se_t0;\n"
        "                }"
    )
    ob = strict_replace(ob, se_script_old, se_script_new,
                        'Step 13o/13p: SUB-PROFILE v8 timers around execute_updateentity_script + check_ai')

    # Patch 13q: time update_animation() per entity.
    se_anim_old = (
        "                update_animation(); // if not frozen, update animation\n"
        "                if(!self->exists)\n"
        "                {\n"
        "                    continue;\n"
        "                }\n"
        "                check_attack();// Collission detection"
    )
    se_anim_new = (
        "                {\n"
        "                    unsigned int _se_t0 = timer_gettick();  /* TEMP SUB-PROFILE v8 */\n"
        "                    update_animation(); // if not frozen, update animation\n"
        "                    _mister_se_anim_ms += timer_gettick() - _se_t0;\n"
        "                }\n"
        "                if(!self->exists)\n"
        "                {\n"
        "                    continue;\n"
        "                }\n"
        "                {\n"
        "                    unsigned int _se_t0 = timer_gettick();  /* TEMP SUB-PROFILE v8 */\n"
        "                    check_attack();// Collission detection\n"
        "                    _mister_se_coll_ms += timer_gettick() - _se_t0;\n"
        "                }"
    )
    ob = strict_replace(ob, se_anim_old, se_anim_new,
                        'Step 13q/13r: SUB-PROFILE v8 timers around update_animation + check_attack')

    # Patch 13s: time arrange_ents() called once per tick (post-loop).
    se_arrange_old = (
        "    }//end of for\n"
        "    arrange_ents();"
    )
    se_arrange_new = (
        "    }//end of for\n"
        "    {\n"
        "        unsigned int _se_t0 = timer_gettick();  /* TEMP SUB-PROFILE v8 */\n"
        "        arrange_ents();\n"
        "        _mister_se_arr_ms += timer_gettick() - _se_t0;\n"
        "    }"
    )
    ob = strict_replace(ob, se_arrange_old, se_arrange_new,
                        'Step 13s: SUB-PROFILE v8 timer around arrange_ents() (per-tick, post-loop)')

    # -- TEMPORARY SUB-PROFILE v9 2026-05-27 (REVERT AFTER MEASURED).
    # Times the unmeasured 'other' bucket: inputrefresh, execute_keyscripts,
    # vga_vwait, video_copy_screen, sound_update_music. Goal: identify what
    # in the outer update() loop caused JL Legacy to drop from 86 to 70 fps
    # despite entity-bucket work being unchanged.
    #
    # 5 patches (13t-13x): one per function call in update() outside the
    # existing entity/render/script timers.

    # Patch 13t: time inputrefresh(playrecstatus->status) inside update().
    o9_input_old = (
        "    inputrefresh(playrecstatus->status);\n"
        "    if(playrecstatus->status == A_REC_REC && !_pause && level) if ( !recordInputs() ) stopRecordInputs();"
    )
    o9_input_new = (
        "    {\n"
        "        unsigned int _o9_t0 = timer_gettick();  /* TEMP SUB-PROFILE v9 */\n"
        "        inputrefresh(playrecstatus->status);\n"
        "        _mister_o9_input_ms += timer_gettick() - _o9_t0;\n"
        "    }\n"
        "    if(playrecstatus->status == A_REC_REC && !_pause && level) if ( !recordInputs() ) stopRecordInputs();"
    )
    ob = strict_replace(ob, o9_input_old, o9_input_new,
                        'Step 13t: SUB-PROFILE v9 timer around inputrefresh()')

    # Patch 13u: time execute_keyscripts() inside update().
    o9_keysc_old = (
        "        if(ingame == 1 || check_in_screen())\n"
        "        {\n"
        "            execute_keyscripts();\n"
        "        }"
    )
    o9_keysc_new = (
        "        if(ingame == 1 || check_in_screen())\n"
        "        {\n"
        "            unsigned int _o9_t0 = timer_gettick();  /* TEMP SUB-PROFILE v9 */\n"
        "            execute_keyscripts();\n"
        "            _mister_o9_keysc_ms += timer_gettick() - _o9_t0;\n"
        "        }"
    )
    ob = strict_replace(ob, o9_keysc_old, o9_keysc_new,
                        'Step 13u: SUB-PROFILE v9 timer around execute_keyscripts()')

    # Patch 13v: time vga_vwait() inside update() (the vsync wait — main suspect).
    o9_vwait_old = (
        "    if(usevwait)\n"
        "    {\n"
        "        vga_vwait();\n"
        "    }\n"
        "    video_copy_screen(vscreen);"
    )
    o9_vwait_new = (
        "    if(usevwait)\n"
        "    {\n"
        "        unsigned int _o9_t0 = timer_gettick();  /* TEMP SUB-PROFILE v9 */\n"
        "        vga_vwait();\n"
        "        _mister_o9_vwait_ms += timer_gettick() - _o9_t0;\n"
        "    }\n"
        "    {\n"
        "        unsigned int _o9_t0 = timer_gettick();  /* TEMP SUB-PROFILE v9 */\n"
        "        video_copy_screen(vscreen);\n"
        "        _mister_o9_vcopy_ms += timer_gettick() - _o9_t0;\n"
        "    }"
    )
    ob = strict_replace(ob, o9_vwait_old, o9_vwait_new,
                        'Step 13v/13w: SUB-PROFILE v9 timers around vga_vwait + video_copy_screen')

    # Patch 13x: time sound_update_music() at end of update().
    o9_audio_old = (
        "    check_music();\n"
        "    sound_update_music();\n"
        "}"
    )
    o9_audio_new = (
        "    check_music();\n"
        "    {\n"
        "        unsigned int _o9_t0 = timer_gettick();  /* TEMP SUB-PROFILE v9 */\n"
        "        sound_update_music();\n"
        "        _mister_o9_audio_ms += timer_gettick() - _o9_t0;\n"
        "    }\n"
        "}"
    )
    ob = strict_replace(ob, o9_audio_old, o9_audio_new,
                        'Step 13x: SUB-PROFILE v9 timer around sound_update_music()')

    # -- TEMPORARY SUB-PROFILE v10 2026-05-27 (REVERT AFTER MEASURED).
    # Times spriteq_draw() — the post-tick sprite-rasterization-to-vscreen call
    # that we infer is responsible for the ~6 ms/frame unmeasured remainder in
    # the [FPS] 'other' bucket on JL Legacy. If v10 measurement confirms it,
    # spriteq_draw becomes the next optimization target. If it's smaller than
    # expected, something else in update() (post-while-loop scaffolding) is
    # eating frame budget that we haven't identified.
    o10_spriteq_old = (
        "    spriteq_draw(vscreen, 0, MIN_INT, MAX_INT, 0, 0); // notice, always draw sprites at the very end of other methods"
    )
    o10_spriteq_new = (
        "    {\n"
        "        unsigned int _o10_t0 = timer_gettick();  /* TEMP SUB-PROFILE v10 */\n"
        "        spriteq_draw(vscreen, 0, MIN_INT, MAX_INT, 0, 0); // notice, always draw sprites at the very end of other methods\n"
        "        _mister_o10_spriteq_ms += timer_gettick() - _o10_t0;\n"
        "    }"
    )
    ob = strict_replace(ob, o10_spriteq_old, o10_spriteq_new,
                        'Step 13y: SUB-PROFILE v10 timer around spriteq_draw() inside update()')

    print("  TEMPORARY per-frame profile inserted (5 patches: globals + entity/render/script timers + [FPS] printf gated on ingame==1 && !_pause)")
    print("  TEMPORARY SUB-PROFILE v8 inserted (3 strict_replace patches inside update_ents() — adds [SUB] entity-internal breakdown line)")
    print("  TEMPORARY SUB-PROFILE v9 inserted (5 patches in outer update() loop — adds [OTH] outer-loop breakdown line)")

    # ----- BELOW: original diagnostic patches removed 2026-05-26 ------
    # (FPS profile + SUB-PROFILE v8 served their purpose; reverted now that
    #  B+E ships as the permanent fix.)
    # Removed: fps_globals_new, fps_entity_start, fps_entity_end, fps_render,
    # fps_script, fps_print, se_globals, se_script (+ai), se_anim (+coll), se_arrange.

    write(ob_path, ob)
    print("  openbor.c: 4 palette patches written (steps 1, 2, 3, 12 — line-29499 fallback intact, no struct mods).")

    # -- TEMPORARY SUB-PROFILE v11 2026-05-27 (REVERT AFTER MEASURED) on spriteq.c.
    # Times the inner calls of spriteq_draw() to identify which dispatch
    # (putsprite vs putscreen vs putpixel/line/box) dominates on Avengers
    # (20 ms/frame spriteq) and He-Man (68 ms/frame spriteq). Globals are
    # DEFINED in openbor.c (extended v9/v10 block above); spriteq.c just
    # extern-references and increments them. Output: [SPQ] line alongside
    # existing [FPS]/[SUB]/[OTH].
    spq_path = os.path.join(obor, 'source/gamelib/spriteq.c')
    spq = read(spq_path)

    # Patch v11.1: add timer.h include + extern decls of v11 globals.
    spq_includes_old = (
        "#include <stdio.h>\n"
        "#include \"types.h\"\n"
        "#include \"screen.h\"\n"
        "#include \"sprite.h\"\n"
        "#include \"draw.h\"\n"
        "#include \"globals.h\"\n"
    )
    spq_includes_new = (
        "#include <stdio.h>\n"
        "#include \"types.h\"\n"
        "#include \"screen.h\"\n"
        "#include \"sprite.h\"\n"
        "#include \"draw.h\"\n"
        "#include \"globals.h\"\n"
        "#include \"timer.h\"  /* TEMP SUB-PROFILE v11 — timer_gettick() */\n"
        "\n"
        "/* TEMPORARY SUB-PROFILE v11 (REVERT AFTER MEASURED). */\n"
        "/* Globals DEFINED in openbor.c v9/v10 globals block. */\n"
        "extern unsigned int _mister_o11_sort_ms;\n"
        "extern unsigned int _mister_o11_putsprite_ms;\n"
        "extern unsigned int _mister_o11_putsprite_count;\n"
        "extern unsigned int _mister_o11_putother_ms;\n"
        "/* TEMPORARY SUB-PROFILE v12 (REVERT AFTER MEASURED). */\n"
        "extern unsigned int _mister_o12_putscreen_ms;\n"
        "extern unsigned int _mister_o12_putscreen_count;\n"
        "extern unsigned int _mister_o12_putpixel_ms;\n"
        "extern unsigned int _mister_o12_putline_ms;\n"
        "extern unsigned int _mister_o12_putbox_ms;\n"
        "extern unsigned int _mister_o12_putbox_count;\n"
    )
    spq = strict_replace(spq, spq_includes_old, spq_includes_new,
                        'v11.1: spriteq.c add timer.h + extern v11 globals')

    # Patch v11.2: wrap the body of spriteq_draw with timer pairs around
    # spriteq_sort + putsprite + putscreen/dot/line/box (grouped as
    # 'putother' since they're rare).
    spq_body_old = (
        "void spriteq_draw(s_screen *screen, int newonly, int minz, int maxz, int dx, int dy)\n"
        "{\n"
        "    int i, x, y;\n"
        "\n"
        "    spriteq_sort();\n"
        "\n"
        "    for(i = 0; i < spritequeue_len; i++)\n"
        "    {\n"
        "        if((newonly && spriteq_locked && order[i] < queue + spriteq_old_len) || order[i]->z < minz || order[i]->z > maxz)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        x = order[i]->x + dx;\n"
        "        y = order[i]->y + dy;\n"
        "\n"
        "        switch(order[i]->type)\n"
        "        {\n"
        "        case SQT_SPRITE: // sprite\n"
        "\n"
        "            if(order[i]->params[0])// determin if the sprite's center should be readjusted;\n"
        "            {\n"
        "                ((s_sprite *)(order[i]->frame))->centerx = order[i]->params[1];\n"
        "                ((s_sprite *)(order[i]->frame))->centery = order[i]->params[2];\n"
        "            }\n"
        "            putsprite(x, y, order[i]->frame, screen, &(order[i]->drawmethod));\n"
        "            break;\n"
        "        case SQT_SCREEN: // draw a screen instead of sprite\n"
        "            putscreen(screen, (s_screen *)(order[i]->frame), x, y, &(order[i]->drawmethod));\n"
        "            break;\n"
        "        case SQT_DOT:\n"
        "            putpixel(x, y, order[i]->params[0], screen, &(order[i]->drawmethod));\n"
        "            break;\n"
        "        case SQT_LINE:\n"
        "            putline(x, y, order[i]->params[1] + dx, order[i]->params[2] + dy, order[i]->params[0], screen, &(order[i]->drawmethod));\n"
        "            break;\n"
        "        case SQT_BOX:\n"
        "            putbox(x, y, order[i]->params[1], order[i]->params[2], order[i]->params[0], screen, &(order[i]->drawmethod));\n"
        "            break;\n"
        "        default:\n"
        "            continue;\n"
        "        }\n"
        "    }\n"
        "}"
    )
    spq_body_new = (
        "void spriteq_draw(s_screen *screen, int newonly, int minz, int maxz, int dx, int dy)\n"
        "{\n"
        "    int i, x, y;\n"
        "    unsigned int _o11_t0;  /* TEMP SUB-PROFILE v11 */\n"
        "\n"
        "    _o11_t0 = timer_gettick();\n"
        "    spriteq_sort();\n"
        "    _mister_o11_sort_ms += timer_gettick() - _o11_t0;\n"
        "\n"
        "    for(i = 0; i < spritequeue_len; i++)\n"
        "    {\n"
        "        if((newonly && spriteq_locked && order[i] < queue + spriteq_old_len) || order[i]->z < minz || order[i]->z > maxz)\n"
        "        {\n"
        "            continue;\n"
        "        }\n"
        "\n"
        "        x = order[i]->x + dx;\n"
        "        y = order[i]->y + dy;\n"
        "\n"
        "        switch(order[i]->type)\n"
        "        {\n"
        "        case SQT_SPRITE: // sprite\n"
        "\n"
        "            if(order[i]->params[0])// determin if the sprite's center should be readjusted;\n"
        "            {\n"
        "                ((s_sprite *)(order[i]->frame))->centerx = order[i]->params[1];\n"
        "                ((s_sprite *)(order[i]->frame))->centery = order[i]->params[2];\n"
        "            }\n"
        "            _o11_t0 = timer_gettick();\n"
        "            putsprite(x, y, order[i]->frame, screen, &(order[i]->drawmethod));\n"
        "            _mister_o11_putsprite_ms += timer_gettick() - _o11_t0;\n"
        "            _mister_o11_putsprite_count++;\n"
        "            break;\n"
        "        case SQT_SCREEN: // draw a screen instead of sprite\n"
        "            _o11_t0 = timer_gettick();\n"
        "            putscreen(screen, (s_screen *)(order[i]->frame), x, y, &(order[i]->drawmethod));\n"
        "            { unsigned int _dt = timer_gettick() - _o11_t0;\n"
        "              _mister_o11_putother_ms += _dt;\n"
        "              _mister_o12_putscreen_ms += _dt;\n"
        "              _mister_o12_putscreen_count++; }\n"
        "            break;\n"
        "        case SQT_DOT:\n"
        "            _o11_t0 = timer_gettick();\n"
        "            putpixel(x, y, order[i]->params[0], screen, &(order[i]->drawmethod));\n"
        "            { unsigned int _dt = timer_gettick() - _o11_t0;\n"
        "              _mister_o11_putother_ms += _dt;\n"
        "              _mister_o12_putpixel_ms += _dt; }\n"
        "            break;\n"
        "        case SQT_LINE:\n"
        "            _o11_t0 = timer_gettick();\n"
        "            putline(x, y, order[i]->params[1] + dx, order[i]->params[2] + dy, order[i]->params[0], screen, &(order[i]->drawmethod));\n"
        "            { unsigned int _dt = timer_gettick() - _o11_t0;\n"
        "              _mister_o11_putother_ms += _dt;\n"
        "              _mister_o12_putline_ms += _dt; }\n"
        "            break;\n"
        "        case SQT_BOX:\n"
        "            _o11_t0 = timer_gettick();\n"
        "            putbox(x, y, order[i]->params[1], order[i]->params[2], order[i]->params[0], screen, &(order[i]->drawmethod));\n"
        "            { unsigned int _dt = timer_gettick() - _o11_t0;\n"
        "              _mister_o11_putother_ms += _dt;\n"
        "              _mister_o12_putbox_ms += _dt;\n"
        "              _mister_o12_putbox_count++; }\n"
        "            break;\n"
        "        default:\n"
        "            continue;\n"
        "        }\n"
        "    }\n"
        "}"
    )
    spq = strict_replace(spq, spq_body_old, spq_body_new,
                        'v11.2: spriteq_draw inner timer pairs around sort + putsprite + putother')

    write(spq_path, spq)
    print("  TEMPORARY SUB-PROFILE v11 inserted (2 patches in spriteq.c — adds [SPQ] sort/putsprite/putother breakdown)")

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
        "            s_screen *bg32 = allocscreen(background->width, background->height, PIXEL_32);\n"
        "            if (bg32)\n"
        "            {\n"
        "                unsigned *dst32 = (unsigned *)bg32->data;\n"
        "                unsigned char *src8 = (unsigned char *)background->data;\n"
        "                unsigned *pal_u32 = (unsigned *)background->palette;\n"
        "                int total = background->width * background->height;\n"
        "                int i;\n"
        "                for (i = 0; i < total; i++)\n"
        "                {\n"
        "                    dst32[i] = pal_u32[src8[i]];\n"
        "                }\n"
        "                freescreen(&background);\n"
        "                background = bg32;\n"
        "            }\n"
        "        }\n"
        "    }"
    )
    ob_step23 = strict_replace(ob_step23, step23_old, step23_new,
                                'Step 23: load_background pre-decode 8 -> 32bpp')
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

    print("\nAll patches applied successfully.")

if __name__ == '__main__':
    main()
