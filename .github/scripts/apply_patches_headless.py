#!/usr/bin/env python3
"""apply_patches_headless.py — patch upstream OpenBOR for the headless diff/debug
harness build (diff_harness.yml). SEPARATE from apply_patches.py (the MiSTer ship
build): this applies only the two harness hooks needed to run PAKs off-device:

  1. sdl/sdlport.c : replace main() with the headless main (env OB_PAK, crash +
     SIGALRM-hang handlers, SDL dummy) from patches/headless_patch.c.
  2. sdl/video.c   : inject a per-frame counter + exit-after-OB_FRAMES + alarm
     re-arm into video_copy_screen (so a PAK runs N frames then exits clean, and
     a stuck frame trips SIGALRM).

NO engine-logic patches yet (milestone 1b is crash/hang plumbing). Engine-logic
patches (palette/stale-pointer/screen_status/range/loadsprite-hash) get layered
in a later milestone so the harness tests OUR shipped behavior.

Usage: apply_patches_headless.py <openbor_engine_dir> <patches_dir>
"""
import sys, os

def read(p):
    with open(p, "r", encoding="utf-8", errors="surrogateescape") as f:
        return f.read()

def write(p, c):
    with open(p, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(c)

def extract_function(source, func_sig):
    start = source.find(func_sig)
    if start < 0:
        return None, -1, -1
    brace = 0; found_open = False; end = start
    for i in range(start, len(source)):
        if source[i] == '{':
            brace += 1; found_open = True
        elif source[i] == '}':
            brace -= 1
        if found_open and brace == 0:
            end = i + 1; break
    return source[start:end], start, end

def strict_replace(content, old, new, label, count=1):
    if old not in content:
        raise RuntimeError(f"strict_replace failed for '{label}': pattern not found.\n"
                           f"  expected: {old[:80]!r}")
    n = content.count(old)
    if n != count:
        raise RuntimeError(f"strict_replace failed for '{label}': expected {count}, found {n}.")
    return content.replace(old, new)

def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <openbor_dir> <patches_dir>")
        sys.exit(1)
    obor, patches = sys.argv[1], sys.argv[2]

    # ── 1. Replace main() in sdl/sdlport.c with the headless main ──────
    print("Patching sdl/sdlport.c (headless main)...")
    sp_path = os.path.join(obor, "sdl/sdlport.c")
    sp = read(sp_path)
    sig = "int main(int argc, char *argv[])"
    _, start, end = extract_function(sp, sig)
    if start < 0:
        raise RuntimeError(f"could not find '{sig}' in sdl/sdlport.c")
    patch = read(os.path.join(patches, "headless_patch.c"))
    # Inject the whole patch file (includes + helpers + new main) where the
    # old main was. Helpers land before main; #includes mid-file are legal.
    pstart = patch.find("#include <signal.h>")
    if pstart < 0:
        raise RuntimeError("headless_patch.c missing expected header marker")
    sp = sp[:start] + patch[pstart:] + sp[end:]
    write(sp_path, sp)
    print("  main() replaced with headless main.")

    # ── 2. Inject frame-counter + exit + alarm re-arm into video_copy_screen ─
    print("Patching sdl/video.c (headless frame counter + hang re-arm)...")
    v_path = os.path.join(obor, "sdl/video.c")
    v = read(v_path)
    # Anchor on just the SDL present pair — it exists BOTH in pristine upstream
    # AND inside the #else of apply_patches.py's MISTER_NATIVE_VIDEO bypass, so
    # this works whether or not the ship patcher ran first (engine-logic layer).
    anchor = ("\tSDL_UpdateTexture(texture, NULL, surface->data, surface->pitch);\n"
              "\tblit();")
    repl = ("\t{\n"
            "\t\t/* headless harness: per-frame counter + hang re-arm + exit */\n"
            "\t\tstatic long _hl_n = 0, _hl_max = -2, _hl_alarm = -2;\n"
            "\t\tif (_hl_max == -2) { const char *e = getenv(\"OB_FRAMES\"); _hl_max = e ? atol(e) : 120; }\n"
            "\t\tif (_hl_alarm == -2) { const char *e = getenv(\"OB_ALARM\"); _hl_alarm = e ? atol(e) : 30; }\n"
            "\t\tif (_hl_alarm > 0) alarm((unsigned)_hl_alarm);\n"
            "\t\t_hl_n++;\n"
            "\t\tif (_hl_max > 0 && _hl_n >= _hl_max) {\n"
            "\t\t\tfprintf(stderr, \"[headless] reached %ld frames, exiting clean\\n\", _hl_n);\n"
            "\t\t\tfflush(stderr);\n"
            "\t\t\texit(0);\n"
            "\t\t}\n"
            "\t}\n"
            "\tSDL_UpdateTexture(texture, NULL, surface->data, surface->pitch);\n"
            "\tblit();")
    v = strict_replace(v, anchor, repl, "sdl/video.c headless frame counter")
    # ensure stdlib/unistd are available for getenv/atol/alarm/exit
    if "#include <stdlib.h>" not in v:
        v = "#include <stdlib.h>\n" + v
    if "#include <unistd.h>" not in v:
        v = "#include <unistd.h>\n" + v
    write(v_path, v)
    print("  video_copy_screen frame counter + hang re-arm injected.")

    print("All headless patches applied successfully.")

if __name__ == "__main__":
    main()
