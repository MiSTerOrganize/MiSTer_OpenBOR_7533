#!/bin/bash
# OpenBOR golden-trace gate, ONE SHARD (container entry point). Runs
# ob_golden_check.sh over this shard's PAK list, sequentially.
#
#   args: <shard list file> <shard id>
#   mounts: /paks (ro), /goldens (ro), /ob (ro: OpenBOR_headless + scripts), /work (rw)
#
# 🛑 One container per shard, never parallel workers in one container: the engine
# writes config and saves under a FIXED /media/fat, so two runs in one container
# would read each other's state -- the reason trace_shard.sh is sharded too.
set -u
SH="$1"; ID="$2"
apt-get update -qq >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  libsdl2-2.0-0 libsdl2-gfx-1.0-0 libvpx9 libvorbisfile3 libpng16-16t64 libgl1 \
  >/dev/null 2>&1
mkdir -p /media/fat/logs/OpenBOR_7533 /media/fat/saves/OpenBOR_7533 \
         /media/fat/savestates/OpenBOR_7533 /media/fat/config /work
cp /ob/OpenBOR_headless /tmp/ob && chmod +x /tmp/ob
export RES="/work/results_shard$ID.txt"; : > "$RES"
while IFS= read -r pak; do
    [ -n "$pak" ] || continue
    bash /ob/ob_golden_check.sh "$pak"
done < "$SH"
