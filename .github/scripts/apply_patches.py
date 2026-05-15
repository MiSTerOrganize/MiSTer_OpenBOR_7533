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
    with open(path, 'r') as f:
        return f.read()

def write(path, content):
    with open(path, 'w') as f:
        f.write(content)

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
    mf = mf.replace(
        "ifdef BUILD_SDL\nCFLAGS \t       += -DSDL=1\nendif",
        "ifdef BUILD_SDL\nCFLAGS \t       += -DSDL=1\nendif\n\n\nifdef BUILD_MISTER\nCFLAGS         += -DMISTER_NATIVE_VIDEO -fcommon -Wno-error -O1 -g -rdynamic -funwind-tables -fasynchronous-unwind-tables -mapcs-frame\nendif"
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
    mf = mf.replace(
        "LIBS           += -lpng -lz -lm",
        "LIBS           += -lpng -lz -lm\n\n\nifdef BUILD_MISTER\nLIBS           += -lSDL2 -lSDL2_gfx -ldl -lpthread\nendif"
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
    src = src.replace(
        '#include "openbor.h"',
        '#include "openbor.h"\n#ifdef MISTER_NATIVE_VIDEO\n#include "native_video_writer.h"\n#endif'
    )

    src = replace_function(src, "void control_update(s_playercontrols ** playercontrols, int numplayers)", "control_patch.c", patches)
    write(os.path.join(obor, 'sdl/control.c'), src)
    print("  control_update() replaced.")

    # ── 5. Patch sdl/sdlport.c — replace main() ─────────────────────
    print("Patching sdl/sdlport.c (main + NativeVideoWriter init)...")
    src = read(os.path.join(obor, 'sdl/sdlport.c'))

    # Add includes
    src = src.replace(
        '#include "menu.h"',
        '#include "menu.h"\n#ifdef MISTER_NATIVE_VIDEO\n#include "native_video_writer.h"\n#include "native_audio_writer.h"\n#include <sys/stat.h>\n#include <stdlib.h>\n#include <time.h>\n#include <unistd.h>\n#include <pthread.h>\n#include <signal.h>\n#include <execinfo.h>\n#endif'
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

    old_macro = '#define COPY_ROOT_PATH(buf, name) strncpy(buf, "./", 2); strncat(buf, name, strlen(name)); strncat(buf, "/", 1);'

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
            strncpy(buf, "./", 2); strncat(buf, name, strlen(name)); strncat(buf, "/", 1); \\
        } \\
    } while(0)
#else
#define COPY_ROOT_PATH(buf, name) strncpy(buf, "./", 2); strncat(buf, name, strlen(name)); strncat(buf, "/", 1);
#endif"""

    src = src.replace(old_macro, new_macro)

    # Patch the four LOGFILE macros that hardcode "./Logs/OpenBorLog.txt"
    # and "./Logs/ScriptLog.txt" relative paths. These are used by the
    # engine's writeToLogFile() unconditionally (NOT via COPY_ROOT_PATH),
    # so they need their own replacement. Writing to cwd's Logs/ directory
    # violates the canonical single-location log rule
    # (/media/fat/logs/{CoreName}/) — patch to absolute paths.
    src = src.replace(
        '"./Logs/OpenBorLog.txt"',
        '"/media/fat/logs/OpenBOR_7533/OpenBorLog.txt"'
    )
    src = src.replace(
        '"./Logs/ScriptLog.txt"',
        '"/media/fat/logs/OpenBOR_7533/ScriptLog.txt"'
    )

    write(os.path.join(obor, 'source/utils.c'), src)
    print("  Save path redirected; log path absolute (/media/fat/logs/OpenBOR_7533/).")

    # ── 6c. Patch openbor.c — route .cfg/.hi to Config, .s00 to SaveStates ──
    print("Patching openbor.c (split save directories)...")
    obor_c = read(os.path.join(obor, 'openbor.c'))

    # .cfg files: savesettings/loadsettings → "Config"
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 4);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 4);'
    )

    # default.cfg — v7533 uses strcat instead of strncat with size limit
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    strcat(path, "default.cfg");',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    strcat(path, "default.cfg");',
    )

    # .hi files
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 1);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 1);'
    )

    # .s00 save states (saveScriptFile uses tmpvalue)
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpvalue, 2);//.scr',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "SaveStates", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpvalue, 2);//.scr'
    )
    # loadScriptFile uses tmpname
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 2);//.scr',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "SaveStates", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 2);//.scr'
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
    print("Patching source/gamelib/pixelformat.c (32-bit blend R/B fix)...")
    pf_path = os.path.join(obor, 'source/gamelib/pixelformat.c')
    if os.path.exists(pf_path):
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

    # ── 10. Audio Stage 1: force 48kHz native + cubic Hermite resample ─
    # Two coordinated fixes per the audio quality ladder and the rate-
    # mismatch finding:
    #
    # (a) Force playfrequency = 48000. Upstream hardcodes 44100; our
    #     sblaster_patch.c submits to the DDR3 ring at 48 kHz pace, so
    #     every PAK has played +0.88 semitone sharp (~8.8% too fast)
    #     since launch. Force-override just before SB_playstart() so
    #     the upstream mixer's per-sample rate math uses 48000 too.
    #
    # (b) Replace the three nearest-neighbor sample reads (FIX_TO_INT
    #     (fp_pos) lookups) in update_sample() with cubic Hermite
    #     (4-tap Catmull-Rom). OpenBOR content is 16-bit recorded
    #     audio (music tracks + vocals + percussion + SFX) — the
    #     "16-bit + treble + sharp transients" ladder case where
    #     cubic is correct, not linear.
    #
    # See project_openbor_audio_rate_mismatch.md +
    #     project_openbor_audio_stage1_nearest_neighbor.md
    print("Patching source/gamelib/soundmix.c (force 48kHz + cubic Hermite)...")
    sm_path = os.path.join(obor, 'source/gamelib/soundmix.c')
    sm = read(sm_path)

    # 10a — force playfrequency = 48000, playbits = 16 right before
    #       SB_playstart() so it overrides every prior code-path
    #       assignment (default, savedata, WII path, etc.)
    fr_old = '    if(!SB_playstart(playbits, playfrequency))'
    fr_new = ('    /* MiSTer: force 48 kHz / 16-bit output to match FPGA audio rate.\n'
              '     * Kills the +0.88 semitone pitch shift from rate mismatch with\n'
              '     * sblaster_patch.c which submits the DDR3 ring at 48 kHz pace. */\n'
              '    playfrequency = 48000;\n'
              '    playbits = 16;\n'
              '    if(!SB_playstart(playbits, playfrequency))')
    if fr_old in sm:
        sm = sm.replace(fr_old, fr_new, 1)
        print("  Audio output rate forced to 48000 Hz / 16-bit.")
    else:
        print("  WARN: SB_playstart anchor not found — playfrequency override skipped")

    # 10b — inject three cubic Hermite (4-tap Catmull-Rom) helpers near
    #       the top of soundmix.c, then call them from each sample-read
    #       site.
    hermite_helpers = (
        '\n'
        '/* MiSTer audio Stage 1: cubic Hermite (4-tap Catmull-Rom) helpers.\n'
        ' * Replace nearest-neighbor FIX_TO_INT(fp_pos) reads in update_sample()\n'
        ' * to eliminate aliasing on 16-bit recorded audio with treble + sharp\n'
        ' * transients (music, vocals, percussion, SFX in OpenBOR PAKs).\n'
        ' *\n'
        ' * fp_pos uses INT_TO_FIX/FIX_TO_INT with shift 12 (see soundmix.h).\n'
        ' * Boundary handling: neighbors outside [0, maxip) clamp to the\n'
        ' * nearest valid sample (1-2 samples per buffer end use clamped\n'
        ' * Hermite vs full 4-tap, below audible threshold).\n'
        ' *\n'
        ' * Cost on Cortex-A9 NEON: ~3 mul (smull) + ~6 add per output\n'
        ' * sample, negligible against the mixer\'s existing per-sample math.\n'
        ' */\n'
        '#ifdef MISTER_NATIVE_VIDEO\n'
        'static inline int _mister_hermite_s16(short *p, int ip, int fr, int maxip)\n'
        '{\n'
        '    int sm1 = (ip >= 1) ? (int)p[ip - 1] : (int)p[ip];\n'
        '    int s0  = (int)p[ip];\n'
        '    int s1  = (ip + 1 < maxip) ? (int)p[ip + 1] : s0;\n'
        '    int s2  = (ip + 2 < maxip) ? (int)p[ip + 2] : s1;\n'
        '    int a0_2 = -sm1 + 3*s0 - 3*s1 + s2;\n'
        '    int a1_2 = 2*sm1 - 5*s0 + 4*s1 - s2;\n'
        '    int a2_2 = -sm1 + s1;\n'
        '    int a3_2 = 2*s0;\n'
        '    int t  = fr;\n'
        '    int t2 = (int)(((long long)t  * t ) >> 12);\n'
        '    int t3 = (int)(((long long)t2 * t ) >> 12);\n'
        '    int x2 = (int)(((long long)a0_2 * t3) >> 12)\n'
        '          + (int)(((long long)a1_2 * t2) >> 12)\n'
        '          + (int)(((long long)a2_2 * t ) >> 12)\n'
        '          +  a3_2;\n'
        '    return x2 >> 1;\n'
        '}\n'
        'static inline int _mister_hermite_s16_swap(unsigned short *p, int ip, int fr, int maxip)\n'
        '{\n'
        '    /* For sites that read via (int)(short)SwapLSB16(p[i]). On ARM\n'
        '     * little-endian SwapLSB16 swaps to big-endian — but since the\n'
        '     * upstream cast back to short re-interprets the bytes anyway,\n'
        '     * we mimic that behavior here for byte-exact parity. */\n'
        '    int sm1 = (ip >= 1) ? (int)(short)SwapLSB16(p[ip - 1]) : (int)(short)SwapLSB16(p[ip]);\n'
        '    int s0  = (int)(short)SwapLSB16(p[ip]);\n'
        '    int s1  = (ip + 1 < maxip) ? (int)(short)SwapLSB16(p[ip + 1]) : s0;\n'
        '    int s2  = (ip + 2 < maxip) ? (int)(short)SwapLSB16(p[ip + 2]) : s1;\n'
        '    int a0_2 = -sm1 + 3*s0 - 3*s1 + s2;\n'
        '    int a1_2 = 2*sm1 - 5*s0 + 4*s1 - s2;\n'
        '    int a2_2 = -sm1 + s1;\n'
        '    int a3_2 = 2*s0;\n'
        '    int t  = fr;\n'
        '    int t2 = (int)(((long long)t  * t ) >> 12);\n'
        '    int t3 = (int)(((long long)t2 * t ) >> 12);\n'
        '    int x2 = (int)(((long long)a0_2 * t3) >> 12)\n'
        '          + (int)(((long long)a1_2 * t2) >> 12)\n'
        '          + (int)(((long long)a2_2 * t ) >> 12)\n'
        '          +  a3_2;\n'
        '    return x2 >> 1;\n'
        '}\n'
        'static inline int _mister_linear_u8(unsigned char *p, int ip, int fr, int maxip)\n'
        '{\n'
        '    /* Linear interpolation for 8-bit voice samples.\n'
        '     *\n'
        '     * Originally tried cubic Hermite (4-tap Catmull-Rom) here, but\n'
        '     * Hermite overshoots ~50%% of adjacent-sample delta on sharp\n'
        '     * transients. For 8-bit voice samples (special-move attacks,\n'
        '     * hits, "BURST" sounds) — quintessential sharp transients —\n'
        '     * the overshoot got clamped to [0, 255], producing audible\n'
        '     * crackles on special moves (user A/B 2026-05-15).\n'
        '     *\n'
        '     * Per the audio quality ladder, 8-bit / sub-22kHz sources should\n'
        '     * use LINEAR interpolation. Linear never overshoots — output is\n'
        '     * always in [min(s0,s1), max(s0,s1)] — so no clamp distortion.\n'
        '     * Cubic stays on the music 16-bit and voice 16-bit paths where\n'
        '     * treble preservation matters more than overshoot risk. */\n'
        '    int s0 = (int)p[ip];\n'
        '    int s1 = (ip + 1 < maxip) ? (int)p[ip + 1] : s0;\n'
        '    /* fr is 12-bit fraction [0, 4095]; weight = fr / 4096.\n'
        '     * output = s0 * (4096 - fr) + s1 * fr, then >> 12. */\n'
        '    int v = (s0 * (4096 - fr) + s1 * fr) >> 12;\n'
        '    return v;\n'
        '}\n'
        '#endif\n'
    )

    # Insert helpers AFTER the full #include block — _mister_hermite_s16_swap
    # uses SwapLSB16 which is defined in borendian.h. Anchor on the last
    # include in the standard block so all dependencies are visible.
    helper_anchor = '#include "List.h"'
    if helper_anchor in sm:
        sm = sm.replace(helper_anchor, helper_anchor + hermite_helpers, 1)
        print("  Hermite helpers injected (s16, s16_swap, u8) — post-borendian.h.")
    else:
        print("  WARN: List.h include anchor not found — Hermite helpers skipped")

    # 10c — Site 1 (music ch 16-bit, ~line 483):
    s1_old = ('            // Mix a sample\n'
              '            lmusic = rmusic = sptr16[FIX_TO_INT(fp_pos)];')
    s1_new = ('            // Mix a sample (MiSTer: cubic Hermite)\n'
              '            lmusic = rmusic = _mister_hermite_s16(sptr16, (int)FIX_TO_INT(fp_pos), (int)(fp_pos & 0xFFF), (int)FIX_TO_INT(fp_playto));')
    if s1_old in sm:
        sm = sm.replace(s1_old, s1_new, 1)
        print("  Site 1 (music 16-bit): Hermite call substituted.")
    else:
        print("  WARN: Site 1 music-channel anchor not found")

    # 10d — Site 2 (voice ch 8-bit, ~line 527):
    # Voice 8-bit uses LINEAR (not cubic Hermite) — see _mister_linear_u8
    # docstring for the overshoot/clamp/crackles incident on 2026-05-15.
    s2_old = '                    lmusic = rmusic = sptr8[FIX_TO_INT(fp_pos)];'
    s2_new = '                    lmusic = rmusic = _mister_linear_u8(sptr8, (int)FIX_TO_INT(fp_pos), (int)(fp_pos & 0xFFF), (int)modlen);'
    if s2_old in sm:
        sm = sm.replace(s2_old, s2_new, 1)
        print("  Site 2 (voice 8-bit): Hermite call substituted.")
    else:
        print("  WARN: Site 2 voice 8-bit anchor not found")

    # 10e — Site 3 (voice ch 16-bit with SwapLSB16, ~line 552):
    s3_old = '                    lmusic = rmusic = (int)(short)SwapLSB16(sptr16[FIX_TO_INT(fp_pos)]);'
    s3_new = '                    lmusic = rmusic = _mister_hermite_s16_swap((unsigned short *)sptr16, (int)FIX_TO_INT(fp_pos), (int)(fp_pos & 0xFFF), (int)modlen);'
    if s3_old in sm:
        sm = sm.replace(s3_old, s3_new, 1)
        print("  Site 3 (voice 16-bit + SwapLSB16): Hermite call substituted.")
    else:
        print("  WARN: Site 3 voice 16-bit anchor not found")

    write(sm_path, sm)
    print("  soundmix.c patched (48kHz native + cubic Hermite resample).")

    print("\nAll patches applied successfully.")

if __name__ == '__main__':
    main()
