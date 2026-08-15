# Script-save validator — restoring exact shared-take fidelity, safely

**Status: WIRED AND GATED.** A payload `.sNN` is admitted by name and then
parsed against the grammar below before anything is written; the writer runs the
same parser, so nothing this build produces is something its own reader refuses.
The local-savestates seed that stood in for this (`678e438`) is REMOVED — a
replay reads nothing off the receiver's card.

Where the pieces live:

| | |
|---|---|
| the parser | `patches/sdlport_patch.c`, `mrec_snap_script_ok` |
| reader gate | same file, pass 2 of `mrec_extract_snap` — buffered and validated BEFORE the file exists |
| writer gate | `.github/scripts/apply_patches.py`, the payload embed loop (externs the same function) |
| build gate | `apply_patches.py` required signatures (both halves) + a FORBIDDEN list on filesystem calls |
| tests | `tools/harness/test_snap_script.py` (the grammar, cut from the emitted C), `test_snap_extract.py` (the extractor consults it), `test_writer_reader_agree.py` (one parser, one byte cap) |

The rest of this document is the reasoning and the corpus survey, which stay
current: **re-run the survey before widening the grammar.**

## The problem this exists to solve

A `.inp` is meant to be self-sufficient — hand someone the file and they see
exactly what you saw. That holds for the input stream, the RNG seed, `.sav` and
`.hi`. It does NOT hold for unlocked characters, because **the unlocks are a
program**: `saveScriptFile` writes OpenBOR script and `loadScriptFile` compiles
and EXECUTES it. You cannot apply a sender's roster without running their code.

Round 14 found that accepting `.sNN` from a payload was a root RCE (a take
reached `savefilestream`, whose path argument is unsanitised, and could write
`/media/fat/linux/user-startup.sh`). Refusing it closed the hole and cost the
fidelity promise on every PAK that stores progression this way — **12 of the 15
on the dev card**, including TMNT-RP, He-Man, JLL, Avengers, PDC2.

The fix is to whitelist the LANGUAGE rather than the file.

## Corpus survey — measured, not assumed

All 15 `.sNN` on the dev card, 124..1691 bytes:

| | |
|---|---|
| total calls | 248 |
| distinct functions | **2** — `setglobalvar` (147), `changemodelproperty` (101) |
| distinct line shapes | 4 |
| bytes outside printable ASCII | **0** |

Grammar, complete:

```
void main() {
    setglobalvar(STR|NUM, NUM);
    changemodelproperty(STR, NUM, NUM);
}
```

🛑 **Re-run this survey before widening the grammar.** The command:

```sh
cat *.s0* | grep -oE '[A-Za-z_][A-Za-z0-9_]*[[:space:]]*\(' | tr -d ' (' | sort | uniq -c
cat *.s0* | sed 's/"[^"]*"/S/g; s/-\?[0-9][0-9]*\.\?[0-9]*/N/g' | sort -u
```

## Why permitting exactly these two calls is safe

- `setglobalvar` — sets a script variable. Pure state, no reach.
- `changemodelproperty` — `switch`-dispatched on the property index
  (`openborscript.c:2058`), so an out-of-range index falls through rather than
  indexing anything.

Everything that made the hole exploitable — `savefilestream`, `openfilestream`,
anything with filesystem or spawn reach — has **no production in this grammar**
and cannot appear. The validator rejects; it never sanitises or rewrites.

## How it landed (and the two traps)

1. **Wiring.** `mrec_snap_ext_ok` sees only a NAME; validating needs CONTENT,
   which exists only mid-write in pass 2 of the extractor. Pass 1 accepts
   `.sNN` by name and bounds its size; pass 2 buffers the entry, validates, and
   only then writes. It does NOT stream to disk and validate after -- that is a
   window in which a stranger's program sits at the path `loadScriptFile`
   compiles and executes.
2. **The forbidden-gate entry moved.** It used to ban accepting `.sNN` at all.
   That is now REQUIRED (a take must carry its unlocks), paired in the gate with
   the validator call that makes it safe; what is banned instead is a
   filesystem call gaining a production in the grammar.
3. **The writer validates too.** One bad entry refuses the WHOLE payload, so a
   take carrying a script this build's own reader rejects would lose its `.sav`
   and `.hi` as well. The writer externs the SAME function -- never a copy --
   and `test_writer_reader_agree.py` asserts there is exactly one definition and
   one byte cap.
4. **The statement cap had to be made reachable.** At 4096 it could never fire:
   the shortest statement the grammar admits is 19 bytes, so a file at the
   64 KiB byte cap holds ~3450. A bound that cannot fire is decoration. 2048.

## Verified

- `test_snap_script.py` 41/41 -- the grammar, cut from the emitted C
- `test_snap_extract.py` 35/35 -- the extractor consults it (hostile script
  refuses the whole payload; a valid one lands in `savestates/`)
- `test_writer_reader_agree.py` 22/22 -- one parser, one cap
- negative-tested: widening the grammar turns the suite red (40/41), and adding
  `savefilestream` fails the build gate by name. Restored from a byte snapshot.

**Owed:** the hardware test -- TMNT-RP, record with unlocks, replay, confirm the
roster. That is the acceptance test for a self-contained `.inp`, and the
stronger form is a take carried to a MiSTer that never had those unlocks.

## The code

🛑 **Not reproduced here.** It lived in this file while it was a proposal; now
that it ships, a copy in a document is a second version to keep in step, and the
copy is always the one that rots. The source of truth is
`patches/sdlport_patch.c` (`mrec_snap_script_ok` and its `mrec_sv_*` helpers),
which carries the same reasoning as comments and is what
`tools/harness/test_snap_script.py` cuts, compiles and drives.

This section previously held a snapshot with `MREC_SCRIPT_MAX_STMTS 4096` --
already wrong by the time it shipped, because that value cannot fire under the
byte cap. One document, one week, one stale constant: that is the argument.
