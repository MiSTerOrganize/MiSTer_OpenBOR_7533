#!/bin/sh
# Scan every 324-byte OpenBOR savedata .cfg for the fields that select
# getVideoSurface's bscreen branch.  Offsets from engine/source/savedata.h
# (arm32, all members 4-byte): swfilter@272, fullscreen@288, hwscale@316.
n=0
live=0
nz=0
for f in /media/fat/config/*.cfg; do
    [ -f "$f" ] || continue
    sz=$(wc -c < "$f")
    [ "$sz" = "324" ] || continue
    n=$((n + 1))
    sw=$(od -An -tu4 -j272 -N4 "$f" | tr -dc '0-9')
    fs=$(od -An -tu4 -j288 -N4 "$f" | tr -dc '0-9')
    if [ "$sw" != "0" ]; then
        nz=$((nz + 1))
        echo "SWFILTER-SET  $f  swfilter=$sw fullscreen=$fs"
        if [ "$fs" != "0" ]; then
            live=$((live + 1))
            echo "  ^^ BSCREEN LIVE for this PAK"
        fi
    fi
done
echo "----"
echo "324-byte OpenBOR configs scanned : $n"
echo "with swfilter != 0               : $nz"
echo "with bscreen actually LIVE       : $live"
