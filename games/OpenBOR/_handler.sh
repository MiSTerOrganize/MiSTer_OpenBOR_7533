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
# LOGDIR is per-build, set after the dispatch case below — matches the
# saves/savestates per-build pattern. Prevents cross-build log mixing
# when both binaries dispatch under the unified "OpenBOR" setname.

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

# Per-build log directory (matches per-build saves/savestates pattern).
LOGDIR="/media/fat/logs/$BINARY"
mkdir -p "$LOGDIR"

# Rotate ARM-binary log
mv -f "$LOGDIR/OpenBOR.log" "$LOGDIR/OpenBOR.prev.log" 2>/dev/null

# Preserve OpenBOR's engine logs across restart loops. The engine writes
# to /media/fat/logs/$BINARY/{OpenBorLog,ScriptLog}.txt in "wt" mode
# (truncate on open) thanks to the apply_patches.py absolute-path patch
# — per-build path matches the saves/savestates pattern.
# Keep one .prev + timestamped copy of any non-empty current log.
if [ -s "$LOGDIR/OpenBorLog.txt" ]; then
    cp -f "$LOGDIR/OpenBorLog.txt" "$LOGDIR/OpenBorLog.$(date +%H%M%S).txt" 2>/dev/null
fi
mv -f "$LOGDIR/OpenBorLog.txt"   "$LOGDIR/OpenBorLog.prev.txt"   2>/dev/null
mv -f "$LOGDIR/ScriptLog.txt"    "$LOGDIR/ScriptLog.prev.txt"    2>/dev/null

# Auto-prune: keep only the 10 newest timestamped OpenBorLog archives.
# Per CLAUDE.md "hybrid-core handlers must auto-prune log history" —
# without this, /media/fat/logs/$BINARY/ accumulates one timestamped
# copy per launch and grows unbounded over months of use.
ls -t "$LOGDIR"/OpenBorLog.[0-9]*.txt 2>/dev/null | tail -n +11 | xargs -r rm -f
ls -t "$LOGDIR"/ScriptLog.[0-9]*.txt  2>/dev/null | tail -n +11 | xargs -r rm -f

# Belt-and-suspenders .s0 cleanup — Master_Daemon already clears .s0 on
# core transitions, but sister-core swaps (4086 ↔ 7533 share setname
# "OpenBOR") and MiSTer Main's auto-resume-last-file behavior can leave
# OpenBOR.s0 populated when handler spawns. Clearing here at handler
# start (before binary launches, before MGL's 2-second timer) gives
# users a clean "go to OSD picker" experience on every entry, while
# still allowing MGL to write .s0 in its window before binary polls.
#
# EXCEPTIONS — preserve .s0 when either marker is present:
#   /tmp/openbor_reset_marker — pause-menu Reset Pak (engine wrote it
#       in pausemenu_patch.c case 2). Reset needs .s0 PRESERVED so the
#       binary re-mounts the same PAK fresh from .s0. (2026-05-17 fix.)
#   /tmp/openbor_hotswap_marker — mid-gameplay PAK hot-swap from OSD
#       (engine wrote it in sdlport_patch.c::mister_swap_thread). The
#       freshly-written .s0 holds the NEW PAK path the user just picked;
#       deleting it would force a second OSD pick. (2026-05-18 fix.)
# Without these exceptions, the else-branch wipes .s0 → binary respawns
# into wait-for-OSD-pick → black screen until user picks again.
if [ -f /tmp/openbor_reset_marker ] || [ -f /tmp/openbor_hotswap_marker ]; then
    rm -f /tmp/openbor_reset_marker /tmp/openbor_hotswap_marker 2>/dev/null
else
    rm -f /media/fat/config/OpenBOR.s0 2>/dev/null
fi

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
