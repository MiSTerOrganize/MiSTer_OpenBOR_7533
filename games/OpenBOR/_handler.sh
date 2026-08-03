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

# Auto-create ALL of the core's folders so they always exist for every user
# (standing hybrid-core rule: the handler owns every folder — content +
# recordings + logs). Paks/ = PAK game modules (SC0 "Load PAK").
mkdir -p "$GAMEDIR/Paks" 2>/dev/null

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

# $BINARY is resolved ABOVE the recorder block on purpose, so $REPLAYS is
# genuinely per-build. It used to be a hardcoded OpenBOR_7533 literal set
# before the dispatch ran, which meant launching 4086 created and scrubbed a
# 7533 tree -- harmless in practice (4086 has no recorder and the scratch is
# per-session) but it contradicted the per-build rule the rest of the layout
# follows.

# EVERYTHING the recorder owns lives here: takes plus the hidden working dirs.
# Top-level and per-build, matching saves/, savestates/, config/ and logs/ --
# there was never a principled reason for replays alone to sit under games/.
#
# It moved out of saves/ (2026-08-02) because the scratch used to be a hidden dir
# INSIDE the real saves directory, so any careless glob over saves/OpenBOR_7533/*
# would have destroyed a user's .sav progress. That hazard had to be worked
# around with defensive checks; now it simply does not exist. Nothing of the
# user's is under this tree, so rm -rf on it is always safe.
#
# The per-take snapshots and scratch are DOT-directories so the OSD "Load Replay"
# picker never lists them -- that was the original reason for keeping them out of
# the takes folder, and hiding them solves it without a second location.
#
# 🛑 mkdir -p on EVERY launch is load-bearing, not tidiness: MiSTer's browser
# starts at the core's games/ home, and Selected_S[] only remembers a different
# directory while that directory still EXISTS. If this vanished, "Load Replay"
# would silently snap back to games/OpenBOR/ and the user's takes would look gone.
REPLAYS="/media/fat/replays/$BINARY"
REPSTATE="$REPLAYS/.snapshots"
mkdir -p "$REPLAYS" "$REPSTATE" 2>/dev/null

# Retire the pre-2026-08-02 location. rmdir (not rm -rf) is the whole point: it
# refuses on a non-empty directory, so anyone holding takes in there keeps them
# exactly where they are -- still playable via the OSD "Load Replay", which
# browses anywhere. Only the empty folder our old handler created goes away,
# rather than lingering in the PAK browser as a decoy.
rmdir "$GAMEDIR/Replays" 2>/dev/null

# Recorder save-isolation scratch. While a recording or replay is armed the
# engine resolves Saves and SaveStates here instead of the real dirs (see
# COPY_ROOT_PATH in source/utils.c), so both runs boot from identical
# persistent state -- otherwise a <pak>.sav written while recording is still
# present when the replay boots, and the two runs start in different worlds.
# Wiped on EVERY launch: Record and Play each reset the PAK through this
# handler, so wiping here is what guarantees the two starting states match.
# Cheap and safe for normal launches too, since nothing outside a session
# ever reads it. The user's real saves are never touched.

# Keep only the 20 newest per-take snapshots. Nothing pruned these before, so
# they accumulated one directory per Stop Recording, forever, under saves/.
# Everything else in this project auto-prunes (see the log rotation below).
ls -dt "$REPSTATE"/*.* 2>/dev/null | tail -n +21 | xargs -r rm -rf
rm -rf "$REPLAYS/.scratch" 2>/dev/null
mkdir -p "$REPLAYS/.scratch/saves" "$REPLAYS/.scratch/savestates" 2>/dev/null

# Seed the scratch so a session can start from YOUR progress.
#
# Isolating to EMPTY made recordings deterministic but also made it impossible
# to record from anywhere except a fresh game -- Record resets to the title,
# and with nothing to continue from you could only ever capture a run from the
# very beginning. So:
#
#   REC  -> copy this PAK's real save/high-score/savestate in. You boot with
#           your progress and load it through the PAK's own menus, and that
#           navigation is recorded (takes are title-anchored).
#   PLAY -> restore the snapshot stored WITH the take instead, so the replay
#           boots against exactly the data the recording started from. Using
#           current saves would change the LOAD GAME menu's shape and send the
#           recorded D-pad presses to a different slot.
#
# This runs in the handler, not the engine, because loadGameFile() happens
# during startup -- far too early for the recorder itself to do it.
# Real saves are only ever READ.
# These echoes used to go to the handler's inherited stdout, which nothing
# captures -- and they run long before $LOGDIR exists, so even redirecting would
# have been erased by the truncating redirect further down. The recorder is
# 7533-only, so the path is known here.
_RECLOG="/media/fat/logs/OpenBOR_7533/OpenBOR.log"
mkdir -p /media/fat/logs/OpenBOR_7533 2>/dev/null
if [ -f /tmp/openbor_recmode ]; then
    _MODE=$(cat /tmp/openbor_recmode 2>/dev/null)
    _SCR="$REPLAYS/.scratch"
    case "$_MODE" in
        REC*)
            _S0=$(cat /media/fat/config/OpenBOR.s0 2>/dev/null | tr -d '\0')
            _PAK=$(basename "${_S0%.pak}" 2>/dev/null)
            if [ -n "$_PAK" ]; then
                cp -f "/media/fat/saves/OpenBOR_7533/$_PAK.sav"      "$_SCR/saves/"       2>/dev/null
                cp -f "/media/fat/config/$_PAK.hi"                   "$_SCR/saves/"       2>/dev/null
                # SCRIPT-SAVE data, not emulator save states -- OpenBOR has none.
                # saveScriptFile() emits re-executable OpenBOR script here
                # (setglobalvar / changemodelproperty), which is how mods persist
                # unlocked characters and progression, so it is real game state.
                # "SaveStates" is only the MiSTer-side directory name we chose.
                #
                # Filename: getPakName builds "<pak>.scr", then saveScriptFile
                # OVERWRITES the last two chars with the level-set number --
                # ".scr" -> ".s00", ".s01", ... one file PER SET. So glob it:
                # matching only .s00 would miss every later set on the big
                # multi-set PAKs, which is exactly where the progress lives.
                cp -f "/media/fat/savestates/OpenBOR_7533/$_PAK".s[0-9][0-9] "$_SCR/savestates/" 2>/dev/null
                cp -f "/media/fat/savestates/OpenBOR_7533/$_PAK".scr        "$_SCR/savestates/" 2>/dev/null
                echo "[REC] seeded scratch from your saves for: $_PAK" >> "$_RECLOG"
            fi
            # Keep a PRISTINE copy of exactly what the run is about to boot with.
            #
            # The take must be paired with the state the run STARTED from. The
            # scratch is written INTO all session long -- a PAK that autosaves on
            # stage clear rewrites .sav, a qualifying score rewrites .hi -- so
            # snapshotting the scratch at Stop pairs the take with POST-run saves.
            # The replay then boots against a LOAD GAME menu of a different shape
            # and the recorded navigation selects a different slot: exactly the
            # desync the snapshot exists to prevent.
            #
            # PICO-8 has always kept this distinction (P8REC_ARMSNAP vs the live
            # scratch). This is OpenBOR catching up.
            rm -rf "$REPLAYS/.armsnap" 2>/dev/null
            mkdir -p "$REPLAYS/.armsnap/saves" "$REPLAYS/.armsnap/savestates" 2>/dev/null
            cp -f "$_SCR/saves/"*       "$REPLAYS/.armsnap/saves/"       2>/dev/null
            cp -f "$_SCR/savestates/"*  "$REPLAYS/.armsnap/savestates/"  2>/dev/null
            ;;
        PLAY*)
            _INP=$(cat /tmp/openbor_playfile 2>/dev/null | tr -d '\0')
            # Keyed on the take's SEED, read from the .inp header, NOT on its
            # name. Take names are <content-id>_<N>.inp, identical on every
            # machine for the same PAK, so a received take whose name collided
            # with one of yours used to restore YOUR snapshot and report it as
            # that take's data -- wrong save state, asserted as correct.
            #
            # Header: magic 4 + container 4 + engine_ver 4 + build_date 12 +
            # pak 256 = seed at byte 280, 8 bytes. The engine names the snapshot
            # with the same bytes in the same order, so this matches by
            # construction. A received take finds no local snapshot and says so,
            # which is the honest answer.
            #
            # This said 277 and "magic 5" until 2026-08-02. The magic shrank
            # 5 -> 4 without this moving, so it read three bytes early, every
            # snapshot name mismatched, and PLAY always reported "no save
            # snapshot for this take" -- for every take, on every PAK. If a
            # header field is ever added or resized, MREC_OFF_SEED in
            # apply_patches.py moves and THIS NUMBER MUST MOVE WITH IT.
            _SEED=$(dd if="$_INP" bs=1 skip=280 count=8 2>/dev/null | od -An -tx1 | tr -d ' \n')
            _SNAP="$REPSTATE/$(basename "${_INP%.inp}").$_SEED"
            # A SHARED take carries its saves inside the .inp; a locally-recorded
            # one also has a seed-keyed snapshot dir beside it. Try the embedded
            # payload FIRST -- it is the only source that travels between
            # machines, and for a local take it holds the same bytes.
            #
            # Parsing stays in the binary (--extract-snap), not here. Reaching
            # into the container with dd is how the seed offset silently went
            # stale, and the extension whitelist that decides which files are
            # allowed to land is a security control, not something to express in
            # shell quoting.
            _EMB=0
            if [ -n "$_INP" ] && [ -f "$_INP" ]; then
                # $_PAK is the locally-loaded PAK's stem: entries are renamed to
                # it, because the engine derives .sav/.hi/.sNN from getPakName and
                # would not see a payload still named for the recorder's copy.
                if ./"$BINARY" --extract-snap "$_INP" "$_SCR/saves" "$_PAK" >> "$_RECLOG" 2>&1; then
                    _EMB=$(ls -A "$_SCR/saves" 2>/dev/null | grep -vc '^$')
                fi
                [ "${_EMB:-0}" -gt 0 ] && echo "[REC] restored $_EMB save file(s) from inside: $_INP" >> "$_RECLOG"
            fi
            if [ "${_EMB:-0}" -gt 0 ]; then
                :   # embedded payload won; leave the local snapshot alone
            elif [ -n "$_INP" ] && [ -d "$_SNAP" ]; then
                cp -f "$_SNAP/saves/"* "$_SCR/saves/" 2>/dev/null
                cp -f "$_SNAP/savestates/"* "$_SCR/savestates/" 2>/dev/null
                # Count what LANDED. The old line printed "restored" whenever the
                # directory merely EXISTED -- an empty or unreadable snapshot, or a
                # cp that failed into 2>/dev/null, all reported success.
                _N=$(ls -A "$_SCR/saves" "$_SCR/savestates" 2>/dev/null | grep -vc '^$')
                if [ "${_N:-0}" -gt 0 ]; then
                    echo "[REC] restored $_N save file(s) for: $_INP" >> "$_RECLOG"
                else
                    echo "[REC] snapshot for this take is empty or unreadable -- starting empty" >> "$_RECLOG"
                fi
            else
                echo "[REC] no save snapshot for this take -- starting empty" >> "$_RECLOG"
            fi
            ;;
    esac
fi


# Per-build log directory (matches per-build saves/savestates/replays).
# $BINARY was resolved near the top of this script, above the recorder block.
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

# NOTE: .s1 is intentionally NOT cleared here. The binary detects a replay pick
# by .s1's MTIME (baselined at startup) — every OSD "Load Replay" pick bumps the
# mtime, even re-picking the same .inp, so a fresh pick triggers while a stale/
# unchanged .s1 never auto-replays. Clearing .s1 would risk a clear-then-restore
# false trigger, so we leave it as a persistent "last replay" marker.

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

# Affinity: allow BOTH cores (mask 0x03 = cores 0+1). The process mask must
# cover both cores or the binary's own thread pins fail with EINVAL (a child
# thread cannot widen affinity past the process mask — the original 0x02 mask
# silently broke thread pinning). WHICH thread goes on WHICH core is decided
# inside each build's binary (native_video_writer.c render pin +
# sblaster_patch.c audio pin), not here — this shared handler only opens
# both cores so those pins take effect.
exec taskset 0x03 ./"$BINARY" >> "$LOGDIR/OpenBOR.log" 2>&1
