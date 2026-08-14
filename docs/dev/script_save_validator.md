# Script-save validator — restoring exact shared-take fidelity, safely

**Status: DESIGNED + CODE WRITTEN, NOT WIRED.** Shipped behaviour today is
`f5a1928` + `678e438`: a payload `.sNN` is REFUSED, and script-saves are seeded
from the *local* savestates instead. That is safe and is what runs now. This
document is the plan to go further.

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

## Remaining work (the hard part)

1. **Wiring.** `mrec_snap_ext_ok` sees only a NAME; validating needs CONTENT,
   which exists only mid-write in pass 2 of the extractor. Pass 1 must accept
   `.sNN` by name again, and pass 2 must buffer a `.sNN` entry (bounded by
   `MREC_SCRIPT_MAX_BYTES`) and validate BEFORE writing, refusing the whole
   payload on failure. Do not stream a script straight to disk and validate
   after — that is a window where the file exists.
2. **The forbidden-gate entry changes.** `apply_patches.py` currently bans
   `if (mrec_snap_is_script_save(dot)) return 1;`. Once wired, the ban must
   instead assert that acceptance is *conditional on the validator* — otherwise
   the gate blocks the fix.
3. **Tests (`tools/harness/test_snap_script.py`), non-negotiable.** Cut
   `mrec_snap_script_ok` out of the SHIPPED source, compile it natively, and
   drive it. Accept: all 15 corpus files. Refuse, at minimum: `savefilestream`,
   `openfilestream`, `system`, a second function definition, a nested call, a
   backslash escape, an embedded NUL, a high-bit byte, unbalanced braces,
   trailing content after `}`, a statement count over the cap, and a file over
   the byte cap. Distinct exit codes; every refusal asserts `rc == 1`, never
   `rc != 0`.
4. **Register it in `#Debugging_Tools/preflight.py`** — an unregistered checker
   does not exist.

## The code

Written and reviewed, not yet wired. `-Werror` fails an unused `static`, so it
cannot land until step 1 is done.

```c
/* ---------------------------------------------------------------------------
 * Script-save validator.
 *
 * A .sNN is a PROGRAM: saveScriptFile emits it and loadScriptFile compiles and
 * EXECUTES it. That is how OpenBOR persists unlocked characters, so a take
 * cannot reproduce a sender's roster without running their code -- the unlocks
 * ARE the code. Refusing .sNN outright (round 14) closed a root RCE but broke
 * exactly that: a shared take replayed against the receiver's unlocks, so any
 * recording that navigates an unlock-dependent roster diverged.
 *
 * This restores it without trusting the sender, by whitelisting the LANGUAGE
 * rather than the file. A payload .sNN is accepted only if it is EXACTLY:
 *
 *     void main() {
 *         setglobalvar(STR|NUM, NUM);
 *         changemodelproperty(STR, NUM, NUM);
 *         ...
 *     }
 *
 * and nothing else. Anything unrecognised refuses the whole payload.
 *
 * The grammar is not a guess. Every .sNN on the dev card was surveyed --
 * 15 files, 124..1691 bytes, across TMNT-RP / He-Man / JLL / Avengers /
 * Double Dragon / PDC2 and 6 more. Result: 248 calls, exactly TWO distinct
 * functions, four distinct line shapes, zero bytes outside printable ASCII.
 * Re-run that survey before widening anything here.
 *
 * Why these two calls are safe to permit:
 *   setglobalvar        -- sets a script variable. Pure state, no reach.
 *   changemodelproperty -- switch-dispatched on the property index
 *                          (openborscript.c), so an out-of-range index falls
 *                          through instead of indexing anything.
 * The functions that made the original hole exploitable -- savefilestream,
 * openfilestream, and everything else with filesystem or spawn reach -- simply
 * have no production in this grammar and cannot appear.
 *
 * 🛑 DO NOT add a call here to make some PAK work. A new production is a new
 * thing a stranger may run as root. Re-run the corpus survey, and if a call is
 * genuinely needed, establish that it cannot touch the filesystem, spawn, or
 * index memory from its arguments -- and add a hostile case for it to
 * test_snap_script.py, which drives THIS function compiled from THIS source.
 *
 * Rejects rather than sanitises: no rewriting, no "clean it up and run it".
 * Returns 1 = safe to write into .scratch/savestates, 0 = refuse.
 * -------------------------------------------------------------------------*/
#define MREC_SCRIPT_MAX_BYTES 65536
#define MREC_SCRIPT_MAX_STMTS 4096

static const char *mrec_sv_ws(const char *p)
{
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    return p;
}

/* A bare double-quoted run. NO escapes are permitted -- the corpus has none,
 * and allowing a backslash means reasoning about what the engine's lexer does
 * with it. Simpler is the point. */
static const char *mrec_sv_str(const char *p)
{
    if (*p != '"') return NULL;
    p++;
    while (*p && *p != '"')
    {
        if (*p == '\\') return NULL;
        if ((unsigned char)*p < 0x20 || (unsigned char)*p > 0x7e) return NULL;
        p++;
    }
    return (*p == '"') ? p + 1 : NULL;
}

static const char *mrec_sv_num(const char *p)
{
    const char *s = p;
    if (*p == '-') p++;
    if (*p < '0' || *p > '9') return NULL;
    while (*p >= '0' && *p <= '9') p++;
    if (*p == '.')
    {
        p++;
        if (*p < '0' || *p > '9') return NULL;
        while (*p >= '0' && *p <= '9') p++;
    }
    return (p > s) ? p : NULL;
}

/* One argument: a string, or a number. */
static const char *mrec_sv_arg(const char *p, int allow_str)
{
    const char *q;
    p = mrec_sv_ws(p);
    if (*p == '"')
        return allow_str ? mrec_sv_str(p) : NULL;
    q = mrec_sv_num(p);
    return q;
}

static const char *mrec_sv_lit(const char *p, const char *lit)
{
    size_t n = strlen(lit);
    p = mrec_sv_ws(p);
    return strncmp(p, lit, n) == 0 ? p + n : NULL;
}

/* text must be NUL-terminated. len is the byte count before the NUL. */
static int mrec_snap_script_ok(const char *text, long len)
{
    const char *p = text;
    long i;
    int stmts = 0;

    if (!text || len <= 0 || len > MREC_SCRIPT_MAX_BYTES) return 0;

    /* Whole-file byte class first: one pass, so nothing below has to wonder
     * about embedded NULs, control bytes or high-bit sequences. */
    for (i = 0; i < len; i++)
    {
        unsigned char c = (unsigned char)text[i];
        if (c == '\t' || c == '\n' || c == '\r') continue;
        if (c < 0x20 || c > 0x7e) return 0;
    }
    if ((long)strlen(text) != len) return 0;      /* no embedded NUL */

    if (!(p = mrec_sv_lit(p, "void")))   return 0;
    if (!(p = mrec_sv_lit(p, "main")))   return 0;
    if (!(p = mrec_sv_lit(p, "(")))      return 0;
    if (!(p = mrec_sv_lit(p, ")")))      return 0;
    if (!(p = mrec_sv_lit(p, "{")))      return 0;

    for (;;)
    {
        const char *q;
        p = mrec_sv_ws(p);
        if (*p == '}') { p++; break; }
        if (++stmts > MREC_SCRIPT_MAX_STMTS) return 0;

        if ((q = mrec_sv_lit(p, "setglobalvar")))
        {
            if (!(q = mrec_sv_lit(q, "(")))     return 0;
            if (!(q = mrec_sv_arg(q, 1)))       return 0;   /* name: str or num */
            if (!(q = mrec_sv_lit(q, ",")))     return 0;
            if (!(q = mrec_sv_arg(q, 0)))       return 0;   /* value: num only */
            if (!(q = mrec_sv_lit(q, ")")))     return 0;
            if (!(q = mrec_sv_lit(q, ";")))     return 0;
            p = q; continue;
        }
        if ((q = mrec_sv_lit(p, "changemodelproperty")))
        {
            if (!(q = mrec_sv_lit(q, "(")))     return 0;
            if (!(q = mrec_sv_arg(q, 1)))       return 0;   /* model name */
            if (!(q = mrec_sv_lit(q, ",")))     return 0;
            if (!(q = mrec_sv_arg(q, 0)))       return 0;   /* property index */
            if (!(q = mrec_sv_lit(q, ",")))     return 0;
            if (!(q = mrec_sv_arg(q, 0)))       return 0;   /* value */
            if (!(q = mrec_sv_lit(q, ")")))     return 0;
            if (!(q = mrec_sv_lit(q, ";")))     return 0;
            p = q; continue;
        }
        return 0;                                  /* anything else: refuse */
    }

    p = mrec_sv_ws(p);
    return *p == 0;                                /* nothing may follow main() */
}

```
