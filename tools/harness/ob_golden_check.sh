#!/bin/bash
# OpenBOR golden-trace REGRESSION worker: one OB_TEST run of one PAK, compared
# against the STORED golden. Called by ob_golden_gate.sh inside a container.
#
# The sibling of trace_shard.sh, and the difference matters:
#   trace_shard.sh     runs a PAK TWICE and compares A vs B  -> is it DETERMINISTIC
#   ob_golden_check.sh runs it ONCE and compares to the golden -> did it CHANGE
# The first passes happily when every frame of every PAK changes identically,
# which is exactly the regression the golden corpus exists to catch.
#
#   args: <pak path under /paks>
#   mounts: /paks (ro), /goldens (ro: goldens/ + scan_results.txt), /ob (ro), /work (rw)
#
# 🛑 THE RUN CONDITIONS ARE COPIED FROM trace_shard.sh AND MUST STAY IDENTICAL.
# A golden is only comparable to a run made the same way:
#   - pristine /media/fat config/saves before the run (no carry-over)
#   - OB_TEST=<file> OB_TESTFRAMES=120 OB_FRAMES=200 OB_ALARM=25, timeout 60
#     (OB_TEST implies the synthetic 60.000 fps clock + no wall-clock pacing)
#   - the golden name: basename without .pak, spaces and slashes to '_', then
#     everything outside [A-Za-z0-9._-] dropped
# test_ob_golden_sync.py asserts this file still matches trace_shard.sh.
set -u
pak="$1"
base="$(basename "$pak" .pak)"
safe="$(echo "$base" | tr '/ ' '__' | tr -cd 'A-Za-z0-9._-')"
golden="/goldens/goldens/$safe.trace"
t="$(mktemp /tmp/ob_gc.XXXXXX)"

rm -rf /media/fat/config/* /media/fat/saves/OpenBOR_7533/* \
       /media/fat/savestates/OpenBOR_7533/* 2>/dev/null
OB_PAK="$pak" OB_FRAMES=200 OB_ALARM=25 OB_TEST="$t" OB_TESTFRAMES=120 \
  timeout 60 /tmp/ob >/dev/null 2>&1
ec=$?

# What the baseline scan recorded for this PAK (DET, FAIL1, FAIL139, ...).
was="$(grep -F "|$(basename "$pak")" /goldens/scan_results.txt 2>/dev/null | head -1 | cut -d'|' -f1)"

if   [ $ec -eq 124 ] || [ $ec -eq 98 ]; then cls=HANG
elif [ $ec -ne 0 ]; then
    # A PAK that failed the same way when the goldens were made is not a new
    # regression; any other failure is.
    if [ "$was" = "FAIL$ec" ]; then cls="KNOWNFAIL$ec"; else cls="FAIL$ec"; fi
elif [ ! -f "$golden" ]; then
    # Runs cleanly now but had no golden: it was a failure at baseline time and
    # has since been FIXED. Visible, never silently passed.
    cls=NOGOLDEN
elif cmp -s "$t" "$golden"; then cls=MATCH
else cls=DIFF
fi

detail=""
[ "$cls" = DIFF ] && detail="$(diff "$golden" "$t" 2>/dev/null | head -4 | tr '\n' ' ')"
[ -n "$was" ] && detail="baseline=$was $detail"
echo "$cls|$ec|$(basename "$pak")|$detail" >> "$RES"

if [ "$cls" = DIFF ] || [ "$cls" = NOGOLDEN ]; then
    mkdir -p /work/actual
    mv -f "$t" "/work/actual/$safe.trace"
else
    rm -f "$t"
fi
