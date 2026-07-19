#!/bin/bash
# Oracle differential: run each PAK through OUR build and the STOCK-engine
# oracle build under identical OB_TEST conditions, diff the traces.
# DIFF = our engine-logic patches change this PAK's boot behavior
# (expected for the deliberate fixes; unexpected entries = bugs in our
# patches). SAME = our patches are behavior-neutral for this PAK's boot.
set -u
apt-get update -qq >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  libsdl2-2.0-0 libsdl2-gfx-1.0-0 libvpx9 libvorbisfile3 libpng16-16t64 libgl1 \
  >/dev/null 2>&1
mkdir -p /media/fat/logs/OpenBOR_7533 /media/fat/saves/OpenBOR_7533 \
         /media/fat/savestates/OpenBOR_7533 /media/fat/config
cp /ours/OpenBOR_headless /tmp/ob_ours && chmod +x /tmp/ob_ours
cp /oracle/OpenBOR_headless /tmp/ob_oracle && chmod +x /tmp/ob_oracle
wipe() { rm -rf /media/fat/config/* /media/fat/saves/OpenBOR_7533/* /media/fat/savestates/OpenBOR_7533/* 2>/dev/null; }
for pak in "A Tale of Vengeance" "Teenage Mutant Ninja Turtles - Rescue Palooza!" "He-Man and the Masters of the Universe" "A Saga de Ryu"; do
  p="/paks/$pak.pak"
  [ -f "$p" ] || { echo "$pak: MISSING"; continue; }
  wipe
  OB_PAK="$p" OB_FRAMES=200 OB_ALARM=30 OB_TEST=/tmp/t_ours.txt OB_TESTFRAMES=120 \
    timeout 90 /tmp/ob_ours >/dev/null 2>&1; ea=$?
  wipe
  OB_PAK="$p" OB_FRAMES=200 OB_ALARM=30 OB_TEST=/tmp/t_oracle.txt OB_TESTFRAMES=120 \
    timeout 90 /tmp/ob_oracle >/dev/null 2>&1; eb=$?
  if cmp -s /tmp/t_ours.txt /tmp/t_oracle.txt; then
    echo "SAME  $pak (ec $ea/$eb)"
  else
    fv=$(paste -d' ' /tmp/t_ours.txt /tmp/t_oracle.txt | awk '{split($1,x,":");split($2,y,":"); if(x[2]!=y[2]){print x[1]; exit}}')
    fa=$(paste -d' ' /tmp/t_ours.txt /tmp/t_oracle.txt | awk '{split($1,x,":");split($2,y,":"); if(x[3]!=y[3]){print x[1]; exit}}')
    echo "DIFF  $pak (ec $ea/$eb, first video diff @${fv:-never}, first audio diff @${fa:-never})"
  fi
done
