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
        # ASCII only: this is the handler's one user-visible RUNTIME string (it
        # lands in the daemon log / console). The em-dash that used to be here
        # rendered as "â€”" once it crossed into a non-UTF-8 console. Comments
        # elsewhere in this file can keep theirs -- they are never emitted.
        echo "OpenBOR handler: unrecognized RBF '$MISTER_RBF' -- defaulting to 7533" >&2
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
#
# 🛑 ...but only for a build that HAS a recorder. 4086 never got one (deliberate
# divergence -- it is archival), so creating its tree put an empty
# replays/OpenBOR_4086/ on the card at every 4086 launch, advertising a feature
# that build does not have. The reasoning above is about not losing a browse
# directory the user relies on; 4086 has no takes to browse.
REPLAYS="/media/fat/replays/$BINARY"
# ONE flag, tested at every site that creates something under $REPLAYS. Keeping
# it a variable rather than repeating the test is what let the 4086 guard cover
# the class instead of the instances -- its first version wrapped only the
# .snapshots mkdir and was defeated three lines later by the .scratch one.
HAS_RECORDER=1
[ "$BINARY" = "OpenBOR_4086" ] && HAS_RECORDER=0
# No .snapshots at all. The sidecar is gone from the engine, and the fallback
# that read it was unreachable for every take that can exist: a pre-payload
# take is container v1 and the reader rejects it at `_mr_cv==2u` before any of
# this runs, while a v2 take always carries its payload -- v2 and the payload
# shipped in the same binary, with no build between them. The .inp is the whole
# take, exactly as on PICO-8.
if [ "$HAS_RECORDER" = 1 ]; then
    mkdir -p "$REPLAYS" 2>/dev/null
fi

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

# The keep-20 snapshot prune is gone with the store it pruned. The LOG prune
# further down still uses this idiom, and the reasoning is recorded there.
if [ "$HAS_RECORDER" = 1 ]; then
    rm -rf "$REPLAYS/.scratch" 2>/dev/null
    mkdir -p "$REPLAYS/.scratch/saves" "$REPLAYS/.scratch/savestates" 2>/dev/null
fi

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
# These echoes describe THIS session, so they belong in this session's log --
# but they run before $LOGDIR exists and before the truncating redirect that
# creates it, so they cannot be written there directly. They used to be aimed
# straight at OpenBOR.log, where the rotation further down swept them into
# .prev.log: preserved, but filed under the PREVIOUS run. Buffer them in tmp
# instead and splice them into the real log right after it is created.
_RECLOG="/tmp/openbor_recboot.log"
: > "$_RECLOG" 2>/dev/null
# A recmode marker with NO reset marker beside it was never asked for.
#
# Every writer of recmode -- Record, Play, Reset-while-recording, and the OSD
# .s1 pick -- writes /tmp/openbor_reset_marker in the same breath, then exits so
# the daemon respawns us. But recmode is consumed by the BINARY, much later:
# not until the first inputrefresh, i.e. after the PAK has finished loading,
# which is 12-90 s away. Kill the process in that window -- a core switch, a
# reboot, a crash -- and recmode outlives it.
#
# The next time ANY content is launched, the recorder then armed unrequested,
# and worse: mrec_isolate is raised from this same marker at openborMain entry,
# so that whole session's .sav/.hi/.sNN resolved into .scratch -- which this
# script rm -rf's on the following launch. Silent loss of REAL progress.
#
# The reset marker is still present here (it is consumed further down, after
# this block), so its absence is positive evidence of a leftover.
#
# 🛑 The trade is deliberate: if a transitional handler spawn is killed AFTER
# consuming the reset marker but before the binary reads recmode, this discards
# a legitimate arm -- Record appears not to start, the user presses it again.
# That is visible and recoverable. Silently isolating and then wiping someone's
# save files is neither. Prefer the loud failure.
if [ -f /tmp/openbor_recmode ] && [ ! -f /tmp/openbor_reset_marker ]; then
    echo "[REC] stale recmode marker with no reset marker -- discarding, not arming" >> "$_RECLOG"
    rm -f /tmp/openbor_recmode /tmp/openbor_recslot /tmp/openbor_playfile 2>/dev/null
fi

# ── Stale recwarn ────────────────────────────────────────────────────
# recwarn gets its OWN rule, keyed on the markers rather than on recmode.
#
# It is the only one of these that produces a USER-VISIBLE message, and the
# engine consumes it unconditionally at openborMain entry, so a leftover is
# shown on the next launch of ANY Pak with no take involved.
#
# Keying it on recmode did not cover it. The branch below that fails to prepare
# the saves writes recwarn and DELETES recmode -- deliberately, so the recorder
# does not arm -- which makes the recmode-based guard structurally unable to
# ever see it. Killed anywhere between that write and the exec (the settle sleep
# is in that window) it orphaned permanently.
#
# The right key is the marker, because a recwarn that is legitimately WAITING
# for this launch always arrives with one:
#   reset   -> pausemenu writes recwarn, then /tmp/openbor_reset_marker
#   hotswap -> the swap thread writes /tmp/openbor_hotswap_marker (sdlport:160)
#              BEFORE its recwarn (sdlport:203)
# Neither marker present means nothing legitimate is pending, so anything here
# is a leftover.
#
# 🛑 Must stay ABOVE both the branches that write recwarn themselves and the
# marker consumption further down -- it is asking "what did the PREVIOUS process
# leave", and either of those would change the answer.
if [ ! -f /tmp/openbor_reset_marker ] && [ ! -f /tmp/openbor_hotswap_marker ]; then
    rm -f /tmp/openbor_recwarn 2>/dev/null
fi
if [ -f /tmp/openbor_recmode ]; then
    _MODE=$(cat /tmp/openbor_recmode 2>/dev/null)
    _SCR="$REPLAYS/.scratch"
    # The loaded PAK's stem, needed by BOTH branches: REC copies your real saves
    # in under it, PLAY renames the take's embedded payload to it. It used to be
    # derived inside the REC arm only, so on PLAY it expanded EMPTY and the
    # rename never happened -- a take received from someone else restored under
    # the SENDER's pak name while the engine opened yours, so the replay booted
    # with no saves at all against a recording that had them. Guaranteed desync,
    # reported as "restored N save file(s)" because the handler counts files
    # rather than usable ones.
    #
    # Trim at the FIRST .pak, not the last: MiSTer writes .s0 WITHOUT truncating,
    # so a shorter new path leaves a tail from the previous longer one, and the
    # engine's own reader trims at the first .pak for exactly that reason.
    # ${_S0%.pak} would keep the residue and yield a stem nothing matches -- and
    # it fails SILENTLY, because every cp then misses and the arm-snapshot
    # equality check passes with 0 == 0.
    _S0=$(cat /media/fat/config/OpenBOR.s0 2>/dev/null | tr -d '\0')
    _PAK=$(basename "${_S0%%.pak*}" 2>/dev/null)
    case "$_MODE" in
        REC*)
            if [ -n "$_PAK" ]; then
                # $BINARY, not a literal. The comment at the top of this file records
                # that a hardcoded OpenBOR_7533 was the bug fixed for $REPLAYS -- three
                # instances survived that fix, here in the REC branch. Not reachable
                # today (4086 has no recorder), but a 4086 launch carrying a leftover
                # recmode+reset pair would seed 7533's real saves into a 4086 scratch,
                # and a third build makes it live.
                cp -f "/media/fat/saves/$BINARY/$_PAK.sav"      "$_SCR/saves/"       2>/dev/null
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
                cp -f "/media/fat/savestates/$BINARY/$_PAK".s[0-9][0-9] "$_SCR/savestates/" 2>/dev/null
                cp -f "/media/fat/savestates/$BINARY/$_PAK".scr        "$_SCR/savestates/" 2>/dev/null
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
            # Every cp above is 2>/dev/null, so a full SD or a read-only FS is
            # silent. If the arm snapshot is not a faithful copy of what the run
            # is about to boot from, the take cannot replay: playback restores a
            # set of files the run never had, the load menu differs, and the
            # recorded navigation selects a different slot. REFUSE to arm rather
            # than hand over a take that is guaranteed to desync -- which is what
            # PICO-8 does, and why its notice can honestly say "not recording".
            _NS=$(find "$_SCR/saves" "$_SCR/savestates" -maxdepth 1 -type f 2>/dev/null | wc -l)
            # BOTH sides must count the same way. `ls -A` with TWO directory
            # operands prints a "dir:" header per directory, and `grep -vc '^$'`
            # only strips blanks -- so this counted 2 phantom files. That was
            # survivable while _NS counted identically (both inflated by 2, so
            # the comparison held), and became a HARD BREAK the moment _NS was
            # switched to `find` and this line was not: _NS=3 vs _NA=5 on a
            # faithful 3-file snapshot, so the guard fired on EVERY Record and
            # OpenBOR could not arm at all. Hardware-caught 2026-08-09; source
            # review, dry-run, CI and the integrity gate all passed it.
            _NA=$(find "$REPLAYS/.armsnap/saves" "$REPLAYS/.armsnap/savestates" -maxdepth 1 -type f 2>/dev/null | wc -l)
            if [ "${_NA:-0}" -ne "${_NS:-0}" ]; then
                echo "[REC] arm snapshot is $_NA of $_NS file(s) -- not arming" >> "$_RECLOG"
                printf %s "Could not prepare saves - not recording" > /tmp/openbor_recwarn
                rm -f /tmp/openbor_recmode 2>/dev/null
            fi
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
            # 🛑 NOTHING IS SEEDED FROM THIS CARD ON PLAY. The take is the only
            # source, and that is the requirement, not a preference: a .inp must
            # be FULLY SELF-CONTAINED -- inputs, progress, stage AND character
            # unlocks -- and reproduce the same screen on ANY MiSTer.
            #
            # There WAS a `cp` here (678e438) copying /media/fat/savestates into
            # the scratch. It existed for one week, as the stopgap for refusing
            # .sNN in the payload, and it made a replay depend on the RECEIVER's
            # unlocks: your own takes stayed in sync and a take someone sent you
            # silently ran against your roster instead of theirs. Removed once
            # the payload could carry script-saves safely (validated content --
            # see mrec_snap_script_ok), which is what closed the requirement.
            #
            # 🛑 Do not add it back as a "fallback" for a take that carries no
            # script-saves. Falling back to local data is precisely the failure
            # this requirement names: it turns a visible, diagnosable gap into a
            # replay that looks right on YOUR machine and diverges on everyone
            # else's. An empty savestates scratch is the honest state.
            _EMB=0
            _SNAPFAIL=0
            if [ -n "$_INP" ] && [ -f "$_INP" ]; then
                # $_PAK is the locally-loaded PAK's stem: entries are renamed to
                # it, because the engine derives .sav/.hi/.sNN from getPakName and
                # would not see a payload still named for the recorder's copy.
                # Hand over the scratch ROOT: the payload is flat but the engine
                # reads .sav/.hi from saves/ and script-saves (.sNN) from
                # savestates/, so the binary routes each entry by extension.
                # Passing "$_SCR/saves" put every .sNN where nothing reads it.
                if ./"$BINARY" --extract-snap "$_INP" "$_SCR" "$_PAK" >> "$_RECLOG" 2>&1; then
                    _EMB=$(find "$_SCR/saves" "$_SCR/savestates" -maxdepth 1 -type f 2>/dev/null | wc -l)
                else
                    # Non-zero means the extractor REFUSED a payload it found --
                    # a bad name, a disallowed extension, a short read -- and it
                    # returns before creating the snapshot dir. Without this flag
                    # the else below announced "carries no save data" about a take
                    # that carried data we rejected, and playback ran on empty
                    # saves: a certain desync, reported as a benign fact.
                    _SNAPFAIL=1
                fi
                [ "${_EMB:-0}" -gt 0 ] && echo "[REC] restored $_EMB save file(s) from inside: $_INP" >> "$_RECLOG"
            fi
            if [ "${_EMB:-0}" -gt 0 ]; then
                :   # the take's own payload restored; nothing else to try
            elif [ "${_SNAPFAIL:-0}" -eq 1 ]; then
                echo "[REC] this take's payload was refused -- not playing" >> "$_RECLOG"
                printf %s "Could not restore the save data in this take" > /tmp/openbor_recwarn
                # REFUSE, do not play on. A take whose payload we rejected would
                # replay against empty saves -- a certain desync. PICO-8 has
                # refused this since it shipped. Dropping recmode also keeps
                # isolation down, so the player gets their REAL saves back,
                # exactly as the "could not prepare saves" branch above does.
                rm -f /tmp/openbor_recmode /tmp/openbor_playfile 2>/dev/null
            else
                echo "[REC] this take carries no save payload -- starting empty" >> "$_RECLOG"
                printf %s "This take carries no save data" > /tmp/openbor_recwarn
            fi
            ;;
    esac
fi


# Per-build log directory (matches per-build saves/savestates/replays).
# $BINARY was resolved near the top of this script, above the recorder block.
LOGDIR="/media/fat/logs/$BINARY"
mkdir -p "$LOGDIR"


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

# All log rotation happens HERE, immediately before the redirect that recreates
# the files -- not 70 lines up where it used to sit.
#
# During a sister-core RBF swap MiSTer's CORENAME lags the loaded RBF by a
# second or two, so Master_Daemon briefly spawns the OUTGOING core's handler
# against the INCOMING RBF and kills it moments later: a documented, harmless
# transient. With rotation up top, that short-lived spawn reached the mv but
# died in the `sleep 1` above before ever creating a new log -- so it rotated
# the logs away and recreated nothing, leaving OpenBorLog.txt simply absent.
# Observed 2026-08-03. Nothing was lost (mv -f on a missing source is a no-op,
# so .prev always holds the last real session), but a handler that never
# launches a binary should not disturb the logs at all.
#
# The engine-log rotation must still precede the engine, which starts at the
# exec below -- so late is correct for it too, not merely acceptable.

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
# Same idiom as the snapshot prune above, for the reason stated there: xargs
# splits on whitespace, so a path containing a space reaches `rm -f` as two
# fragments -- the second resolved relative to `cd "$GAMEDIR"`, i.e. inside the
# user's PAK library. Safe today only because $LOGDIR and these generated names
# happen to contain none, which makes it correct by accident rather than by the
# rule this file states two hundred lines earlier.
ls -t "$LOGDIR"/OpenBorLog.[0-9]*.txt 2>/dev/null | tail -n +11 | while IFS= read -r _old; do
    [ -n "$_old" ] && rm -f "$_old"
done
ls -t "$LOGDIR"/ScriptLog.[0-9]*.txt 2>/dev/null | tail -n +11 | while IFS= read -r _old; do
    [ -n "$_old" ] && rm -f "$_old"
done

echo "OpenBOR handler: dispatching to $BINARY (RBF=$MISTER_RBF)" > "$LOGDIR/OpenBOR.log"
[ -s "$_RECLOG" ] && cat "$_RECLOG" >> "$LOGDIR/OpenBOR.log"
rm -f "$_RECLOG" 2>/dev/null

# Affinity: allow BOTH cores (mask 0x03 = cores 0+1). The process mask must
# cover both cores or the binary's own thread pins fail with EINVAL (a child
# thread cannot widen affinity past the process mask — the original 0x02 mask
# silently broke thread pinning). WHICH thread goes on WHICH core is decided
# inside each build's binary (native_video_writer.c render pin +
# sblaster_patch.c audio pin), not here — this shared handler only opens
# both cores so those pins take effect.
exec taskset 0x03 ./"$BINARY" >> "$LOGDIR/OpenBOR.log" 2>&1
