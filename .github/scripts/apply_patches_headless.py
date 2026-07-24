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

    # ── 3. Headless scripted-input injection into inputrefresh (the AI bot) ──
    # OpenBOR's `.inp` replay only drives an already-loaded level and contains
    # no menu navigation, so a hands-free headless bot first needs to REACH
    # gameplay by scripting menu input. Both the menus (they read `bothnewkeys`)
    # and in-level play (they read `player[p].keys`) consume
    # `playercontrolpointers[p]->{keyflags,newkeyflags}` via inputrefresh(), so
    # a single injection right after control_update() drives everything.
    #
    # OB_INPUT = path to a timeline file, lines: `startframe endframe hexkeys`
    # (frame = a monotonic per-inputrefresh counter). Keys are the engine FLAG_*
    # bits (START=0x400, ESC=0x1000, MOVEUP=1, MOVERIGHT=8, ATTACK=0x10,
    # JUMP=0x100, ...). newkeyflags is the rising edge vs the previous frame, so
    # spaced rows yield discrete menu "presses". Unset OB_INPUT => no rows =>
    # NO-OP (control path unchanged), so the mass-scan and every other run are
    # unaffected. Headless-only (never in the ship binary).
    #
    # On top of injection, two env triggers exercise OpenBOR's built-in
    # deterministic `.inp` input recorder (the "AI bot" as a debug repro):
    #   OB_RECINP=<N>  : when a level first loads, arm A_REC_REC and stop+write
    #                    /tmp/botrec.inp after N in-level frames. The in-level
    #                    moves come from OB_INPUT, so a scripted session is
    #                    captured hands-free.
    #   OB_PLAYINP=1   : when a level first loads, arm A_REC_PLAY on
    #                    /tmp/botrec.inp — the engine replays the recorded
    #                    per-frame inputs (reseeding RNG from the .inp header).
    # A per-frame FNV-1a state hash of player[0]'s position (+ _time) is logged
    # (`[state] ...`) so two replays can be diffed for determinism. `.inp` byte
    # layout is arch-specific (unsigned long / struct padding), so record and
    # replay must use the SAME build.
    print("Patching openbor.c (headless input injection + .inp record/replay)...")
    o_path = os.path.join(obor, "openbor.c")
    o = read(o_path)
    inj_anchor = "    control_update(playercontrolpointers, MAX_PLAYERS);"
    inj_code = inj_anchor + """
    /* headless AI bot: two-phase scripted input injection + .inp record/replay.
       Menu phase (OB_INPUT) drives while level==NULL, indexed by global frame;
       the instant a level loads we auto-switch to the in-level timeline
       (OB_INPUT2), indexed by frames-since-level-load. This makes the menu
       timeline robust to level-load timing (dense menu presses auto-stop at
       load, so they never pause the running game). Replay leaves OB_INPUT2
       unset => silent in-level => only the .inp drives the player. */
    {
        static int _inj_init = 0, _mn = 0, _ln = 0, _rec_n = -1, _play = 0;
        static long _inj_frame = 0, _lvl_frame = -1;
        static int _lvl_seen = 0, _rec_stopped = 0;
        static unsigned long long _mprev = 0, _lprev = 0, _shash = 1469598103934665603ULL;
        static struct { long a, b; unsigned long long k; } _menu[512], _lvl[512];
        if (!_inj_init) {
            _inj_init = 1;
            const char *_pm = getenv("OB_INPUT"), *_pl = getenv("OB_INPUT2");
            char _ln2[256]; long _a, _b; unsigned long long _k; FILE *_f;
            if (_pm && (_f = fopen(_pm, "r"))) {
                while (_mn < 512 && fgets(_ln2, sizeof(_ln2), _f))
                    if (_ln2[0] != '#' && sscanf(_ln2, "%ld %ld %llx", &_a, &_b, &_k) == 3) {
                        _menu[_mn].a = _a; _menu[_mn].b = _b; _menu[_mn].k = _k; _mn++; }
                fclose(_f);
            }
            if (_pl && (_f = fopen(_pl, "r"))) {
                while (_ln < 512 && fgets(_ln2, sizeof(_ln2), _f))
                    if (_ln2[0] != '#' && sscanf(_ln2, "%ld %ld %llx", &_a, &_b, &_k) == 3) {
                        _lvl[_ln].a = _a; _lvl[_ln].b = _b; _lvl[_ln].k = _k; _ln++; }
                fclose(_f);
            }
            { const char *_r = getenv("OB_RECINP"); if (_r) _rec_n = atoi(_r); }
            { const char *_q = getenv("OB_PLAYINP"); if (_q) _play = 1; }
            if (_mn || _ln || _play || _rec_n >= 0) {
                fprintf(stderr, "[inject] menu=%d rows, level=%d rows, rec=%d play=%d\\n",
                        _mn, _ln, _rec_n, _play); fflush(stderr);
            }
        }
        if (_mn > 0 || _ln > 0 || _play || _rec_n >= 0) {
            unsigned long long _want = 0; int _i;
            if (!level) {
                for (_i = 0; _i < _mn; _i++)
                    if (_inj_frame >= _menu[_i].a && _inj_frame <= _menu[_i].b) _want |= _menu[_i].k;
                playercontrolpointers[0]->keyflags = _want;
                playercontrolpointers[0]->newkeyflags = _want & ~_mprev;
                _mprev = _want;
            } else {
                if (!_lvl_seen) {
                    _lvl_seen = 1; _lvl_frame = _inj_frame;
                    fprintf(stderr, "[inject] entered level at frame %ld\\n", _inj_frame);
                    if (playrecstatus) {
                        strcpy(playrecstatus->path, "/tmp/");
                        strcpy(playrecstatus->filename, "botrec.inp");
                        playrecstatus->begin = 0;
                        if (_play) { playrecstatus->status = A_REC_PLAY;
                            fprintf(stderr, "[inject] REPLAY armed: /tmp/botrec.inp\\n"); }
                        else if (_rec_n >= 0) { playrecstatus->status = A_REC_REC;
                            fprintf(stderr, "[inject] RECORD armed: /tmp/botrec.inp for %d frames\\n", _rec_n); }
                    }
                    fflush(stderr);
                }
                long _lf = _inj_frame - _lvl_frame;
                for (_i = 0; _i < _ln; _i++)
                    if (_lf >= _lvl[_i].a && _lf <= _lvl[_i].b) _want |= _lvl[_i].k;
                playercontrolpointers[0]->keyflags = _want;
                playercontrolpointers[0]->newkeyflags = _want & ~_lprev;
                _lprev = _want;
                if (_rec_n >= 0 && !_rec_stopped && _lf >= _rec_n) {
                    _rec_stopped = 1;
                    if (playrecstatus && playrecstatus->status == A_REC_REC) {
                        stopRecordInputs();
                        playrecstatus->handle = NULL; /* engine leaves this dangling -> avoid double-fclose at exit */
                        fprintf(stderr, "[inject] RECORD stopped+written at level-frame %ld\\n", _lf);
                        fflush(stderr);
                    }
                }
                if (player[0].ent) {
                    unsigned int _xb, _yb, _zb;
                    memcpy(&_xb, &player[0].ent->position.x, 4);
                    memcpy(&_yb, &player[0].ent->position.y, 4);
                    memcpy(&_zb, &player[0].ent->position.z, 4);
                    _shash = (_shash ^ _xb) * 1099511628211ULL;
                    _shash = (_shash ^ _yb) * 1099511628211ULL;
                    _shash = (_shash ^ _zb) * 1099511628211ULL;
                    _shash = (_shash ^ (unsigned long long)_time) * 1099511628211ULL;
                    if ((_lf % 20) == 0) {
                        fprintf(stderr, "[state] lf=%ld x=%d y=%d z=%d h=%016llx\\n",
                                _lf, (int)player[0].ent->position.x, (int)player[0].ent->position.y,
                                (int)player[0].ent->position.z, _shash);
                        fflush(stderr);
                    }
                }
            }
            _inj_frame++;
        }
    }"""
    o = strict_replace(o, inj_anchor, inj_code, "openbor.c headless input injection")
    write(o_path, o)
    print("  two-phase input injection + .inp record/replay hooked into inputrefresh().")

    print("All headless patches applied successfully.")

if __name__ == "__main__":
    main()
