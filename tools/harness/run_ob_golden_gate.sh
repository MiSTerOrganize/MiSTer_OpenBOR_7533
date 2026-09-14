#!/bin/bash
# Run the OpenBOR golden-trace regression gate. ONE command, so nobody
# hand-rolls the docker lines -- every mount and flag below is load-bearing.
#
#   bash tools/harness/run_ob_golden_gate.sh            # subset gate (a few minutes)
#   bash tools/harness/run_ob_golden_gate.sh --full     # all 450 PAKs
#   OB=/path/to/OpenBOR_headless  PAKS='Z:\...\Paks'  JOBS=6  bash ...
#
# OpenBOR_headless: pass OB=, or it downloads the artifact from the newest
# successful diff_harness run on main -- the binary CI built from that commit.
#
# What it answers: after an engine change, which PAKs now render or sound
# different from the stored goldens (120 frames from boot, FRAME:VIDEOCRC:AUDIOCRC).
#
# 🛑 NOT A CI GATE, for the same reason as PICO-8's: the PAK library is user
# content in no repo, and the goldens live in the workspace's #Golden_Traces.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WS="$(cd "$REPO/.." && pwd)"

# Default is the LOCAL collection on C:. OB_ALARM is a 25 s WALL-CLOCK limit, so
# a PAK library on a busy drive turns slow loads into false HANGs: the first
# full run read Z: while a collection rebuild wrote to it and reported 7 HANG,
# all 7 of which traced clean (6 DET, 1 its baseline FAIL1) from the local copy.
PAKS="${PAKS:-C:\\Users\\miste\\OneDrive\\Desktop\\MiSTerOrganize\\MiSTerFrontier\\OpenBOR_Paks\\Paks}"
GOLDENS="$WS/#Golden_Traces/OpenBOR_7533"
WORK="${WORK:-$HERE/.ob_golden_gate_work}"
STAGE="$WORK/ob"
JOBS="${JOBS:-6}"

FULL=0
[ "${1:-}" = "--full" ] && FULL=1

[ -d "$GOLDENS/goldens" ] || { echo "ERROR: not found: $GOLDENS/goldens" >&2; exit 2; }
[ -f "$GOLDENS/scan_results.txt" ] || { echo "ERROR: not found: $GOLDENS/scan_results.txt" >&2; exit 2; }
command -v docker >/dev/null || { echo "ERROR: docker not on PATH" >&2; exit 2; }

rm -rf "$WORK"; mkdir -p "$STAGE" "$WORK/out"

if [ -n "${OB:-}" ]; then
    cp "$OB" "$STAGE/OpenBOR_headless"
else
    echo "Fetching OpenBOR_headless from the newest successful diff_harness run on main..."
    RID="$(gh run list -R MiSTerOrganize/MiSTer_OpenBOR_7533 --workflow diff_harness.yml \
             --branch main --status success --limit 1 --json databaseId --jq '.[0].databaseId')"
    [ -n "$RID" ] || { echo "ERROR: no successful diff_harness run on main" >&2; exit 2; }
    gh run download "$RID" -R MiSTerOrganize/MiSTer_OpenBOR_7533 -n openbor-headless-linux -D "$STAGE"
    echo "  from run $RID"
fi
[ -f "$STAGE/OpenBOR_headless" ] || { echo "ERROR: no OpenBOR_headless staged" >&2; exit 2; }
cp "$HERE/ob_golden_check.sh" "$HERE/ob_golden_gate.sh" "$STAGE/"
md5sum "$STAGE/OpenBOR_headless"

# The PAK list, as container paths. The subset names PAK FILES; a subset entry
# that is not in the library is LOUD, because a gate that quietly shrinks is
# worse than one that fails.
LIST="$WORK/paks.txt"; : > "$LIST"
PAKS_POSIX="$(cygpath -u "$PAKS" 2>/dev/null || echo "$PAKS")"
missing=0
if [ "$FULL" -eq 1 ]; then
    ( cd "$PAKS_POSIX" && find . -iname '*.pak' | sed 's|^\./|/paks/|' | sort ) > "$LIST"
else
    while IFS= read -r name; do
        case "$name" in ''|\#*) continue ;; esac
        if [ -f "$PAKS_POSIX/$name" ]; then echo "/paks/$name" >> "$LIST"
        else echo "SUBSET-MISSING|-|$name|not in the library" >> "$WORK/out/missing.txt"; missing=$((missing + 1)); fi
    done < "$HERE/ob_golden_gate_subset.txt"
fi
total=$(wc -l < "$LIST")
[ "$total" -gt 0 ] || { echo "ERROR: no PAKs to run" >&2; exit 2; }
echo "gate: $total PAK(s) ($([ $FULL -eq 1 ] && echo FULL library || echo subset)), $JOBS shard(s)"

# Round-robin shards, so slow PAKs do not all land in one container.
for i in $(seq 1 "$JOBS"); do : > "$WORK/out/shard$i.txt"; done
n=0
while IFS= read -r p; do
    echo "$p" >> "$WORK/out/shard$(( n % JOBS + 1 )).txt"; n=$((n + 1))
done < "$LIST"

W_STAGE="$(cygpath -w "$STAGE" 2>/dev/null || echo "$STAGE")"
W_GOLD="$(cygpath -w "$GOLDENS" 2>/dev/null || echo "$GOLDENS")"
W_OUT="$(cygpath -w "$WORK/out" 2>/dev/null || echo "$WORK/out")"
pids=()
for i in $(seq 1 "$JOBS"); do
    [ -s "$WORK/out/shard$i.txt" ] || continue
    MSYS_NO_PATHCONV=1 docker run --rm \
        -v "$PAKS:/paks:ro" -v "$W_GOLD:/goldens:ro" -v "$W_STAGE:/ob:ro" -v "$W_OUT:/work" \
        ubuntu:24.04 bash /ob/ob_golden_gate.sh "/work/shard$i.txt" "$i" \
        > "$WORK/out/docker$i.log" 2>&1 &
    pids+=($!)
done
for p in "${pids[@]}"; do wait "$p" || true; done

R="$WORK/out/results.txt"
cat "$WORK/out"/results_shard*.txt > "$R" 2>/dev/null || true
[ -f "$WORK/out/missing.txt" ] && cat "$WORK/out/missing.txt" >> "$R"
got=$(wc -l < "$R")
echo "=== class histogram ($got results for $total PAKs) ==="
cut -d'|' -f1 "$R" | sed 's/[0-9]*$//' | sort | uniq -c | sort -rn
if [ "$got" -lt $((total + missing)) ]; then
    echo "GATE FAIL: $(( total + missing - got )) PAK(s) produced no result -- read $WORK/out/docker*.log"
    exit 1
fi
bad=$(grep -cvE '^(MATCH|KNOWNFAIL[0-9]+)\|' "$R" || true)
if [ "${bad:-0}" -gt 0 ]; then
    echo "=== NOT MATCHING (first 25) ==="
    grep -vE '^(MATCH|KNOWNFAIL[0-9]+)\|' "$R" | head -25
    cat <<EOF
GATE FAIL: $bad PAK(s) differ from their golden (results: $R, traces: $WORK/out/actual/)

A DIFF is not automatically an engine bug. Run trace_shard.sh on the PAK (two
runs, compared): DET means the engine is reproducible and the GOLDEN is stale --
expected after a change to anything the first 120 frames touch; re-baseline and
record WHY in #Golden_Traces/OpenBOR_7533/README.md. NONDET is a real
determinism regression: fix the engine, never re-baseline. NOGOLDEN is a PAK
that failed at baseline time and now runs: it needs a golden.
EOF
    exit 1
fi
echo "GATE PASS: every PAK matched its golden (known failures unchanged)"
