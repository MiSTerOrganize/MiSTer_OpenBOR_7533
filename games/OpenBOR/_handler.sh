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

# Auto-create ALL of the core's game folders so they always exist for every user
# (standing hybrid-core rule: the handler owns every folder — content + recordings
# + logs). Paks/ = PAK game modules (SC0 "Load PAK"); Replays/ = recorder .inp files
# (SC1 "Load Replay"). Both live under games/OpenBOR/ so the OSD file browser (which
# opens at the core's games/<setname>/ HomeDir) can reach them; NOT under saves/,
# which the browser can't reach. (Logs/ is created below, once $LOGDIR is known.)
mkdir -p "$GAMEDIR/Paks" "$GAMEDIR/Replays" 2>/dev/null

# Recorder save-isolation scratch. While a recording or replay is armed the
# engine resolves Saves and SaveStates here instead of the real dirs (see
# COPY_ROOT_PATH in source/utils.c), so both runs boot from identical
# persistent state -- otherwise a <pak>.sav written while recording is still
# present when the replay boots, and the two runs start in different worlds.
# Wiped on EVERY launch: Record and Play each reset the PAK through this
# handler, so wiping here is what guarantees the two starting states match.
# Cheap and safe for normal launches too, since nothing outside a session
# ever reads it. The user's real saves are never touched.
# Lives under saves/, NOT inside Replays/ and NOT under savestates/.
#   - Not in Replays/ because the OSD "Load Replay" picker lists directories as
#     well as files, so scratch and per-take snapshots showed up as decoy folders
#     that open EMPTY (they hold saves; the picker filters to .inp). Replays/ now
#     contains nothing but .inp takes.
#   - Under saves/ because that is what this data IS: .sav progress, .hi scores
#     and .sNN script-saves are all game state. OpenBOR has no emulator save
#     states at all -- "savestates" is only the directory name we chose.
REPSTATE="/media/fat/saves/OpenBOR_7533/.replays"
rm -rf "$REPSTATE/.scratch" 2>/dev/null
mkdir -p "$REPSTATE/.scratch/saves" "$REPSTATE/.scratch/savestates" 2>/dev/null

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
if [ -f /tmp/openbor_recmode ]; then
    _MODE=$(cat /tmp/openbor_recmode 2>/dev/null)
    _SCR="$REPSTATE/.scratch"
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
                echo "[REC] seeded scratch from your saves for: $_PAK"
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
            rm -rf "$REPSTATE/.armsnap" 2>/dev/null
            mkdir -p "$REPSTATE/.armsnap/saves" "$REPSTATE/.armsnap/savestates" 2>/dev/null
            cp -f "$_SCR/saves/"*       "$REPSTATE/.armsnap/saves/"       2>/dev/null
            cp -f "$_SCR/savestates/"*  "$REPSTATE/.armsnap/savestates/"  2>/dev/null
            ;;
        PLAY*)
            _INP=$(cat /tmp/openbor_playfile 2>/dev/null | tr -d '\0')
            # Snapshot is keyed by the take's BASENAME under $REPSTATE (it is no
            # longer a sidecar next to the .inp -- see the note above).
            _SNAP="$REPSTATE/$(basename "${_INP%.inp}")"
            if [ -n "$_INP" ] && [ -d "$_SNAP" ]; then
                cp -f "$_SNAP/saves/"* "$_SCR/saves/" 2>/dev/null
                cp -f "$_SNAP/savestates/"* "$_SCR/savestates/" 2>/dev/null
                # Count what LANDED. The old line printed "restored" whenever the
                # directory merely EXISTED -- an empty or unreadable snapshot, or a
                # cp that failed into 2>/dev/null, all reported success.
                _N=$(ls -A "$_SCR/saves" "$_SCR/savestates" 2>/dev/null | grep -vc '^$')
                if [ "${_N:-0}" -gt 0 ]; then
                    echo "[REC] restored $_N save file(s) for: $_INP"
                else
                    echo "[REC] snapshot for this take is empty or unreadable -- starting empty"
                fi
                # KNOWN LIMITATION, not fixed here. The snapshot is keyed on the
                # take's BASENAME, and take names are <content-id>_<N>.inp -- which
                # is identical on every machine for the same PAK. So a take copied
                # in from someone else whose name collides with one of yours will
                # restore YOUR snapshot and report it as that take's data. Only an
                # in-band payload fixes it (step 21 of #INP_SHARING_DESIGN.md);
                # until then a received take is not trustworthy on this path.
            else
                echo "[REC] no save snapshot for this take -- starting empty"
            fi
            ;;
    esac
fi

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
