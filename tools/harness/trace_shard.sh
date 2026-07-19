#!/bin/bash
# OpenBOR golden-trace shard worker. One container per shard so parallel
# engine-log/config writes under /media/fat can't collide across instances.
#   args: <shard list file> <shard id>
set -u
SH="$1"; ID="$2"
apt-get update -qq >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  libsdl2-2.0-0 libsdl2-gfx-1.0-0 libvpx9 libvorbisfile3 libpng16-16t64 libgl1 \
  >/dev/null 2>&1
mkdir -p /media/fat/logs/OpenBOR_7533 /media/fat/saves/OpenBOR_7533 \
         /media/fat/savestates/OpenBOR_7533 /media/fat/config
cp /binsrc/OpenBOR_headless /tmp/ob && chmod +x /tmp/ob
mkdir -p /work/goldens
RES="/work/results_shard$ID.txt"; : > "$RES"
wipe() {
  # both runs start from pristine engine state (no config/save carry-over)
  rm -rf /media/fat/config/* /media/fat/saves/OpenBOR_7533/* \
         /media/fat/savestates/OpenBOR_7533/* 2>/dev/null
}
while IFS= read -r pak; do
  [ -n "$pak" ] || continue
  base="$(basename "$pak" .pak)"
  safe="$(echo "$base" | tr '/ ' '__' | tr -cd 'A-Za-z0-9._-')"
  ga="/work/goldens/$safe.trace"; tb="/tmp/tb_$ID.trace"
  wipe
  OB_PAK="$pak" OB_FRAMES=200 OB_ALARM=25 OB_TEST="$ga" OB_TESTFRAMES=120 \
    timeout 60 /tmp/ob >/dev/null 2>&1; eca=$?
  wipe
  OB_PAK="$pak" OB_FRAMES=200 OB_ALARM=25 OB_TEST="$tb" OB_TESTFRAMES=120 \
    timeout 60 /tmp/ob >/dev/null 2>&1; ecb=$?
  if   [ $eca -eq 124 ] || [ $ecb -eq 124 ] || [ $eca -eq 98 ] || [ $ecb -eq 98 ]; then cls=HANG
  elif [ $eca -ne 0 ]  || [ $ecb -ne 0 ];  then cls="FAIL$eca"
  elif cmp -s "$ga" "$tb"; then cls=DET
  else cls=NONDET
  fi
  lines=0; [ -f "$ga" ] && lines=$(wc -l < "$ga")
  echo "$cls|$eca|$ecb|$lines|$(basename "$pak")" >> "$RES"
  case "$cls" in DET|NONDET) ;; *) rm -f "$ga" ;; esac
done < "$SH"
echo "shard $ID done: $(wc -l < "$RES") paks"
