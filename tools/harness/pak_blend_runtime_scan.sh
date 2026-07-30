#!/bin/bash
# OpenBOR diff-harness -- RUNTIME blend-mode census over a whole PAK library.
# Tier-B Phase-1b LAYER 2. Runs INSIDE a glibc container, in parallel.
#
# Sibling of pak_run_scan.sh (which classifies crashes/hangs). This one drives
# each PAK into GAMEPLAY with the generic AI-bot menu timeline and harvests the
# [TBB] blend histogram the headless build dumps just before its clean exit.
#
#   docker run --rm \
#     -v "<PAKS_DIR>:/paks:ro" \
#     -v "<DIR_WITH_OpenBOR_headless_+_ai_*.txt_+_THIS>:/binsrc:ro" \
#     -v "<HOST_OUT_DIR>:/work" \
#     -e JOBS=6 -e OB_FRAMES=900 -e OB_ALARM=45 \
#     ubuntu:24.04 bash /binsrc/pak_blend_runtime_scan.sh
#
# Output: /work/blend_runtime.tsv   pak <TAB> exit <TAB> the raw [TBB] line
#         /work/logs/<pak>.log      per-PAK stderr (kept only if non-clean)
#
# NOTE the bot is generic: ai_menu_start.txt just pulses START every 20 frames
# and auto-stops the instant a level loads, so it is PAK-agnostic -- but it will
# NOT reach gameplay in every cart (branching menus, char-select, cutscenes).
# A PAK that never leaves its title still reports a valid histogram; it is just
# a title-screen one. Interpret coverage accordingly.
set -u

apt-get update -qq >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  libsdl2-2.0-0 libsdl2-gfx-1.0-0 libvpx9 libvorbisfile3 libpng16-16t64 libgl1 \
  >/dev/null 2>&1

mkdir -p /media/fat/logs/OpenBOR_7533 /media/fat/saves/OpenBOR_7533 \
         /media/fat/savestates/OpenBOR_7533 /media/fat/config
mkdir -p /work/logs
cp /binsrc/OpenBOR_headless /work/ob && chmod +x /work/ob
cp /binsrc/ai_menu_start.txt /binsrc/ai_lvl_moves.txt /work/ 2>/dev/null
cd /work

JOBS="${JOBS:-6}"
FRAMES="${OB_FRAMES:-900}"
ALARM="${OB_ALARM:-45}"
HARDTIMEOUT=$((ALARM + 240))
: > /work/blend_runtime.tsv

run_one() {
  pak="$1"
  rel="${pak#/paks/}"
  name="${rel%.pak}"
  safe="$(echo "$rel" | tr '/ ' '__' | tr -cd 'A-Za-z0-9._-')"
  log="/work/logs/${safe}.log"
  # Each PAK gets its OWN save/config dirs: the engine writes <pak>.cfg etc and
  # parallel jobs must not race on them.
  d="/tmp/j$$_$RANDOM"; mkdir -p "$d"
  HOME="$d" OB_PAK="$pak" OB_INPUT=/work/ai_menu_start.txt \
    OB_INPUT2=/work/ai_lvl_moves.txt OB_FRAMES="$FRAMES" OB_ALARM="$ALARM" \
    timeout "$HARDTIMEOUT" /work/ob > "$log" 2>&1
  ec=$?
  tbb="$(grep -a '^\[TBB\]' "$log" | tail -1)"
  printf '%s\t%s\t%s\n' "$name" "$ec" "${tbb:-NO_TBB_LINE}" >> /work/blend_runtime.tsv
  # keep logs only for the interesting cases
  [ "$ec" = "0" ] && [ -n "$tbb" ] && rm -f "$log"
  rm -rf "$d"
}
export -f run_one
export FRAMES ALARM HARDTIMEOUT

find /paks -iname '*.pak' | sort > /work/paklist.txt
total=$(wc -l < /work/paklist.txt)
echo "scanning $total PAKs, JOBS=$JOBS FRAMES=$FRAMES ALARM=$ALARM"
date -u +"start %H:%M:%SZ"

xargs -a /work/paklist.txt -d '\n' -I{} -P "$JOBS" \
      bash -c 'run_one "$@"' _ {}

date -u +"end   %H:%M:%SZ"
echo "SCAN DONE: $(wc -l < /work/blend_runtime.tsv) rows -> /work/blend_runtime.tsv"
echo "=== exit-code histogram ==="
cut -f2 /work/blend_runtime.tsv | sort | uniq -c | sort -rn
echo "=== rows with no [TBB] line (never reached the clean exit) ==="
awk -F'\t' '$3=="NO_TBB_LINE"' /work/blend_runtime.tsv | wc -l
