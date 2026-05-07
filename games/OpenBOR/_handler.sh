#!/bin/bash
#
# Unified OpenBOR handler — invoked by Master_Daemon when EITHER the
# OpenBOR_4086 or OpenBOR_7533 RBF loads (both share setname "OpenBOR").
#
# Dispatch by reading MiSTer Main's argv[1] (the RBF path) from
# /proc/$pid/cmdline. /tmp/RBFNAME and /tmp/CORENAME both contain the
# setname (not the RBF filename) so they cannot distinguish 4086 from 7533.
# Master_Daemon owns the lifecycle.

GAMEDIR="/media/fat/games/OpenBOR"
LOGDIR="/media/fat/logs/OpenBOR"

cd "$GAMEDIR" || exit 1

# Read MiSTer Main's argv to find the loaded RBF filename.
# `pidof MiSTer` may return multiple PIDs (older lingering shells); take
# the one whose argv contains an .rbf path.
MISTER_RBF=""
for pid in $(pidof MiSTer 2>/dev/null); do
    cand=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null | grep -E '\.rbf$' | head -1)
    if [ -n "$cand" ]; then
        MISTER_RBF="$cand"
        break
    fi
done

case "$MISTER_RBF" in
    *4086*)
        BUILD=4086
        BINARY="OpenBOR_4086"
        ;;
    *7533*)
        BUILD=7533
        BINARY="OpenBOR_7533"
        ;;
    *)
        echo "OpenBOR handler: unrecognized RBF '$MISTER_RBF' — defaulting to 7533" >&2
        BUILD=7533
        BINARY="OpenBOR_7533"
        ;;
esac

mkdir -p "$LOGDIR" Logs

# Rotate ARM-binary log
mv -f "$LOGDIR/OpenBOR.log" "$LOGDIR/OpenBOR.prev.log" 2>/dev/null

# Preserve OpenBOR's internal engine log across restart loops
# (truncated on every launch in 'wt' mode by the engine itself).
# Keeps one prev + timestamped copy of any non-empty current log.
if [ -s Logs/OpenBorLog.txt ]; then
    cp -f Logs/OpenBorLog.txt "Logs/OpenBorLog.$(date +%H%M%S).txt" 2>/dev/null
fi
mv -f Logs/OpenBorLog.txt   Logs/OpenBorLog.prev.txt   2>/dev/null
mv -f Logs/ScriptLog.txt    Logs/ScriptLog.prev.txt    2>/dev/null

# Free kernel page cache — FC0 PAK streaming exhausts RAM otherwise.
# OpenBOR segfaults on repeated PAK loads without this.
echo 3 > /proc/sys/vm/drop_caches 2>/dev/null

# SDL environment differs per build:
#   4086 → SDL 1.2.15 with custom dummy video driver
#   7533 → SDL 2.0.8 with patched dummy framebuffer + software renderer
export SDL_VIDEODRIVER=dummy
if [ "$BUILD" = "7533" ]; then
    # SDL2 dummy driver registers no render driver, so SDL_CreateRenderer
    # fails silently — force software renderer explicitly.
    export SDL_AUDIODRIVER=dummy
    export SDL_RENDER_DRIVER=software
fi

# FPGA settle on first launch
sleep 1

echo "OpenBOR handler: dispatching to $BINARY (RBF=$MISTER_RBF)" > "$LOGDIR/OpenBOR.log"
exec ./"$BINARY" >> "$LOGDIR/OpenBOR.log" 2>&1
