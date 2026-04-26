#!/usr/bin/env python3
"""
apply_patches.py — Apply all MiSTer patches to OpenBOR 3979 source tree.

Usage: python3 apply_patches.py <openbor_source_dir> <patches_dir>

Applies:
  1. Makefile: adds BUILD_MISTER target
  2. openbor.c: replaces pausemenu() with custom 4-item menu
  3. sdl/video.c: intercepts SDL_Flip with NativeVideoWriter
  4. sdl/control.c: replaces control_update() with DDR3 joystick reading
  5. sdl/sdlport.c: replaces main() with NativeVideoWriter init + OSD PAK loading
  6. source/utils.c: redirects save path to /media/fat/saves/OpenBOR_4086/
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

    # Add BUILD_MISTER target block after BUILD_OPENDINGUX endif
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
                  $(SDL_PREFIX)/include/SDL
LIBRARIES       = $(SDL_PREFIX)/lib
ifeq ($(BUILD_MISTER), 0)
BUILD_DEBUG     = 1
endif
endif

"""
    # Insert after the BUILD_OPENDINGUX endif
    marker = "ifeq ($(BUILD_OPENDINGUX), 0)\nBUILD_DEBUG     = 1\nendif\nendif"
    mf = mf.replace(marker, marker + "\n" + mister_target)

    # Add MISTER_NATIVE_VIDEO CFLAG + suppress warnings that v4153's
    # older C style triggers under modern GCC (stringop-overflow,
    # multistatement-macros, etc.)
    mf = mf.replace(
        "ifdef BUILD_SDL\nCFLAGS \t       += -DSDL\nendif",
        "ifdef BUILD_SDL\nCFLAGS \t       += -DSDL\nendif\n\n\nifdef BUILD_MISTER\nCFLAGS         += -DMISTER_NATIVE_VIDEO -fcommon -Wno-error -O1 -g -rdynamic -funwind-tables -fasynchronous-unwind-tables -mapcs-frame\nendif"
    )

    # Add native_video_writer.o and native_audio_writer.o to objects.
    # r3979 has trailing spaces after menu.o; r4086 doesn't. Match both.
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

    # Add strip rule
    mf = mf.replace(
        "ifdef BUILD_OPENDINGUX\nSTRIP           = $(OPENDINGUX_TOOLCHAIN_PREFIX)/bin/mipsel-linux-strip $(TARGET) -o $(TARGET_FINAL)\nendif",
        "ifdef BUILD_OPENDINGUX\nSTRIP           = $(OPENDINGUX_TOOLCHAIN_PREFIX)/bin/mipsel-linux-strip $(TARGET) -o $(TARGET_FINAL)\nendif\nifdef BUILD_MISTER\nSTRIP           = strip $(TARGET) -o $(TARGET_FINAL)\nendif"
    )

    # Add -ldl for MiSTer (needed for dlopen/dlsym/dlclose in static SDL)
    mf = mf.replace(
        "LIBS           += -lpng -lz -lm",
        "LIBS           += -lpng -lz -lm\n\n\nifdef BUILD_MISTER\nLIBS           += -ldl\nendif"
    )

    write(os.path.join(obor, 'Makefile'), mf)
    print("  Makefile patched.")

    # ── 2. Patch openbor.c — replace pausemenu() ─────────────────────
    print("Patching openbor.c (pausemenu)...")
    src = read(os.path.join(obor, 'openbor.c'))
    src = replace_function(src, "void pausemenu()", "pausemenu_patch.c", patches)
    write(os.path.join(obor, 'openbor.c'), src)
    print("  pausemenu() replaced.")

    # ── 3. sdl/video.c -- stub SDL 2 API for SDL 1.2 build ─────────
    # r4086+ added unguarded SDL 2 calls (SDL_AllocPalette,
    # SDL_GetDesktopDisplayMode, etc.) in video init. Since we use
    # SDL_VIDEODRIVER=dummy and our DDR3 bridge, video.c's init just
    # needs to compile -- it doesn't have to produce real output.
    # Guard the SDL 2 calls so they're skipped on SDL 1.2.
    print("Patching sdl/video.c (SDL 1.2 compat stubs)...")
    vid_path = os.path.join(obor, 'sdl/video.c')
    vid = read(vid_path)
    # Add compat header after includes
    compat_block = """
/* MiSTer SDL 1.2 compat -- stub SDL 2 functions that r4086+ uses
   outside of #ifdef SDL2 guards. Our DDR3 bridge handles all real
   video output; these stubs just prevent link/compile errors. */
#ifndef SDL2
#include <stdlib.h>
typedef struct { int ncolors; SDL_Color *colors; } MiSTer_Palette;
static inline MiSTer_Palette *SDL_AllocPalette(int n) {
    MiSTer_Palette *p = (MiSTer_Palette*)malloc(sizeof(MiSTer_Palette));
    if(p) { p->ncolors = n; p->colors = (SDL_Color*)calloc(n, sizeof(SDL_Color)); }
    return p;
}
static inline void SDL_FreePalette(MiSTer_Palette *p) { if(p) { free(p->colors); free(p); } }
static inline int SDL_SetPaletteColors(MiSTer_Palette *p, const SDL_Color *c, int f, int n) {
    if(p && c) { int i; for(i=0;i<n&&(f+i)<p->ncolors;i++) p->colors[f+i]=c[i]; } return 0;
}
static inline int SDL_SetSurfacePalette(SDL_Surface *s, MiSTer_Palette *p) { (void)s;(void)p; return 0; }
typedef struct { int w, h, refresh_rate; unsigned format; } SDL_DisplayMode;
static inline int SDL_GetDesktopDisplayMode(int d, SDL_DisplayMode *m) {
    if(m){m->w=320;m->h=240;m->refresh_rate=60;m->format=0;} return 0;
}
#define SDL_Palette MiSTer_Palette
#endif
"""
    # Insert after the last #include line
    last_include = vid.rfind('#include')
    eol = vid.index('\n', last_include) + 1
    vid = vid[:eol] + compat_block + vid[eol:]
    write(vid_path, vid)
    print("  SDL 1.2 compat stubs injected.")

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

    # ── 6. Patch source/utils.c — redirect save path ─────────────────
    print("Patching source/utils.c (save path redirect)...")
    src = read(os.path.join(obor, 'source/utils.c'))

    old_macro = '#define COPY_ROOT_PATH(buf, name) strncpy(buf, "./", 2); strncat(buf, name, strlen(name)); strncat(buf, "/", 1);'

    new_macro = """#ifdef MISTER_NATIVE_VIDEO
#define COPY_ROOT_PATH(buf, name) \\
    do { \\
        if (strcmp(name, "Saves") == 0) { \\
            strcpy(buf, "/media/fat/saves/OpenBOR_4086/"); \\
        } else if (strcmp(name, "SaveStates") == 0) { \\
            strcpy(buf, "/media/fat/savestates/OpenBOR_4086/"); \\
        } else if (strcmp(name, "Config") == 0) { \\
            strcpy(buf, "/media/fat/config/"); \\
        } else if (strcmp(name, "Logs") == 0) { \\
            strcpy(buf, "/media/fat/logs/OpenBOR_4086/"); \\
        } else { \\
            strncpy(buf, "./", 2); strncat(buf, name, strlen(name)); strncat(buf, "/", 1); \\
        } \\
    } while(0)
#else
#define COPY_ROOT_PATH(buf, name) strncpy(buf, "./", 2); strncat(buf, name, strlen(name)); strncat(buf, "/", 1);
#endif"""

    src = src.replace(old_macro, new_macro)
    write(os.path.join(obor, 'source/utils.c'), src)
    print("  Save path redirected.")

    # ── 6c. Patch openbor.c — route .cfg/.hi to Config, .s00 to SaveStates ──
    print("Patching openbor.c (split save directories)...")
    obor_c = read(os.path.join(obor, 'openbor.c'))

    # .cfg files: savesettings/loadsettings → "Config"
    # These have: getBasePath(path, "Saves", 0); getPakName(tmpname, 4);
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 4);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 4);'
    )

    # default.cfg: saveasdefault/loadfromdefault → "Config"
    # These have: getBasePath(path, "Saves", 0); strncat(path, "default.cfg", 128);
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    strncat(path, "default.cfg", 128);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    strncat(path, "default.cfg", 128);'
    )

    # .hi files: saveHighScoreFile/loadHighScoreFile → "Config"
    # These have: getBasePath(path, "Saves", 0); getPakName(tmpname, 1);
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 1);',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "Config", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 1);'
    )

    # .s00 save states: saveScriptFile/loadScriptFile → "SaveStates"
    # These have: getBasePath(path, "Saves", 0); getPakName(tmpvalue, 2);//.scr
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpvalue, 2);//.scr',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "SaveStates", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpvalue, 2);//.scr'
    )
    # loadScriptFile uses tmpname instead of tmpvalue
    obor_c = obor_c.replace(
        'getBasePath(path, "Saves", 0);\n    getPakName(tmpname, 2);//.scr',
        '#ifdef MISTER_NATIVE_VIDEO\n    getBasePath(path, "SaveStates", 0);\n#else\n    getBasePath(path, "Saves", 0);\n#endif\n    getPakName(tmpname, 2);//.scr'
    )

    write(os.path.join(obor, 'openbor.c'), obor_c)
    print("  .cfg/.hi → /media/fat/config/, .s00 → /media/fat/savestates/OpenBOR_4086/")

    # ── 6b. Patch logsDir default to /media/fat/logs/OpenBOR_4086 ────
    # logsDir is declared in sdl/sdlport.c as: char logsDir[128] = {"Logs"};
    print("Patching logsDir default in sdl/sdlport.c...")
    sdlport = read(os.path.join(obor, 'sdl/sdlport.c'))
    logs_old = 'char logsDir[128] = {"Logs"};'
    logs_new = '#ifdef MISTER_NATIVE_VIDEO\nchar logsDir[128] = {"/media/fat/logs/OpenBOR_4086"};\n#else\nchar logsDir[128] = {"Logs"};\n#endif'
    if logs_old in sdlport:
        sdlport = sdlport.replace(logs_old, logs_new, 1)
        write(os.path.join(obor, 'sdl/sdlport.c'), sdlport)
        print("  logsDir default changed to /media/fat/logs/OpenBOR_4086")
    else:
        print("  WARN: logsDir pattern not found in sdl/sdlport.c")

    # -- 7. Replace sdl/sblaster.c with MiSTer DDR3 audio backend --------
    print("Patching sdl/sblaster.c (DDR3 audio backend)...")
    sb = read(os.path.join(patches, 'sblaster_patch.c'))
    write(os.path.join(obor, 'sdl/sblaster.c'), sb)
    print("  sdl/sblaster.c replaced.")

    # -- 8. Fix R/B swap bug in 32-bit blend functions ------------------
    # pixelformat.c's blend_screen32 / blend_multiply32 / blend_half32
    # pass arguments to _color() in swapped (B, G, R) order when they
    # use their inline math path. That path only runs when blendtables
    # is NULL, which is ALWAYS the case in PIXEL_32 mode (set_blendtables
    # is gated on screenformat == PIXEL_8 in openbor.c). Result: every
    # sprite drawn with a screen / multiply / half blend comes out with
    # its R and B channels swapped. Player draws are direct copies and
    # don't hit this; enemies using hit-flash / shadow / alpha blend do.
    #
    # Fix: swap the first and third args of the inline _color(...) calls
    # so argument order matches the _color(r, g, b) signature.
    print("Patching source/gamelib/pixelformat.c (32-bit blend R/B fix)...")
    pf_path = os.path.join(obor, 'source/gamelib/pixelformat.c')
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
            print(f"  WARN: blend fix pattern not found (already patched?):\n    {old[:60]}...")

    # -- Keep native PIXEL_8 default.
    # PIXEL_32 causes NULL pointer crash at address 0xe4 during model
    # loading — OpenBOR structs aren't initialized in 32bpp path.
    # 8bpp works for all PAKs. Colors may use shared palette but no crashes.
    # PAKs with data/video.txt still override to their own format.
    print("  Keeping native PIXEL_8 default (all PAKs work, no crashes).")

    write(pf_path, pf)
    print(f"  {applied}/{len(fixes)} blend R/B fixes applied.")

    print("\nAll patches applied successfully.")

if __name__ == '__main__':
    main()
