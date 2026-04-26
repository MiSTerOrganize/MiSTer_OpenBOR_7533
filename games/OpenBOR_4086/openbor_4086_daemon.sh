#!/bin/bash
# openbor_daemon.sh — Auto-start OpenBOR engine when core loads
#
# Uses mkdir as atomic lock to guarantee only ONE daemon runs.
# Uses wait to guarantee only ONE binary runs at a time.
# No race conditions — process must fully exit before next spawn.

LOCKDIR="/tmp/openbor_daemon.lock"
PIDFILE="/tmp/openbor_arm.pid"
GAMEDIR="/media/fat/games/OpenBOR_4086"
BINARY="$GAMEDIR/OpenBOR"

# Delete stale .s0 at daemon startup so MiSTer doesn't
# auto-mount the previous PAK on core load
rm -f /media/fat/config/OpenBOR_4086.s0

# Prevent multiple daemon instances
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    OLDPID=$(cat "$LOCKDIR/pid" 2>/dev/null)
    if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
        exit 0
    fi
    rm -rf "$LOCKDIR"
    mkdir "$LOCKDIR" 2>/dev/null || exit 0
fi
echo $$ > "$LOCKDIR/pid"

CHILD=""
cleanup() {
    [ -n "$CHILD" ] && kill $CHILD 2>/dev/null
    rm -f "$PIDFILE"
    rm -rf "$LOCKDIR"
    exit 0
}
trap cleanup TERM INT

FIRST_LOAD=1
while true; do
    CUR=$(cat /tmp/CORENAME 2>/dev/null)

    if [ "$CUR" = "OpenBOR_4086" ] && [ -z "$CHILD" ]; then
        # No binary running — start one
        if [ "$FIRST_LOAD" = "1" ]; then
            # Clear stale .s0 so MiSTer doesn't auto-mount previous PAK.
            # Survives reboots since .s0 is on SD, not /tmp.
            rm -f /media/fat/config/OpenBOR_4086.s0
            sleep 1  # FPGA settle on first load only
            FIRST_LOAD=0
        fi
        export SDL_VIDEODRIVER=dummy
        cd "$GAMEDIR"
        # Free kernel buffer cache before starting — FC0 ioctl streams
        # the entire PAK (50-150MB) through SPI, filling the cache.
        # Without this, repeated PAK loads exhaust RAM and OpenBOR segfaults.
        echo 3 > /proc/sys/vm/drop_caches 2>/dev/null
        mkdir -p /media/fat/logs/OpenBOR_4086
        mv -f /media/fat/logs/OpenBOR_4086/OpenBOR.log /media/fat/logs/OpenBOR_4086/OpenBOR.prev.log 2>/dev/null
        ./OpenBOR > /media/fat/logs/OpenBOR_4086/OpenBOR.log 2>&1 &
        CHILD=$!
        echo $CHILD > "$PIDFILE"
    fi

    if [ -n "$CHILD" ]; then
        if ! kill -0 $CHILD 2>/dev/null; then
            # Process exited (quit, reset pak, or crash) — reap it
            wait $CHILD
            EXIT_CODE=$?
            echo "OpenBOR exited with code $EXIT_CODE at $(date)" >> /media/fat/logs/OpenBOR_4086/OpenBOR.log
            CHILD=""
            rm -f "$PIDFILE"
            # Don't sleep — restart fast on next iteration
            continue
        fi
        if [ "$CUR" != "OpenBOR_4086" ]; then
            # User left the core -- kill binary and clear cached state
            # so the next entry goes through MiSTer's OSD picker instead
            # of auto-loading the previous PAK.
            kill $CHILD 2>/dev/null
            wait $CHILD 2>/dev/null
            CHILD=""
            FIRST_LOAD=1
            rm -f "$PIDFILE"
            rm -f /tmp/openbor_current.pak
            # Delete .s0 so MiSTer doesn't auto-mount the previous PAK.
            # Keep .cfg (user's OSD video settings like scanlines).
            rm -f /media/fat/config/OpenBOR_4086.s0
        fi
    fi

    sleep 1
done
