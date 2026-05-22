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

def strict_replace(content, old, new, label):
    """Replace `old` with `new` in content; RAISE if `old` not found.

    Use this instead of `content.replace(old, new)` for patches where a
    silent no-op would corrupt the build. The 2026-05-19 ATOV palette
    session uncovered that the original `source/utils.c` COPY_ROOT_PATH
    macro replacement had been silently failing since the patch was written
    — pattern expected `strncpy(buf, "./", 2)` but pristine upstream v7533
    has `strcpy(buf, "./")`. Saves/Config/SaveStates redirect had been
    broken without anyone noticing because plain `.replace()` returns the
    source unchanged when the pattern doesn't match.
    """
    if old not in content:
        raise RuntimeError(
            f"strict_replace failed for '{label}': pattern not found.\n"
            f"  First 80 chars of expected: {old[:80]!r}\n"
            f"  Verify the pattern matches PRISTINE upstream at "
            f"https://raw.githubusercontent.com/DCurrent/openbor/v7533/engine/..."
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
    # -DSDL2 needed (no codepaths gate on it). Keep -O1 to dodge the
    # GCC aggressive-loop UB that bit us at openbor.c in 4086.
    mf = strict_replace(
        mf,
        "ifdef BUILD_SDL\nCFLAGS \t       += -DSDL=1\nendif",
        "ifdef BUILD_SDL\nCFLAGS \t       += -DSDL=1\nendif\n\n\nifdef BUILD_MISTER\nCFLAGS         += -DMISTER_NATIVE_VIDEO -fcommon -Wno-error -O1 -g -rdynamic -funwind-tables -fasynchronous-unwind-tables -mapcs-frame\nendif",
        'Makefile MISTER_NATIVE_VIDEO CFLAGS injection'
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
    src = strict_replace(
        src,
        '"./Logs/OpenBorLog.txt"',
        '"/media/fat/logs/OpenBOR_7533/OpenBorLog.txt"',
        'source/utils.c OpenBorLog absolute path'
    )
    src = strict_replace(
        src,
        '"./Logs/ScriptLog.txt"',
        '"/media/fat/logs/OpenBOR_7533/ScriptLog.txt"',
        'source/utils.c ScriptLog absolute path'
    )

    write(os.path.join(obor, 'source/utils.c'), src)
    print("  Save path redirected; log path absolute (/media/fat/logs/OpenBOR_7533/).")

    # ── 6c. Patch openbor.c — route .cfg/.hi to Config, .s00 to SaveStates ──
    print("Patching openbor.c (split save directories)...")
    obor_c = read(os.path.join(obor, 'openbor.c'))

    # .cfg files: savesettings/loadsettings → "Config"
    obor_c = strict_replace(
        obor_c,
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 4);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 4);',
        '.cfg path -> Config (savesettings/loadsettings)'
    )

    # default.cfg — v7533 uses strcat instead of strncat with size limit
    obor_c = strict_replace(
        obor_c,
        'getBasePath(path, "Saves", 0);\n    strcat(path, "default.cfg");',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    strcat(path, "default.cfg");',
        'default.cfg path -> Config'
    )

    # .hi files
    obor_c = strict_replace(
        obor_c,
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 1);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 1);',
        '.hi (high score) path -> Config'
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

    # ── 6d. TEMPORARY: profiling instrumentation in update() ─────────────
    # Diagnostic only — REMOVE AFTER MEASUREMENT (Section 10 of audit rule).
    # Injects 4 timer_uticks() markers around major update() sections to
    # identify which section consumes the most CPU. 7533's update() runs at
    # ~29 fps on Cortex-A9 (~34ms/call) vs 4086's ~120 fps (~8ms/call) on the
    # same hardware. Need to find which section in 7533's heavier engine is
    # the bottleneck. Logs 1 line/sec to stderr — well under hotpath threshold
    # per feedback_logging_hotpath_perf.md.
    print("Patching openbor.c (TEMPORARY profiling instrumentation in update())...")

    # Marker 1: top of update() — declare accumulators + capture entry time
    obor_c = strict_replace(
        obor_c,
        'void update(int ingame, int usevwait)\n'
        '{\n'
        '    int i = 0;\n'
        '    int p_keys = 0;\n'
        '\n'
        '    getinterval();',
        'void update(int ingame, int usevwait)\n'
        '{\n'
        '    int i = 0;\n'
        '    int p_keys = 0;\n'
        '\n'
        '#ifdef MISTER_NATIVE_VIDEO\n'
        '    /* MiSTer profiling instrumentation 2026-05-22 (TEMPORARY).\n'
        '     * REMOVE AFTER MEASUREMENT. */\n'
        '    static u64 _perf_sum_scripts = 0, _perf_sum_inner = 0,\n'
        '               _perf_sum_vid_aud = 0, _perf_sum_total = 0;\n'
        '    static int _perf_frames = 0;\n'
        '    static u64 _perf_last_report = 0;\n'
        '    u64 _perf_t0 = timer_uticks();\n'
        '    u64 _perf_t1 = _perf_t0, _perf_t2 = _perf_t0;\n'
        '#endif\n'
        '\n'
        '    getinterval();',
        'perf marker 1 (entry)'
    )

    # Marker 2: after execute_updatescripts block — scripts/input done
    obor_c = strict_replace(
        obor_c,
        '        execute_updatescripts();\n'
        '    }\n'
        '\n'
        '    newtime = 0;',
        '        execute_updatescripts();\n'
        '    }\n'
        '\n'
        '#ifdef MISTER_NATIVE_VIDEO\n'
        '    _perf_t1 = timer_uticks();\n'
        '#endif\n'
        '\n'
        '    newtime = 0;',
        'perf marker 2 (after scripts)'
    )

    # Marker 3: before video_copy_screen — inner game-tick loop done
    obor_c = strict_replace(
        obor_c,
        '    if(usevwait)\n'
        '    {\n'
        '        vga_vwait();\n'
        '    }\n'
        '    video_copy_screen(vscreen);',
        '    if(usevwait)\n'
        '    {\n'
        '        vga_vwait();\n'
        '    }\n'
        '#ifdef MISTER_NATIVE_VIDEO\n'
        '    _perf_t2 = timer_uticks();\n'
        '#endif\n'
        '    video_copy_screen(vscreen);',
        'perf marker 3 (before video)'
    )

    # Marker 4: end of update() — capture + accumulate + report
    obor_c = strict_replace(
        obor_c,
        '    check_music();\n'
        '    sound_update_music();\n'
        '}',
        '    check_music();\n'
        '    sound_update_music();\n'
        '\n'
        '#ifdef MISTER_NATIVE_VIDEO\n'
        '    {\n'
        '        u64 _perf_t3 = timer_uticks();\n'
        '        _perf_sum_total   += (_perf_t3 - _perf_t0);\n'
        '        _perf_sum_scripts += (_perf_t1 - _perf_t0);\n'
        '        _perf_sum_inner   += (_perf_t2 - _perf_t1);\n'
        '        _perf_sum_vid_aud += (_perf_t3 - _perf_t2);\n'
        '        _perf_frames++;\n'
        '        if (_perf_t3 - _perf_last_report >= 1000000) {\n'
        '            int f = _perf_frames > 0 ? _perf_frames : 1;\n'
        '            fprintf(stderr,\n'
        '                "[PERF7533] frames=%d total=%lluus scripts=%lluus inner=%lluus vid_aud=%lluus\\n",\n'
        '                _perf_frames,\n'
        '                (unsigned long long)(_perf_sum_total/f),\n'
        '                (unsigned long long)(_perf_sum_scripts/f),\n'
        '                (unsigned long long)(_perf_sum_inner/f),\n'
        '                (unsigned long long)(_perf_sum_vid_aud/f));\n'
        '            fflush(stderr);\n'
        '            _perf_last_report = _perf_t3;\n'
        '            _perf_sum_scripts = _perf_sum_inner = 0;\n'
        '            _perf_sum_vid_aud = _perf_sum_total = 0;\n'
        '            _perf_frames = 0;\n'
        '        }\n'
        '    }\n'
        '#endif\n'
        '}',
        'perf marker 4 (end + report)'
    )

    write(os.path.join(obor, 'openbor.c'), obor_c)
    print("  .cfg/.hi -> /media/fat/config/, .s00 -> /media/fat/savestates/OpenBOR_7533/")
    print("  update() profiling markers injected (TEMPORARY — REMOVE after measurement)")

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
    print("v3.9: per-model has_remap_directive struct field + drawmethod gate on step 4 v2 bypass")
    print("       — Cap's frame GIFs have GARBAGE embedded palettes; `palette classic.gif` is the")
    print("         canonical render LUT. Modern PAKs need drawmethod->table = classic, NOT bypass.")
    print("       — Legacy ATOV PAKs need sprite->palette bypass for canonical per-frame render.")
    print("       — Struct field added at END of s_model + s_drawmethod (no offset shifts).")

    # ── Step 0 (v3.9): add `int has_remap_directive;` to END of s_model struct
    # in openbor.h. Adding AT END = no offset shifts for existing fields
    # (v3.5 regression cause was middle-of-struct insertion).
    print("Patching openbor.h (add s_model.has_remap_directive at end of struct)...")
    obh_path = os.path.join(obor, 'openbor.h')
    obh = read(obh_path)
    s_model_old = "    char\t\t\t\t\ttest_fixed[MAX_NAME_LEN];\n    char*\t\t\t\t\ttest_pointer;\n\n} s_model;"
    s_model_new = "    char\t\t\t\t\ttest_fixed[MAX_NAME_LEN];\n    char*\t\t\t\t\ttest_pointer;\n\n    int has_remap_directive; /* MiSTer v3.9: set by CMD_MODEL_REMAP only; gates step 4 v2 sprite.c bypass per-model */\n} s_model;"
    obh = strict_replace(obh, s_model_old, s_model_new, 'v3.9: add has_remap_directive to s_model END')
    write(obh_path, obh)
    print("  s_model.has_remap_directive added at struct end")

    # ── Step 0b (v3.9): add `int has_remap_directive;` to END of s_drawmethod
    # struct in types.h. Drawmethod is per-render-call so this field carries
    # the legacy flag from model to sprite.c::dispatch.
    print("Patching types.h (add s_drawmethod.has_remap_directive at end of struct)...")
    types_path = os.path.join(obor, 'source/gamelib/types.h')
    types = read(types_path)
    s_dm_old = "    water_transform water;\t\n\tint tag;\t\t\t\t// ~~\n} s_drawmethod;"
    s_dm_new = "    water_transform water;\t\n\tint tag;\t\t\t\t// ~~\n\tint has_remap_directive; /* MiSTer v3.9: legacy-PAK flag for sprite.c step 4 v2 bypass; copied from model at render-time */\n} s_drawmethod;"
    types = strict_replace(types, s_dm_old, s_dm_new, 'v3.9: add has_remap_directive to s_drawmethod END')
    write(types_path, types)
    print("  s_drawmethod.has_remap_directive added at struct end")

    print("Patching openbor.c (per-sprite palette: PIXEL_x8 loadsprite + skip force-assign)...")
    ob_path = os.path.join(obor, 'openbor.c')
    ob = read(ob_path)

    # ── Step 0c (v3.9): set newchar->has_remap_directive = 1 inside CMD_MODEL_REMAP.
    # Anchor on the unique CMD_MODEL_REMAP case opener.
    set_flag_old = "            case CMD_MODEL_REMAP:\n            {\n                // This command should not be used under 24bit mode, but for old mods, just give it a default palette"
    set_flag_new = "            case CMD_MODEL_REMAP:\n            {\n                newchar->has_remap_directive = 1; /* MiSTer v3.9: legacy-remap discriminator (NOT set by alternatepal which only increments maps_loaded) */\n                // This command should not be used under 24bit mode, but for old mods, just give it a default palette"
    ob = strict_replace(ob, set_flag_old, set_flag_new, 'v3.9 step 0c: set newchar->has_remap_directive=1 in CMD_MODEL_REMAP')
    print("  set newchar->has_remap_directive=1 inside CMD_MODEL_REMAP case")

    # ── Step 0d (v3.9): copy has_remap_directive from model to drawmethod at
    # render time. Inject right after `drawmethod = &commonmethod;` (line ~29635
    # in stock; that's where per-frame drawmethod is finalized).
    print("Patching openbor.c (copy has_remap_directive into per-render drawmethod)...")
    copy_to_dm_old = "                    drawmethod = &commonmethod;\n\n                    if(e->modeldata.alpha >= 1 && e->modeldata.alpha <= MAX_BLENDINGS)"
    copy_to_dm_new = "                    drawmethod = &commonmethod;\n                    drawmethod->has_remap_directive = e->modeldata.has_remap_directive; /* MiSTer v3.9: pass legacy-PAK flag to sprite.c step 4 v2 */\n\n                    if(e->modeldata.alpha >= 1 && e->modeldata.alpha <= MAX_BLENDINGS)"
    ob = strict_replace(ob, copy_to_dm_old, copy_to_dm_new, 'v3.9 step 0d: copy has_remap_directive into commonmethod at render')
    print("  drawmethod->has_remap_directive set at render-time from e->modeldata.has_remap_directive")

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

    write(ob_path, ob)
    print("  openbor.c: 3 palette patches written (steps 1, 2, 3 — line-29499 fallback intact, no struct mods).")

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
        "            /* MiSTer palette fix step 4 v2 (v3.9, 2026-05-20):\n"
        "             * conditional on per-sprite palette AND model-level legacy flag.\n"
        "             *\n"
        "             * Bypass drawmethod->table -> use sprite->palette ONLY when:\n"
        "             *   1. frame->palette is populated (each Hugo frame's canonical GIF palette), AND\n"
        "             *   2. drawmethod->has_remap_directive is set (model parsed CMD_MODEL_REMAP)\n"
        "             *\n"
        "             * For Hugo (ATOV legacy): both TRUE -> use sprite->palette = canonical per-frame ✓\n"
        "             *\n"
        "             * For Cap (modern, alternatepal-only): has_remap_directive=0 -> NO bypass\n"
        "             * -> use drawmethod->table = ent->colourmap = newchar->palette = classic.gif\n"
        "             * (the canonical render LUT for Cap's GARBAGE-palette frame GIFs). ✓\n"
        "             *\n"
        "             * Discovered 2026-05-20: Cap's frame GIFs (att*, c*, id*, ...) have garbage\n"
        "             * embedded palettes (palette MD5 526b13c7) NOT matching classic.gif (1b8e2d5d).\n"
        "             * Cart author's design: use `palette classic.gif` as the master render LUT for\n"
        "             * all frames. v3.7/v3.8 step 4 v2 bypass to sprite->palette broke this for Cap.\n"
        "             * v3.9 gates bypass on has_remap_directive so modern PAKs (Cap/Beast/War Machine/\n"
        "             * He-Man/Avengers/PDC2) keep stock drawmethod->table path. */\n"
        "            unsigned *table_arg = (frame && frame->palette && drawmethod->has_remap_directive) ? NULL : (unsigned *)drawmethod->table;\n"
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

    # FIX for task #10 audio-ducking continuation (2026-05-17 evening):
    # Build 7533 added * 2.5 multiplier to music mix and * 1.5 multiplier to
    # SFX mix vs Build 3366's * 1.0 unity. This makes 7533 audio ~4-8 dB
    # louder than 3366 — peaks frequently hit our envelope limiter threshold
    # during heavy action, ducking gain to ~85% (-1.4 dB). User reports
    # "music fades out briefly when many simultaneous actions are happening
    # and the volume gets loudest" — matches limiter behavior exactly,
    # confirmed correlates with loudest action moments.
    #
    # User-confirmed 2026-05-17: PC 3366 plays MvC heavy scenes with
    # continuous music (no ducking). PC 7533 has audible ducking too.
    #
    # Fix: revert 7533 multipliers to 3366's unity. Audio is overall ~4-8 dB
    # quieter (user compensates via TV/amp volume), but limiter rarely
    # engages → no ducking → music plays continuously like on 3366.
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
