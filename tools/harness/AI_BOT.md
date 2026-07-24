# OpenBOR headless AI bot — scripted input + `.inp` record/replay

The hands-free gameplay-repro mechanism for the OpenBOR diff harness (full-audit
Section 12 "AI bot" row). It has two layers, both wired into the headless build
by `.github/scripts/apply_patches_headless.py` (headless-only — never in the ship
binary), hooked right after `control_update()` in `inputrefresh()`:

1. **Scripted input injection** — feed a per-frame controller timeline so the
   headless engine navigates menus and reaches gameplay with no human. Menus read
   `bothnewkeys` and in-level play reads `player[p].keys`, both derived from
   `playercontrolpointers[0]->{keyflags,newkeyflags}` in `inputrefresh()`, so a
   single injection drives everything.
2. **`.inp` record/replay** — arm OpenBOR's built-in deterministic input recorder
   the instant a level loads: record the (scripted) in-level session to an `.inp`,
   or replay one. Replay reseeds the RNG from the `.inp` header and feeds exact
   per-frame inputs, so a recorded repro re-runs identically forever.

All controlled by env vars (unset ⇒ complete no-op, so the mass-scan / normal
runs are unaffected):

| env | meaning |
|---|---|
| `OB_INPUT=<file>`  | **menu** timeline — applied while `level==NULL`, indexed by global frame. Rows auto-stop the instant a level loads (so dense menu presses never pause the running game). |
| `OB_INPUT2=<file>` | **in-level** timeline — applied while a level is loaded, indexed by frames-since-level-load. |
| `OB_RECINP=<N>`    | when a level loads, record the next `N` in-level frames to `/tmp/botrec.inp`. In-level moves come from `OB_INPUT2`, so a scripted session is captured. |
| `OB_PLAYINP=1`     | when a level loads, replay `/tmp/botrec.inp` (leave `OB_INPUT2` unset so only the `.inp` drives the player). |

Timeline row = `startframe endframe hexkeys`. `#` = comment. Keys are the engine
`FLAG_*` bits: `START=0x400`, `ESC=0x1000`, `MOVEUP=1`, `MOVEDOWN=2`,
`MOVELEFT=4`, `MOVERIGHT=8`, `ATTACK=0x10`, `JUMP=0x100`, `SPECIAL=0x200`.
`newkeyflags` is the rising edge vs the previous frame, so spaced rows yield
discrete menu "presses". Example timelines: `ai_menu_start.txt` (dense START
pulses to walk any PAK's title→menu→level) + `ai_lvl_moves.txt` (walk right +
jump + attack).

## Verified walkthrough (A Tale of Vengeance, x86 headless, 2026-07-23)

```
# 1) record: bot navigates to LEVEL1, walks right + jumps, records 200 frames
OB_PAK=".../A Tale of Vengeance.pak" OB_INPUT=ai_menu_start.txt \
  OB_INPUT2=ai_lvl_moves.txt OB_RECINP=200 OB_FRAMES=1200 ./OpenBOR_headless
#   -> [inject] entered level at frame 591; player x:20->116 y:0->81 (walk+jump)
#   -> /tmp/botrec.inp written (30 KB)

# 2) replay (OB_INPUT2 unset -> only the .inp drives the player)
OB_PAK="..." OB_INPUT=ai_menu_start.txt OB_PLAYINP=1 OB_FRAMES=1200 ./OpenBOR_headless
#   -> [state] stream reproduces the walk+jump
```

Determinism confirmed: two replays produced **byte-identical** `[state]` streams
(a per-frame FNV-1a hash of player[0]'s position). Reaching gameplay is robust to
level-load timing because the menu timeline auto-stops the moment `level` loads.

## Found engine bug (first use of the recorder headless)

`stopRecordInputs()` (writing the `.inp`) aborts with a glibc `double free`
during its final `fclose` — **after** the file is fully written+flushed, so the
recording is valid and the crash is cleanup-only. The recorder leaves
`playrecstatus->handle` dangling and the exit path double-fcloses; NULLing the
handle after the call did not prevent it, so the double-free is inside
`stopRecordInputs`'s own `fclose` (heap-state dependent — pin with GDB per the
crash-forensics rule before patching). Replay is unaffected. Recorded on the
device the recorder is driven by the pause-menu flow, which may mask it; worth
a fix pass. The `.inp` byte layout is arch-specific (`unsigned long` size +
struct padding), so record and replay must use the SAME build.
