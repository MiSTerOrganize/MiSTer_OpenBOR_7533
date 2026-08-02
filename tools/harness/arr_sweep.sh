#!/bin/sh
# arr_sweep.sh -- unattended [ARR] entity-count sweep across a PAK list.
#
# Runs the arm32 headless engine (built with the TEMPORARY DIAG [ARR] probe)
# per PAK, driven by the AI-bot scripted input so it reaches level 1 with no
# human, and records the peak live entity count + arrange-bucket cost.
#
# WHY DETACHED: a long remote command must never be held open in a WinSCP
# session -- one 20000-frame run already outlived a session and had to be killed
# by the watchdog. This is started with nohup, writes progress to a log, and is
# polled afterwards. It also self-bounds every PAK with a per-run timeout so a
# wedged engine can never run forever.
#
#   nohup sh arr_sweep.sh <paklist> <outdir> [frames] [per_pak_timeout_s] &
#
# paklist = one PAK FILENAME per line (no path, no quoting needed).
# Results: <outdir>/arr_results.txt (one block per PAK) + <outdir>/sweep.log
set -u
LIST="${1:?paklist required}"
OUT="${2:?outdir required}"
FRAMES="${3:-30000}"
TMO="${4:-420}"

BENCH=/media/fat/bench_ob
PAKS=/media/fat/games/OpenBOR/Paks
mkdir -p "$OUT"
RES="$OUT/arr_results.txt"
LOG="$OUT/sweep.log"
: > "$RES"
: > "$LOG"

echo "sweep start $(date)  frames=$FRAMES timeout=${TMO}s" >> "$LOG"

while IFS= read -r name; do
    [ -z "$name" ] && continue
    case "$name" in \#*) continue ;; esac
    if [ ! -f "$PAKS/$name" ]; then
        echo "MISSING: $name" >> "$LOG"
        printf '=== %s\nSTATUS missing\n\n' "$name" >> "$RES"
        continue
    fi
    echo "--- $(date +%H:%M:%S) running: $name" >> "$LOG"
    RAW="$OUT/raw.txt"
    : > "$RAW"

    # run under a watchdog so one wedged PAK cannot stall the sweep
    LD_LIBRARY_PATH="$BENCH/oblibs_arm32" \
    OB_PAK="$PAKS/$name" \
    OB_INPUT="$BENCH/ai_menu.txt" \
    OB_INPUT2="$BENCH/ai_lvl.txt" \
    OB_FRAMES="$FRAMES" \
    nice -n 19 "$BENCH/OpenBOR_headless_arm32" > "$RAW" 2>&1 &
    CPID=$!
    ( sleep "$TMO"; kill -9 $CPID 2>/dev/null ) 2>/dev/null &
    WPID=$!
    wait $CPID
    RC=$?
    kill $WPID 2>/dev/null

    # peak ent_max across every [ARR] line this PAK produced
    PEAK=$(grep -o 'peak=[0-9]*' "$RAW" | cut -d= -f2 | sort -n | tail -1)
    [ -z "$PEAK" ] && PEAK=0
    NARR=$(grep -c '\[ARR\]' "$RAW")
    ENTERED=$(grep -c 'entered level' "$RAW")

    {
        printf '=== %s\n' "$name"
        printf 'STATUS rc=%s arr_lines=%s entered_level=%s peak_ent_max=%s\n' \
               "$RC" "$NARR" "$ENTERED" "$PEAK"
        grep '\[ARR\]' "$RAW" | tail -6
        printf '\n'
    } >> "$RES"
    echo "    rc=$RC arr=$NARR entered=$ENTERED peak=$PEAK" >> "$LOG"
done < "$LIST"

echo "sweep done $(date)" >> "$LOG"
