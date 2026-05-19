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

    # ── 3. sdl/video.c — intercept frame present for SDL2 ────────────
    # 7533 uses SDL2 codepaths natively; no compat stubs needed (those
    # were a 4086 + SDL 1.2 hack). The video intercept is documented
    # in patches/openbor_source_patches.c — we now rely on SDL2's
    # standard SDL_BlitSurface + SDL_UpdateWindowSurface block being
    # replaced by NativeVideoWriter_WriteFrame at the dummy driver
    # level (see patch_sdl_dummy.py for SDL_nullframebuffer.c).
    print("Patching sdl/video.c (no compat stubs needed for SDL2 build).")

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

    write(os.path.join(obor, 'openbor.c'), obor_c)
    print("  .cfg/.hi -> /media/fat/config/, .s00 -> /media/fat/savestates/OpenBOR_7533/")

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
    # Add `has_legacy_remaps` flag to s_model struct in openbor.h.
    # Set when CMD_MODEL_REMAP fires (step 3); gates the line-29499
    # model->palette fallback (step 4) so the bypass scopes to legacy-
    # remap PAKs only — modern PAKs (no `remap` declarations) keep
    # drawmethod->table flow normal (KO flash, level palette, globalmap,
    # per-frame remap effects all preserved).
    #
    # Why: original step 4 v2 unconditionally bypassed drawmethod->table
    # whenever frame->palette was populated (always after step 1). That
    # broke Avengers UBF Captain America rendering (sprite GIFs use
    # placeholder palette; canonical red/white/blue comes from a colourmap
    # the engine applies via drawmethod->table — bypass = pink Captain
    # America). User-reported regression 2026-05-19.
    #
    # New design: flag legacy-remap PAKs at character load time, then
    # condition the model->palette fallback at line 29499 on
    # !has_legacy_remaps. ATOV (has_legacy_remaps=1) gets drawmethod->table
    # NULL → sprite->palette wins → canonical Hugo green. Avengers
    # (has_legacy_remaps=0) keeps the fallback → drawmethod->table set →
    # canonical Captain America rendering preserved. KO flash etc. set
    # drawmethod->table BEFORE the fallback check, so they always work
    # regardless of the flag.
    print("Adding has_legacy_remaps field to s_model struct in openbor.h...")
    obh_path = os.path.join(obor, 'openbor.h')
    obh = read(obh_path)
    obh = strict_replace(
        obh,
        "    int maps_loaded; // Used for player colourmap selecting",
        "    int maps_loaded; // Used for player colourmap selecting\n    int has_legacy_remaps; // MiSTer 2026-05-19: set when CMD_MODEL_REMAP fires (ATOV-style PAK); gates line-29499 model->palette fallback so modern PAKs keep drawmethod->table effects",
        'add has_legacy_remaps field to s_model struct'
    )
    write(obh_path, obh)
    print("  s_model.has_legacy_remaps field added")

    print("Patching openbor.c (per-sprite palette: PIXEL_x8 loadsprite + skip force-assign)...")
    ob_path = os.path.join(obor, 'openbor.c')
    ob = read(ob_path)

    # Step 1: loadsprite uses PIXEL_x8 ONLY for legacy-remap PAKs (ATOV-style).
    # Modern PAKs keep upstream behavior: `nopalette ? PIXEL_x8 : PIXEL_8`.
    #
    # NOTE on ordering: `newchar->has_legacy_remaps` is set by step 3 inside
    # the CMD_MODEL_REMAP case during character.txt parse. Well-formatted
    # OpenBOR character files put `remap` declarations near the top of the
    # file, BEFORE animation `anim`/`frame` blocks. So by the time loadsprite
    # fires for the first anim frame, has_legacy_remaps is already set if
    # the PAK uses remap. Modern PAKs that don't use remap → flag stays 0
    # → loadsprite uses upstream PIXEL_x8/PIXEL_8 conditional → stock
    # rendering path preserved → no regression.
    loadsprite_old = "loadsprite(value, offset.x, offset.y, nopalette ? PIXEL_x8 : PIXEL_8); //don't use palette for the sprite since it will one palette from the entity's remap list in 24bit mode"
    loadsprite_new = "loadsprite(value, offset.x, offset.y, (newchar->has_legacy_remaps || nopalette) ? PIXEL_x8 : PIXEL_8); // MiSTer 2026-05-19: force PIXEL_x8 for legacy-remap PAKs (ATOV) so sprite->palette is populated for the per-sprite-palette path; modern PAKs keep upstream PIXEL_8 (sprite has embedded palette) for stock rendering parity"
    ob = strict_replace(ob, loadsprite_old, loadsprite_new, 'step 1: loadsprite PIXEL_x8 gated on has_legacy_remaps')
    print("  loadsprite → PIXEL_x8 ONLY for legacy-remap PAKs (modern PAKs unchanged)")

    # Step 2: skip force-assign ONLY for legacy-remap PAKs. Modern PAKs keep
    # the force-assign so sprite->palette = model->palette consistently across
    # all frames — same as stock 7533. ATOV-style PAKs skip the force-assign
    # so each sprite keeps its own GIF palette (canonical per-frame).
    #
    # Why this matters: He-Man and other modern PAKs without `remap` use the
    # auto-palette block to set model->palette = idle00.gif palette, then
    # force-assign that to all sprites. Skipping the force-assign for modern
    # PAKs leaves sprite->palette = each frame's own GIF palette (different
    # per frame), and the renderer applies drawmethod->table = model->palette
    # (= idle00) across frames — palette mismatch → visible flashing as
    # character animates. User-reported 2026-05-19. Gating step 2 to legacy-
    # only restores stock 7533 behavior for modern PAKs.
    force_assign_old = "                            sprite_map[index].node->sprite->palette = newchar->palette;\n                            sprite_map[index].node->sprite->pixelformat = pixelformat;"
    force_assign_new = "                            // MiSTer 2026-05-19: skip force-assign for legacy-remap PAKs (ATOV-style)\n                            // so each sprite keeps its own GIF palette. Modern PAKs keep stock behavior\n                            // (force-assign newchar->palette to all sprites) to avoid per-frame palette mismatch flashing.\n                            if (!newchar->has_legacy_remaps) sprite_map[index].node->sprite->palette = newchar->palette;\n                            sprite_map[index].node->sprite->pixelformat = pixelformat;"
    ob = strict_replace(ob, force_assign_old, force_assign_new, 'step 2: skip force-assign gated on has_legacy_remaps')
    print("  sprite->palette force-assign skipped ONLY for legacy PAKs (modern PAKs keep stock force-assign)")

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
    remap_load_new = """// PALETTE FIX: skip inner palette load + flag this model as legacy-remap.
                    // Loading from `value` (first remap arg, e.g. run2.gif for Hugo)
                    // makes that GIF's palette the model's master palette → overrides
                    // every sprite via drawmethod->table. Skip it so auto-palette code
                    // at line ~16895 loads from idle00.gif (canonical) instead.
                    //
                    // has_legacy_remaps gates the line-29499 model->palette fallback:
                    // legacy-remap PAKs (ATOV) get drawmethod->table NULL → sprite->palette
                    // wins → canonical render. Modern PAKs (no `remap` declarations,
                    // flag stays 0) keep the fallback → drawmethod->table set normally
                    // → preserves KO flash / level palette / globalmap / per-frame remap
                    // effects. Fixes both ATOV Hugo green AND Avengers Cap red/white/blue.
                    newchar->has_legacy_remaps = 1;"""
    if remap_load_old in ob:
        ob = ob.replace(remap_load_old, remap_load_new)
        print("  CMD_MODEL_REMAP inner palette load skipped (auto-loads from first anim frame)")
    else:
        raise RuntimeError("openbor.c: CMD_MODEL_REMAP palette load pattern not found — moved?")

    # Step 3b (2026-05-19 follow-up): pre-scan character.txt buffer for
    # `remap` declaration BEFORE the line-by-line parse loop fires. Sets
    # has_legacy_remaps early so loadsprite gating (step 1) sees the
    # correct flag value, regardless of where `remap` appears in the file.
    #
    # WHY: step 3 sets has_legacy_remaps inside the CMD_MODEL_REMAP case,
    # which only fires during the line-by-line parse. If `remap` declarations
    # come AFTER `anim`/`frame` blocks in the character.txt (as ATOV
    # character files do), then the first loadsprite calls (icon, early
    # anim frames) fire with has_legacy_remaps still at 0 → step 1 gate
    # uses upstream PIXEL_8 path → sprite->palette not populated → render
    # falls back to wrong colors for ATOV.
    #
    # User-reported 2026-05-19 (verbatim): "atov all the colors regressed
    # to wrong colors again. this is a legacy pak."
    #
    # FIX: scan the buffer once with strstr("\nremap ") + check buffer
    # start with strncmp("remap ", 6) before the parse loop. If found,
    # set newchar->has_legacy_remaps = 1 immediately. The step 3 in-parse
    # set remains as defensive redundancy.
    #
    # Anchor: the line right before the main parse loop's while-loop
    # `while(pos < size)`. Verified against pristine v7533 source/openbor.c
    # line ~12950: `newchar->hitwalltype = -1; // init to -1` appears
    # immediately before the parse loop starts.
    pre_scan_old = "    newchar->hitwalltype = -1; // init to -1\n\n    //char* test = \"load   knife 0\";\n    //ParseArgs(&arglist,test,argbuf);\n\n    // Now interpret the contents of buf line by line\n    while(pos < size)"
    pre_scan_new = "    newchar->hitwalltype = -1; // init to -1\n\n    /* MiSTer 2026-05-19 step 3b: pre-scan buf for `remap` declaration.\n     * Sets has_legacy_remaps BEFORE the parse loop so loadsprite gating\n     * works regardless of remap-vs-anim ordering in character.txt.\n     * ATOV character files put `anim`/`frame` blocks before `remap`\n     * declarations; without this pre-scan, early loadsprite calls fire\n     * with has_legacy_remaps=0 and miss the PIXEL_x8 path.\n     *\n     * IMPORTANT: ATOV uses TAB separator (`remap\\tA.gif`) not space.\n     * Confirmed via grep on the .pak binary 2026-05-19. We must detect\n     * BOTH `remap ` (space) and `remap\\t` (tab) — OpenBOR ParseArgs\n     * accepts either. Comments like `// remap ...` won't match because\n     * the line starts with `//` not `remap`. */\n    if (buf != NULL && (\n            strstr(buf, \"\\nremap \") != NULL ||\n            strstr(buf, \"\\nremap\\t\") != NULL ||\n            strncmp(buf, \"remap \", 6) == 0 ||\n            strncmp(buf, \"remap\\t\", 6) == 0)) {\n        newchar->has_legacy_remaps = 1;\n    }\n\n    //char* test = \"load   knife 0\";\n    //ParseArgs(&arglist,test,argbuf);\n\n    // Now interpret the contents of buf line by line\n    while(pos < size)"
    ob = strict_replace(ob, pre_scan_old, pre_scan_new, 'step 3b: pre-scan buf for remap declaration before parse loop')
    print("  pre-scan inserted: has_legacy_remaps set BEFORE parse loop (handles remap-after-anim files like ATOV)")

    # NOTE: do NOT write ob here yet — step 4 v3 below makes one more
    # edit to openbor.c (the line-29499 fallback gate). One final write
    # at the end captures all four edits (steps 1, 2, 3, 3b, 4 v3).
    print("  openbor.c per-sprite palette patches: steps 1-3b staged in memory.")

    # Step 4 v3 (option 2, 2026-05-19): gate the line-29499 model->palette
    # fallback on !has_legacy_remaps. Surgical fix that scopes the bypass
    # to ATOV-style PAKs only — modern PAKs keep drawmethod->table flow
    # entirely intact (KO flash, level palette, globalmap, per-frame remap,
    # entity colourmap effects all preserved).
    #
    # History — why this is v3:
    #   v1 (commit 234bc6f): unconditionally NULL drawmethod->table in sprite.c
    #     PIXEL_32 case. Cancelled before deploy when I caught the side effect
    #     would kill all drawmethod-based effects globally.
    #   v2 (commit a728671): conditional `(frame && frame->palette) ? NULL
    #     : drawmethod->table` in sprite.c. Shipped — fixed ATOV but BROKE
    #     Avengers UBF Captain America (rendered pink instead of canonical
    #     red/white/blue). Step 1 forces all sprites to have frame->palette,
    #     so the condition was always true → bypass was effectively
    #     unconditional → all modern PAKs lost drawmethod->table effects.
    #   v3 (this): move the gate from sprite.c to openbor.c, scoped to
    #     the buggy line 29499 specifically. Use has_legacy_remaps flag
    #     (set in step 3 above when CMD_MODEL_REMAP fires). NO sprite.c
    #     modification needed — drawmethod->table flows naturally and is
    #     ignored only when it would be polluted (legacy PAKs) or kept
    #     when it's legitimate (modern PAKs + KO flash etc.).
    #
    # User-reported 2026-05-19 (verbatim): "i noticed in avengers captain
    # america has pink, his correct colors are blue, red, and white."
    # Confirmed the v2 side effect was real, requested v3 (option 2).
    #
    # Pristine v7533 source location (verified via raw.githubusercontent.com):
    #   engine/openbor.c lines 29511-29514:
    #     if(!drawmethod->table)
    #     {
    #         drawmethod->table = e->modeldata.palette;
    #     }
    # (inside an outer `if(!drawmethod->table)` block at line 29480 — the
    # outer block runs only when no explicit setter has fired yet, so KO
    # flash / level palette / globalmap setters at lines 29473/29491/29515
    # /29538-29547 all run BEFORE this fallback and bypass it naturally.)
    print("Gating line-29499 model->palette fallback on !has_legacy_remaps (option 2)...")
    fallback_old = "                        if(!drawmethod->table)\n                        {\n                            drawmethod->table = e->modeldata.palette;\n                        }"
    fallback_new = "                        if(!drawmethod->table && !e->modeldata.has_legacy_remaps)\n                        {\n                            // MiSTer palette fix (option 2, 2026-05-19): skip model->palette\n                            // fallback for legacy-remap PAKs (ATOV — has_legacy_remaps=1).\n                            // Their model->palette was polluted with first-remap-arg's GIF;\n                            // letting drawmethod->table stay NULL means putsprite_x8p32 falls\n                            // back to sprite->palette (canonical per-GIF) → Hugo renders green.\n                            // Modern PAKs (has_legacy_remaps=0) DO take the fallback → model->\n                            // palette feeds drawmethod->table → canonical render via colourmap\n                            // application (Captain America red/white/blue preserved). KO flash,\n                            // level palette, globalmap, per-frame remap all set drawmethod->table\n                            // BEFORE this block and are unaffected by either branch.\n                            drawmethod->table = e->modeldata.palette;\n                        }"
    ob = strict_replace(ob, fallback_old, fallback_new, 'gate line-29499 model->palette fallback on !has_legacy_remaps')
    print("  line 29499 fallback gated: skipped for legacy-remap PAKs (ATOV); preserved for modern PAKs")

    write(ob_path, ob)
    print("  openbor.c: all 4 palette patches (steps 1, 2, 3, 4 v3) written to disk.")

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
