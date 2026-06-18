#!/bin/bash
# OpenBOR diff-harness — CRASH/HANG mass-scan (runs INSIDE a glibc container).
#
# OpenBOR_headless is dynamic glibc+SDL2 (unlike PICO-8's fully-static
# z8headless that runs on the musl WSL), so the scan runs in an ubuntu:24.04
# container matching the diff_harness.yml build env. Host launcher:
#
#   docker run --rm \
#     -v "<PAKS_DIR>:/paks:ro" \
#     -v "<DIR_WITH_OpenBOR_headless_AND_THIS_SCRIPT>:/binsrc:ro" \
#     -v "<HOST_OUT_DIR>:/work" \
#     ubuntu:24.04 bash /binsrc/pak_run_scan.sh
#
# Runs OpenBOR_headless over every PAK in /paks, classifying by exit code:
#   0     = clean (ran OB_FRAMES frames then exited)
#   139   = CRASH (SIGSEGV/BUS/ABRT/FPE — backtrace in the per-PAK log; resolve
#                  offsets with addr2line -e OpenBOR.elf <addr>)
#   98    = HANG  (SIGALRM: no frame within OB_ALARM s — backtrace in the log)
#   124   = HANG  (outer timeout tripped before SIGALRM could dump)
#   other = load/other failure (see per-PAK log)
# Output: /work/scan_results.txt  (idx|exitcode|relpath)  + /work/logs/<pak>.log
set -u
apt-get update -qq >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  libsdl2-2.0-0 libsdl2-gfx-1.0-0 libvpx9 libvorbisfile3 libpng16-16t64 libgl1 \
  >/dev/null 2>&1
mkdir -p /work/logs
# The shipped engine-logic patches redirect logs/saves to /media/fat/... (MiSTer
# paths). Create them in the container so the engine can write + doesn't bail on
# missing save/config dirs (the diff harness is x86, no real /media/fat).
mkdir -p /media/fat/logs/OpenBOR_7533 /media/fat/saves/OpenBOR_7533 \
         /media/fat/savestates/OpenBOR_7533 /media/fat/config
cp /binsrc/OpenBOR_headless /work/ob && chmod +x /work/ob
cd /work
: > /work/scan_results.txt
FRAMES="${OB_FRAMES:-120}"
ALARM="${OB_ALARM:-25}"
i=0
find /paks -iname '*.pak' | sort | while IFS= read -r pak; do
  i=$((i+1))
  rel="${pak#/paks/}"
  safe="$(echo "$rel" | tr '/ ' '__' | tr -cd 'A-Za-z0-9._-')"
  OB_PAK="$pak" OB_FRAMES="$FRAMES" OB_ALARM="$ALARM" \
    timeout $((ALARM + 20)) ./ob > "/work/logs/${safe}.log" 2>&1
  ec=$?
  echo "${i}|${ec}|${rel}" >> /work/scan_results.txt
  [ $((i % 50)) -eq 0 ] && echo "...scanned $i"
done
echo "SCAN DONE: $(wc -l < /work/scan_results.txt) PAKs -> /work/scan_results.txt"
echo "=== exit-code histogram (0=clean 139=CRASH 98/124=HANG other=fail) ==="
cut -d'|' -f2 /work/scan_results.txt | sort | uniq -c
echo "=== non-clean PAKs ==="
awk -F'|' '$2!=0 {print "  ec="$2"  "$3}' /work/scan_results.txt | head -60
