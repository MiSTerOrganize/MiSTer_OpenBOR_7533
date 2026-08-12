/*
 * MiSTer_OpenBOR — sdlport.c Patch
 *
 * Adds NativeVideoWriter initialization, save directory creation,
 * and OSD PAK loading support to OpenBOR's main() function.
 *
 * PATCH: In sdl/sdlport.c, replace the entire main() function
 * (line 52 through line 118) with the version below.
 *
 * Also add these includes at the top of the file, after the existing includes:
 *
 *   #ifdef MISTER_NATIVE_VIDEO
 *   #include "native_video_writer.h"
 *   #include "native_audio_writer.h"
 *   #include <sys/stat.h>
 *   #include <stdlib.h>
 *   #include <time.h>
 *   #include <unistd.h>
 *   #include <pthread.h>
 *   #include <signal.h>
 *   #include <execinfo.h>
 *   #endif
 *
 * Copyright (C) 2026 MiSTer Organize -- GPL-3.0
 */

#ifdef MISTER_NATIVE_VIDEO
/* Crash handler — prints fault address and backtrace to stderr
 * so we can see exactly where the segfault happens.
 *
 * NOTE: apply_patches.py splices this file from the literal marker
 * "#ifdef MISTER_NATIVE_VIDEO\n/* Crash handler" to EOF. Do NOT insert anything
 * between that #ifdef and this comment -- the marker stops matching, the splice
 * silently applies NOTHING from this file, and the dry-run still reports
 * "All patches applied successfully". Add declarations below the handler. */
/* Defined in openbor.c. Validates a take (container version + content hash)
 * WITHOUT reading the frame block, so the .s1 pick can refuse BEFORE the reset
 * rather than after the PAK has already reloaded. Declared here, below the
 * splice marker above -- see that note. */
extern int mrec_probe_take(const char *path, char *why, int whysz);
/* OSD "Replay Slot" picker. mrec_slot is the ONE slot value the pause menu
 * also uses -- mirroring the OSD into it here is what makes the two pickers
 * agree. mrec_arm_slot_play() is the shared policy (library/empty/probe
 * checks + the on-screen refusals); it lives in the engine TU because
 * mrec_content_id and mrec_highest are static there. */
extern volatile int mrec_mode;
extern volatile int mrec_slot;
extern volatile int mrec_slot_pub_fresh;
extern void NativeVideoWriter_Notice(const char *msg, int seconds);
extern int mrec_arm_slot_play(int slot);
extern int mrec_save_slot_marker(int slot);   /* THE one checked writer for /tmp/openbor_recslot */
extern volatile int mrec_slot_recovered;      /* set by the recovery block once the marker is consumed */

static void mister_crash_handler(int sig, siginfo_t *info, void *ucontext)
{
    void *bt[30];
    int count;
    ucontext_t *uc = (ucontext_t *)ucontext;

    fprintf(stderr, "\n=== CRASH: signal %d at address %p ===\n", sig, info->si_addr);

    /* ARM: get PC, LR, SP from the signal context */
    fprintf(stderr, "  PC = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_pc);
    fprintf(stderr, "  LR = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_lr);
    fprintf(stderr, "  SP = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_sp);
    fprintf(stderr, "  R0 = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_r0);
    fprintf(stderr, "  R1 = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_r1);
    fprintf(stderr, "  R2 = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_r2);

    /* Try backtrace — may be empty on ARM but worth trying */
    count = backtrace(bt, 30);
    if (count > 0) {
        fprintf(stderr, "Backtrace (%d frames):\n", count);
        backtrace_symbols_fd(bt, count, STDERR_FILENO);
    }

    /* Also print the PC offset from the binary base for addr2line */
    FILE *maps = fopen("/proc/self/maps", "r");
    if (maps) {
        char line[256];
        fprintf(stderr, "Maps (first 5):\n");
        int n = 0;
        while (fgets(line, sizeof(line), maps) && n < 5) {
            fprintf(stderr, "  %s", line);
            n++;
        }
        fclose(maps);
    }

    fprintf(stderr, "=== END CRASH ===\n");
    fflush(stderr);
    _exit(139);
}

/* PAK swap detection thread — polls .s0 for path changes during gameplay.
 * When user mounts a new PAK from OSD, .s0 updates instantly. This thread
 * detects the change and triggers a clean shutdown so the daemon restarts
 * OpenBOR with the new PAK. */
static volatile int mister_swap_requested = 0;
static char mister_loaded_path[256] = {0};
static long mister_replay_mtime = -1;  /* SC1 .inp picker: baseline .s1 mtime (MiSTer bumps .s1's mtime on EVERY OSD pick, even re-picking the same file) */

static void *mister_swap_thread(void *arg)
{
    (void)arg;
    char check_path[256];

    while (!mister_swap_requested) {
        sleep(1);
        FILE *f = fopen("/media/fat/config/OpenBOR.s0", "r");
        /* 🛑 `continue` here skips the REST of the loop body, which includes
         * the .s1 picker and the OSD Replay Slot poll further down. Neither has
         * anything to do with .s0 being readable, so a missing .s0 silently
         * disabled two unrelated features. Benign today only because this
         * thread starts after the blocking .s0 wait -- i.e. by luck of ordering,
         * not by design. PICO-8's equivalent poll has no such dependency.
         * Skip only the .s0 work. */
        if (f) {
        check_path[0] = 0;
        if (fgets(check_path, sizeof(check_path), f)) {
            char *nl = strchr(check_path, '\n');
            if (nl) *nl = 0;
            char *cr = strchr(check_path, '\r');
            if (cr) *cr = 0;
            /* Same MiSTer write-without-truncate workaround as the
             * initial reader — trim at first .pak + strip trailing space. */
            char *pakext = strstr(check_path, ".pak");
            if (pakext) pakext[4] = 0;
            int pl = (int)strlen(check_path);
            while (pl > 0 && (check_path[pl-1] == ' ' || check_path[pl-1] == '\t')) {
                check_path[--pl] = 0;
            }
        }
        fclose(f);

        if (strlen(check_path) > 0 && strlen(mister_loaded_path) > 0) {
            char full[256];
            /* OSD picks write relative paths (games/OpenBOR/Paks/foo.pak);
             * MGL writes absolute paths (/media/fat/...). exFAT does not
             * normalize //, so unconditional prefix-prepend silently breaks
             * MGL loads. Conditionally prepend only for relative paths. */
            if (check_path[0] == '/')
                snprintf(full, sizeof(full), "%s", check_path);
            else
                snprintf(full, sizeof(full), "/media/fat/%s", check_path);
            if (strcmp(full, mister_loaded_path) != 0) {
                FILE *mf;
                fprintf(stderr, "MiSTer: PAK swap detected: %s\n", full);
                fflush(stderr);
                mister_swap_requested = 1;
                /* Hot-swap marker: tell _handler.sh to PRESERVE the
                 * freshly-written .s0 (which MiSTer Main just populated
                 * with the new PAK path the user picked). Without this,
                 * handler's else-branch wipes the new path → binary
                 * respawns into wait-for-OSD-pick → black screen until
                 * user picks the same PAK a second time. Same shape as
                 * the Reset-Pak marker (pausemenu_patch.c case 2). */
                mf = fopen("/tmp/openbor_hotswap_marker", "w");
                if (mf) fclose(mf);
                /* 🛑 Drop any PENDING arm markers. "Hot-swap resets the
                 * recorder for free because _exit() wipes process state" is
                 * true of the recorder and false of these: they are
                 * FILESYSTEM state and outlive the process by design.
                 * Record on PAK A slot 5, respawn, pick PAK B while A is
                 * still loading, and the recovery block finds recmode=REC
                 * plus recslot=5 still sitting there -- so it arms Record on
                 * PAK B and overwrites B's slot 5 at Stop. The engine
                 * already defends the same shape one line from here
                 * (remove("/tmp/openbor_recstop") in the recovery block);
                 * these two had no equivalent. Recmode is the gate, but
                 * clear its payloads too so nothing stale is left behind. */
                remove("/tmp/openbor_recmode");
                remove("/tmp/openbor_recslot");
                remove("/tmp/openbor_playfile");
                /* Use _exit instead of borExit. borExit() calls SDL_Quit()
                 * which is not safe from a non-main thread (we crashed
                 * with SIGSEGV under our keepalive + SDL teardown). The
                 * process is terminating anyway — daemon will relaunch
                 * cleanly and pick up the new .s0 path. */
                _exit(1);
            }
        }
        }   /* end of the .s0 block -- the pollers below run regardless */

        /* SC1 "Load Replay" picker: MiSTer rewrites .s1 (bumping its MTIME) on
         * EVERY OSD pick — including re-picking the SAME .inp after a quit/re-
         * enter — so detect by mtime, NOT path content (a content-compare missed
         * a re-pick of the same file: same path, new mtime). A .s1 mtime newer
         * than the startup baseline = a fresh pick: read the path, write
         * /tmp/openbor_playfile + PLAY + reset markers (so _handler.sh preserves
         * .s0 -> same PAK reloads to title), and exit so the daemon respawn arms
         * title-anchored playback of that file. The startup baseline means a
         * pre-existing/stale .s1 never auto-replays. */
        {
            struct stat s1st;
            if (stat("/media/fat/config/OpenBOR.s1", &s1st) == 0
                && (long)s1st.st_mtime != mister_replay_mtime) {
                FILE *rf = fopen("/media/fat/config/OpenBOR.s1", "r");
                char rp[256] = {0};
                if (rf) {
                    if (fgets(rp, sizeof(rp), rf)) {
                        char *n = strchr(rp, '\n'); if (n) *n = 0;
                        char *c = strchr(rp, '\r'); if (c) *c = 0;
                        char *e = strstr(rp, ".inp"); if (e) e[4] = 0;
                        int rl = (int)strlen(rp);
                        while (rl > 0 && (rp[rl-1] == ' ' || rp[rl-1] == '\t')) rp[--rl] = 0;
                    }
                    fclose(rf);
                }
                if (strlen(rp) > 0) {
                    char rfull[256];
                    FILE *mf;
                    if (rp[0] == '/') snprintf(rfull, sizeof(rfull), "%s", rp);
                    else snprintf(rfull, sizeof(rfull), "/media/fat/%s", rp);
                    fprintf(stderr, "MiSTer: replay pick (.s1): %s\n", rfull);
                    fflush(stderr);
                    /* Probe before the reset. THIS is where wrong picks come from:
                     * the OSD browser will hand us a take for any PAK in the
                     * library, and this path used to arm and _exit(1) without
                     * opening the file -- destroying the run and explaining itself
                     * only after the reload. Refusing here leaves it untouched. */
                    if (mrec_mode == 1) {
                        /* Refuse rather than exit, exactly as the OSD slot Play
                         * path does. Exiting here would discard an unwritten take
                         * with no confirm and no message, while pause-Quit raises
                         * a LOSE RECORDING dialog for the same loss. Same wording
                         * on both cores. */
                        fprintf(stderr, "MiSTer: replay refused -- recording in progress\n");
                        fflush(stderr);
                        NativeVideoWriter_Notice("Stop the recording first", 5);
                        mister_replay_mtime = (long)s1st.st_mtime;
                        continue;
                    }
                    {
                        char rwhy[128];
                        if (!mrec_probe_take(rfull, rwhy, (int)sizeof(rwhy))) {
                            fprintf(stderr, "MiSTer: replay refused: %s\n", rwhy);
                            fflush(stderr);
                            NativeVideoWriter_Notice(rwhy, 6);
                            /* baseline already advanced below, so this does not re-fire */
                            mister_replay_mtime = (long)s1st.st_mtime;
                            continue;
                        }
                    }
                    /* Commit the slot before dying. The adoption branch below
                     * deliberately does NOT write the marker (see the note
                     * there: this thread polls long before the recovery block
                     * that reads it), so after an OSD slot move the user's
                     * choice lives only in mrec_slot, in memory. Every OTHER
                     * respawn writes the marker at commit time; this was the one
                     * that did not, so a .s1 replay taken after an OSD move came
                     * back showing the PREVIOUS slot.
                     *
                     * 🛑 This write is exempt from that note for one reason
                     * only: the process exits on the next line, so there is no
                     * later read in THIS process for it to clobber. Do not take
                     * it as licence to write the marker from the poll itself.
                     *
                     * 🛑 ONLY AFTER RECOVERY HAS CONSUMED THE MARKER. This
                     * thread starts BEFORE openborMain, while the recovery block
                     * that reads and removes /tmp/openbor_recslot does not run
                     * until the PAK has finished loading (1.9 s on ATOV, 69 s on
                     * JL Legacy). Writing in that window OVERWRITES a pending
                     * marker this process has not read yet: arm Record on slot
                     * 5, respawn, pick a .s1 replay during the load, and the
                     * next process comes up on slot 1.
                     *
                     * 🛑 Gate on the RECOVERY FLAG, never on mrec_slot's value.
                     * The obvious `mrec_slot >= 1 && <= MREC_SLOTS` test looks
                     * equivalent and is not -- the OSD adoption branch below
                     * runs on THIS thread and sets mrec_slot to 1..8 while
                     * mrec_mode is still 0, i.e. long before recovery. So the
                     * value is non-zero for almost the whole load window and the
                     * guard passed exactly when it needed to hold. (It also
                     * referenced MREC_SLOTS, which is #defined into openbor.c
                     * and NOT visible in this translation unit -- it did not
                     * compile.) The flag is owned by the reader, so the
                     * invariant is true by construction rather than by argument. */
                     * 🛑 …but a skipped write is not a free pass either. The
                     * flag alone cannot separate the two ways it reads zero:
                     * a marker is still PENDING (skip -- it already carries
                     * the user's slot, and clobbering it is the bug the flag
                     * was added for), or NO marker exists at all (write, or
                     * the slot is silently dropped). Only the first is a
                     * reason to skip, so ask the filesystem which one it is.
                     *
                     * 🛑 And REFUSE on a failed write -- do not warn to stderr
                     * and arm anyway. No player reads stderr. PICO-8's
                     * identical path refuses with a Notice; this one did not,
                     * and both silent paths land in the same place: the next
                     * process finds no marker, clamps to slot 1, and freezes
                     * adoption at mode 2 for the whole replay -- the pause
                     * picker showing 1 while the OSD shows the real slot, for
                     * the entire run. That is the two-pickers-disagree state
                     * this work exists to prevent, reached through the failure
                     * branch. Nothing has _exit()ed yet, so refusing is free. */
                    if (mrec_slot_recovered || access("/tmp/openbor_recslot", F_OK) != 0) {
                        if (!mrec_save_slot_marker(mrec_slot)) {
                            fprintf(stderr, "MiSTer: could not carry the slot -- not playing\n");
                            fflush(stderr);
                            NativeVideoWriter_Notice("Could not start playback", 5);
                            /* baseline advanced so this does not re-fire */
                            mister_replay_mtime = (long)s1st.st_mtime;
                            continue;
                        }
                    }
                    mister_swap_requested = 1;
                    mf = fopen("/tmp/openbor_playfile", "w"); if (mf) { fputs(rfull, mf); fclose(mf); }
                    mf = fopen("/tmp/openbor_recmode", "w"); if (mf) { fputs("PLAY", mf); fclose(mf); }
                    mf = fopen("/tmp/openbor_reset_marker", "w"); if (mf) fclose(mf);
                    _exit(1);
                }
                /* .s1 changed but empty/unreadable — advance baseline so we don't spin */
                mister_replay_mtime = (long)s1st.st_mtime;
            }
        }

        /* OSD "Replay Slot" 1-8 + "Play Replay". The FPGA publishes both in the
         * spare upper half of the JOY0 qword (0x0C), which it already writes
         * every frame, so reading it here costs nothing.
         *
         * Two jobs, and the first is the one that makes the feature honest:
         * mirror the OSD's slot into mrec_slot so the pause-menu picker shows
         * the SAME number. One slot value, two displays.
         *
         * Play reuses mrec_arm_slot_play() -- the same function the pause menu
         * calls -- so an empty slot refuses with the same words here as there
         * instead of resetting the PAK to find out. */
        {
            /* 🛑 A Play pressed during the ~1-2 s respawn window is DISCARDED,
             * deliberately and silently, and that is worth stating because it
             * looks like a bug from the outside.
             *
             * Record and Play both _exit() and let Master_Daemon respawn us, so
             * for a second or two no process is reading 0x0C. The FPGA still
             * counts presses. On restart rs_primed is 0, we adopt whatever
             * rs_seq has reached as the baseline, and anything pressed while we
             * were gone is absorbed into it.
             *
             * Queuing it instead would be worse: the press that armed THIS
             * respawn is the one still echoing, so honouring it would restart
             * the content again the moment it finished loading -- and a Play
             * queued behind a Record arm would immediately throw away the take
             * the user just started. Discarding is the correct behaviour; it is
             * only the silence that reads oddly, and there is nothing to report
             * against (no record of the pre-exit sequence survives the exit). */
            static int      rs_primed = 0;
            static unsigned rs_last_seq = 0;
            uint32_t rw    = NativeVideoWriter_ReadReplay();
            unsigned rseq  = (rw >> 16) & 0xFFu;
            unsigned rcmd  = rw & 3u;
            int      rslot = (int)((rw >> 8) & 7u) + 1;   /* wire is 0-based */

            /* Seed the sequence from the FIRST real read, never from 0. DDR3
             * holds whatever the previous core left there, so an unseeded
             * compare can fire a spurious Play the moment the core loads. */
            if (!rs_primed) {
                rs_primed = 1;
                /* Consume a publish that happened before priming, or the skip
                 * lands on the wrong poll -- harmless, but it would be spent on
                 * a poll it was not meant for. Mirrors PICO-8. */
                mrec_slot_pub_fresh = 0;
            } else {
                /* The skip below covers ADOPTION ONLY. Play must still be
                 * evaluated on a skipped poll: rs_last_seq advances
                 * unconditionally at the bottom, so a Play pulse arriving during
                 * a skipped poll would be consumed and LOST for good rather than
                 * merely delayed.
                 *
                 * rslot is the slot as of the once-per-frame DDR3 write, NOT a
                 * snapshot taken at the Play press. (An earlier version of this
                 * comment claimed the RTL latched cmd and slot together on the
                 * pulse. It does not -- rs_slot_lat tracks the current slot every
                 * cycle, which is exactly what lets an OSD move be mirrored
                 * without pressing Play.) That is still the right value to act
                 * on: it is what the OSD is displaying when the user presses. */
                if (mrec_slot_pub_fresh) {
                    /* The pause menu published a slot since the last poll, and
                     * the echo we are holding may pre-date it: the reader latches
                     * rs_slot_lat every clk_sys cycle but only WRITES 0x0C once
                     * per frame (ST_WRITE_JOY0). Adopting a stale echo would
                     * REVERT the press the user just made, then self-correct a
                     * poll later -- "the slot sometimes jumps back", and a Record
                     * made inside that window lands in the wrong slot.
                     *
                     * One skipped poll is sufficient and cannot stall: the next
                     * is ~1 s away, long after the reader has rewritten 0x0C many
                     * times. A simultaneous OSD move is simply picked up then. */
                    mrec_slot_pub_fresh = 0;
                } else if (mrec_mode == 0 && rslot != mrec_slot) {
                    /* 🛑 Adoption is FROZEN for the life of a take. The slot row
                     * is drawn on the idle branch only, so while recording there
                     * is nothing on screen showing which slot Stop will write to
                     * -- and the OSD picker is still reachable. Without this
                     * gate, moving it mid-take silently retargets Stop onto a
                     * DIFFERENT slot and destroys the take that was in it, with
                     * the only feedback arriving after the damage.
                     *
                     * 🛑 And do NOT write /tmp/openbor_recslot from this thread.
                     * It runs from before openborMain and polls every second,
                     * while the recovery block that READS that file does not run
                     * until the PAK has finished loading -- 1.9 s on ATOV, 69 s
                     * on JL Legacy. Writing here overwrote the marker before it
                     * was ever read, on essentially every PAK, quietly making the
                     * FPGA echo the source of truth instead of the file the
                     * design says carries the slot. mrec_slot alone is enough:
                     * the arm paths write the marker themselves, at the moment
                     * they commit.
                     *
                     * 🛑 NOT all of them on the engine thread, which this
                     * comment used to claim. The pause-menu arms are on the
                     * engine thread; mrec_arm_slot_play is called from THIS
                     * thread too, by the OSD Play branch just below. That is
                     * safe for one specific reason -- every such caller
                     * _exit()s within microseconds, so nothing in this process
                     * can read the marker after it is written -- and that
                     * reason is worth stating, because it is the property that
                     * makes it safe, not the thread it runs on. */
                    mrec_slot = rslot;
                }

                if (rcmd == 1u && rseq != rs_last_seq) {
                    fprintf(stderr, "MiSTer: OSD play replay, slot %d\n", rslot);
                    fflush(stderr);
                    if (mrec_mode == 1) {
                        /* 🛑 Refuse rather than exit. This is the ONLY route that
                         * can reach Play while recording -- the pause submenu
                         * hides Play at mode 1 -- and an unguarded _exit() here
                         * would discard an unwritten take with no confirm and no
                         * message, while pause-Quit raises a LOSE RECORDING
                         * dialog for exactly the same loss. A Notice() placed
                         * just before _exit() would not help: the process is gone
                         * before a frame publishes. Same wording as PICO-8. */
                        fprintf(stderr, "MiSTer: OSD play refused -- recording in progress\n");
                        fflush(stderr);
                        NativeVideoWriter_Notice("Stop the recording first", 5);
                    }
                    else if (mrec_arm_slot_play(rslot)) {
                        FILE *mf;
                        mister_swap_requested = 1;
                        mf = fopen("/tmp/openbor_recmode", "w");
                        if (mf) { fputs("PLAY", mf); fclose(mf); }
                        mf = fopen("/tmp/openbor_reset_marker", "w");
                        if (mf) fclose(mf);
                        _exit(1);
                    }
                    /* Refused: it has already said why on screen. Fall through
                     * and keep playing -- do NOT reset. */
                }
            }
            rs_last_seq = rseq;
        }
    }
    return NULL;
}

/* ---- .inp save-payload extractor (step 21) -------------------------------
 *
 * Reads the payload embedded in a take and writes it into destdir. Parses the
 * container SEQUENTIALLY -- magic, header fields, identity section, frame block,
 * then records -- so it never depends on an absolute byte offset. That is
 * deliberate: hand-computed offsets are what silently broke this format when the
 * magic shrank 5 -> 4, and the handler's dd went stale the same way.
 *
 * SECURITY. This function parses bytes written by a STRANGER into filenames on
 * the recipient's root filesystem, so every name is subjected to:
 *   1. bare filename only  -- no '/', no '\\', not "." or ".."
 *   2. an EXTENSION WHITELIST -- .sav, .hi, .sNN and nothing else
 * The whitelist is the load-bearing control, and it is by EXTENSION, never a
 * bare-name check. PICO-8 learned that the hard way: its guard asked only "is
 * this a bare filename", so an entry called <cart>.p8 was accepted and spliced
 * over the cart's ROM before the first frame.
 *
 * .sNN stays IN because those are script-saves holding unlocks and progression,
 * so determinism genuinely needs them (design note O1) -- and they are
 * re-executable OpenBOR script, which loadScriptFile() executes. That is an
 * accepted, documented risk: a shared take can run script inside the recipient's
 * engine. The README says so plainly. Do not silently widen this list.
 *
 * Stem remapping: save names derive from getPakName, so a recipient whose file
 * is named differently has an engine looking for names the payload does not
 * contain. Each entry's stem is rewritten to local_stem, preserving only the
 * extension -- which is also why the extension, not the name, is what we trust.
 *
 * Returns 0 on success, non-zero on refusal. Prints one line per outcome. */

/* .sNN -- saveScriptFile overwrites the last two chars of .scr with the level-set
 * number, so a multi-set PAK has .s00, .s01, ... one per set. Matching only .s00
 * would silently drop every set past the first.
 *
 * ONE implementation, because two things need this answer: the extension
 * whitelist, and the router that decides whether an entry belongs in
 * .scratch/savestates or .scratch/saves. Two copies of a rule is how the value a
 * thing is CHECKED against drifts from the value it is USED for -- the shape
 * behind every recorder bug found on 2026-08-07. Takes the dot, not the name. */
static int mrec_snap_is_script_save(const char *dot)
{
    return dot
        && (dot[1] == 's' || dot[1] == 'S')
        && dot[2] >= '0' && dot[2] <= '9'
        && dot[3] >= '0' && dot[3] <= '9'
        && dot[4] == 0;
}

static int mrec_snap_ext_ok(const char *name)
{
    const char *dot = strrchr(name, '.');
    if (!dot || dot == name) return 0;
    if (strcasecmp(dot, ".sav") == 0) return 1;
    if (strcasecmp(dot, ".hi")  == 0) return 1;
    if (mrec_snap_is_script_save(dot)) return 1;
    return 0;
}

/* Empty one scratch leaf. Used only to undo a partial extraction -- see the
 * refusal path below for why clearing wholesale is safe here. */
static void mrec_snap_clear_dir(const char *dir)
{
    DIR *d = opendir(dir);
    struct dirent *e;
    char p[1024];
    if (!d) return;
    while ((e = readdir(d)) != NULL)
    {
        if (e->d_name[0] == '.') continue;
        snprintf(p, sizeof(p), "%s/%s", dir, e->d_name);
        remove(p);
    }
    closedir(d);
}

static int mrec_extract_snap(const char *inp, const char *destdir, const char *local_stem)
{
    FILE *f; unsigned int cont = 0, engver = 0, n32 = 0, crc = 0, cnt = 0, i, pc = 0;
    unsigned short ic = 0, nl = 0, sl = 0;
    unsigned char magic[4];
    char pak[256], stem[128];
    long frames, frames_start = -1, payload_start = -1;
    int wrote = 0, refused = 0;

    f = fopen(inp, "rb");
    if (!f) { printf("[SNAP] cannot open %s\n", inp); return 1; }

    if (fread(magic,1,4,f) != 4 || magic[0]!='M'||magic[1]!='R'||magic[2]!='E'||magic[3]!='C'
        || fread(&cont,4,1,f)!=1 || cont != 2u
        || fread(&engver,4,1,f)!=1
        || fseek(f,12,SEEK_CUR)!=0                 /* build date */
        || fread(pak,1,256,f)!=256
        || fseek(f,8,SEEK_CUR)!=0                  /* seed */
        || fread(&n32,4,1,f)!=1
        || fread(&crc,4,1,f)!=1)
    { printf("[SNAP] %s is not a v2 recording\n", inp); fclose(f); return 1; }

    /* identity section: entry (optional) then the recorder's stem */
    stem[0] = 0;
    if (fread(&ic,2,1,f)!=1 || ic > 1) { printf("[SNAP] bad identity section\n"); fclose(f); return 1; }
    if (ic == 1)
    {
        if (fread(&nl,2,1,f)!=1 || nl == 0 || fseek(f,nl,SEEK_CUR)!=0 || fseek(f,20,SEEK_CUR)!=0)
        { printf("[SNAP] bad identity entry\n"); fclose(f); return 1; }
    }
    if (fread(&sl,2,1,f)!=1 || sl >= (unsigned short)sizeof(stem)
        || (sl && fread(stem,1,sl,f)!=sl))
    { printf("[SNAP] bad stem field\n"); fclose(f); return 1; }
    stem[sl] = 0;
    frames_start = ftell(f);   /* the identity section is variable length, so this
                                * is MEASURED, never a hand-computed constant */
    if (frames_start < 0) { printf("[SNAP] cannot locate the frame block\n"); fclose(f); return 1; }

    /* Skip the frame block: 9 x u64 per frame, fixed width by design.
     *
     * Check the size EXPLICITLY rather than trusting fseek. Seeking past EOF is
     * legal and SUCCEEDS on POSIX, so a header claiming two million frames sailed
     * through this check, the payload read then failed, and the whole thing
     * returned "no payload in this take" -- i.e. SUCCESS -- for a file that was
     * either truncated or lying about its length. */
    frames = (long)n32;
    if (frames < 0 || frames > 2000000L
        || fseek(f, 0, SEEK_END) != 0)
    { printf("[SNAP] frame block is not readable\n"); fclose(f); return 1; }
    {
        long fsize = ftell(f);
        long need = frames_start + frames * (9*8);
        if (fsize < 0 || need < frames_start || fsize < need)
        { printf("[SNAP] %s is truncated (needs %ld bytes, has %ld)\n", inp, need, fsize);
          fclose(f); return 1; }
        if (fseek(f, need, SEEK_SET) != 0)
        { printf("[SNAP] frame block is not readable\n"); fclose(f); return 1; }
    }

    /* Within a version, an ABSENT count is truncation, not "an older take": the
     * container version above already rejected every older format, and the writer
     * ALWAYS writes the count (0 when empty). */
    if (fread(&cnt,4,1,f) != 1)
    { printf("[SNAP] %s is truncated -- no payload count\n", inp); fclose(f); return 1; }
    if (cnt > 4096u) { printf("[SNAP] payload claims %u files -- refusing\n", cnt); fclose(f); return 1; }

    /* TWO PASSES: validate every record, THEN write.
     *
     * The single-pass version wrote each file as it went and stopped at the first
     * bad entry -- so a payload of [good.sav, ../evil.sav] returned "refused" and
     * still left good.sav on disk. The handler counts what landed and treats a
     * non-zero count as success, so a hostile take got a PARTIAL restore in. A
     * partial restore is exactly the desync-dressed-as-a-warning this refuses to
     * do. Validating first means nothing is created unless everything is legal. */
    payload_start = ftell(f);
    for (i = 0; i < cnt; i++)
    {
        unsigned int enl = 0, edl = 0;
        char name[512];
        if (fread(&enl,4,1,f)!=1 || enl == 0 || enl >= sizeof(name)) { refused = 1; break; }
        if (fread(name,1,enl,f)!=enl) { refused = 1; break; }
        name[enl] = 0;
        if (fread(&edl,4,1,f)!=1 || edl > 8u*1024u*1024u) { refused = 1; break; }
        if (strchr(name,'/') || strchr(name,'\\') || strcmp(name,".")==0 || strcmp(name,"..")==0
            || !mrec_snap_ext_ok(name))
        { printf("[SNAP] refusing entry '%s' -- not an allowed save file\n", name);
          refused = 1; break; }
        if (fseek(f, (long)edl, SEEK_CUR) != 0) { refused = 1; break; }
    }
    if (refused || payload_start < 0 || fseek(f, payload_start, SEEK_SET) != 0)
    {
        printf("[SNAP] payload rejected -- nothing restored\n");
        fclose(f);
        return 1;
    }

    mkdir(destdir, 0777);

    for (i = 0; i < cnt; i++)
    {
        unsigned int enl = 0, edl = 0;
        char name[512], out[1024];
        const char *dot;
        FILE *o;

        if (fread(&enl,4,1,f)!=1 || fread(name,1,enl,f)!=enl
            || fread(&edl,4,1,f)!=1) { refused = 1; break; }
        name[enl] = 0;

        dot = strrchr(name, '.');

        /* Route by extension. The payload is FLAT -- the writer walks
         * .armsnap/saves and .armsnap/savestates into one list and stores only
         * the bare filename -- but the engine reads them from two DIFFERENT
         * roots: .sav/.hi from .scratch/saves, and script-saves (.sNN) from
         * .scratch/savestates (see the SaveStates path rewrite in
         * apply_patches.py).
         *
         * destdir is therefore the scratch ROOT, not a leaf. Passing the leaf
         * put every .sNN in .scratch/saves, where nothing reads it, so
         * .scratch/savestates stayed empty and a replay booted WITHOUT the
         * unlocks and progression the take was recorded against -- a guaranteed
         * desync on any multi-set PAK. It could not fall back either: the local-
         * snapshot branch does restore savestates correctly, but it is skipped
         * whenever the embedded count is non-zero, and the misplaced files are
         * what made it non-zero. Found by a review agent 2026-08-07 and
         * confirmed in source. */
        {
            const char *leaf = mrec_snap_is_script_save(dot) ? "savestates" : "saves";
            if (local_stem && *local_stem)
                snprintf(out, sizeof(out), "%s/%s/%s%s", destdir, leaf, local_stem, dot);
            else
                snprintf(out, sizeof(out), "%s/%s/%s", destdir, leaf, name);
        }

        o = fopen(out, "wb");
        if (!o) { printf("[SNAP] cannot write %s\n", out); refused = 1; break; }
        while (edl)
        {
            char buf[8192];
            size_t want = (edl < sizeof(buf)) ? (size_t)edl : sizeof(buf);
            size_t got  = fread(buf,1,want,f);
            if (got != want || fwrite(buf,1,got,o) != got) { refused = 1; break; }
            edl -= (unsigned int)got;
        }
        if (fclose(o) != 0) refused = 1;
        if (refused) { remove(out); break; }   /* the rest is cleared below */
        wrote++;
    }
    fclose(f);

    if (refused)
    {
        /* Pass 1 accepted every record, so reaching here means an I/O failure
         * mid-write. Leave nothing half-restored. */
        /* ...and mean it. Removing only the file that FAILED left every
         * earlier one on disk, and the handler then fell back to the local
         * snapshot and copied ON TOP of that residue -- a boot state
         * matching neither source. A silent desync is worse than a clean
         * refusal.
         *
         * Clearing both leaves wholesale is safe here: the handler recreates
         * .scratch empty immediately before this runs, and this function is
         * only reached on the PLAY path, so the sole contents are the files
         * this call just wrote. */
        {
            char d[1024];
            snprintf(d, sizeof(d), "%s/saves", destdir);      mrec_snap_clear_dir(d);
            snprintf(d, sizeof(d), "%s/savestates", destdir); mrec_snap_clear_dir(d);
        }
        printf("[SNAP] write failed after %d file(s) -- not restoring\n", wrote);
        return 1;
    }
    printf("[SNAP] restored %d save file(s) from %s\n", wrote, inp);
    (void)pc; (void)engver;
    return 0;
}
#endif

int main(int argc, char *argv[])
{
#ifndef SKIP_CODE
    char pakname[256];
#endif
#ifdef CUSTOM_SIGNAL_HANDLER
    struct sigaction sigact;
#endif

#ifdef DARWIN
    char resourcePath[PATH_MAX];
    CFBundleRef mainBundle;
    CFURLRef resourcesDirectoryURL;
    mainBundle = CFBundleGetMainBundle();
    resourcesDirectoryURL = CFBundleCopyResourcesDirectoryURL(mainBundle);
    if(!CFURLGetFileSystemRepresentation(resourcesDirectoryURL, true, (UInt8 *) resourcePath, PATH_MAX))
    {
        borExit(0);
    }
    CFRelease(resourcesDirectoryURL);
    chdir(resourcePath);
#elif WII
    fatInitDefault();
#endif

#ifdef CUSTOM_SIGNAL_HANDLER
    sigact.sa_sigaction = handleFatalSignal;
    sigact.sa_flags = SA_RESTART | SA_SIGINFO;

    if(sigaction(SIGSEGV, &sigact, NULL) != 0)
    {
        printf("Error setting signal handler for %d (%s)\n", SIGSEGV, strsignal(SIGSEGV));
        exit(EXIT_FAILURE);
    }
#endif

#ifdef MISTER_NATIVE_VIDEO
    /* Install crash handler FIRST — catches segfaults with backtrace */
    {
        struct sigaction sa;
        sa.sa_sigaction = mister_crash_handler;
        sa.sa_flags = SA_SIGINFO | SA_RESETHAND;
        sigemptyset(&sa.sa_mask);
        sigaction(SIGSEGV, &sa, NULL);
        sigaction(SIGBUS, &sa, NULL);
        sigaction(SIGABRT, &sa, NULL);
    }
    setenv("SDL_VIDEODRIVER", "dummy",  1);
    setenv("SDL_AUDIODRIVER", "dummy",  1);
    /* OpenBOR 4.0 uses the SDL2 SDL_Renderer API (renderer + texture)
     * for video output. The dummy video driver registers no render
     * drivers, so SDL_CreateRenderer auto-select fails with
     * "Couldn't find matching render driver". Force the software
     * renderer — its SW_RenderPresent calls SDL_UpdateWindowSurface,
     * which lands in our patched SDL_DUMMY_UpdateWindowFramebuffer
     * and pushes the frame to DDR3. */
    setenv("SDL_RENDER_DRIVER", "software", 1);
    /* Disable stdio buffering so OpenBOR's printf calls appear in
     * the log immediately. Without this, a crash mid-init swallows
     * up to 4KB of pending output (including the version banner). */
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    /* --extract-snap <take.inp> <scratch-root> <local-stem>
     *
     * <scratch-root>, NOT a leaf: entries are routed by extension into
     * <root>/saves or <root>/savestates, because the engine reads .sav/.hi from
     * one and script-saves (.sNN) from the other.
     *
     * Unpacks the save payload embedded in a recording and exits. Runs HERE:
     * after unbuffered logging is on, but before SDL, DDR3 video/audio, the
     * directory creation and -- critically -- before the blocking .s0 OSD wait,
     * which would otherwise sit forever with no PAK to pick.
     *
     * It lives in C rather than the handler on purpose. The alternative was
     * hunting byte offsets with dd and od in BusyBox, which is how the seed
     * offset silently went stale when the header changed; and the extension
     * check below is a security control, not a convenience -- shell quoting is
     * the wrong place for it.
     *
     * The handler invokes this on PLAY and then restores from destdir. */
    if (argc >= 4 && strcmp(argv[1], "--extract-snap") == 0)
        return mrec_extract_snap(argv[2], argv[3], argc >= 5 ? argv[4] : NULL);
#endif

    setSystemRam();
    initSDL();

#ifdef MISTER_NATIVE_VIDEO
    /* Initialize DDR3 native video writer */
    if (!NativeVideoWriter_Init()) {
        fprintf(stderr, "NativeVideoWriter: init failed, falling back to SDL\n");
    }

    /* Initialize DDR3 native audio writer. sblaster's SB_playstart
     * thread will refuse to start if this fails. */
    if (!NativeAudioWriter_Init()) {
        fprintf(stderr, "NativeAudioWriter: init failed, audio will be silent\n");
    }

    /* Create MiSTer directories */
    mkdir("/media/fat/saves", 0755);
    mkdir("/media/fat/saves/OpenBOR_7533", 0755);
    mkdir("/media/fat/savestates", 0755);
    mkdir("/media/fat/savestates/OpenBOR_7533", 0755);
    mkdir("/media/fat/config", 0755);
#endif

    packfile_mode(0);
#ifdef ANDROID
    dirExists(rootDir, 1);
    chdir(rootDir);
#endif
    dirExists(paksDir, 1);
#ifdef MISTER_NATIVE_VIDEO
    /* Saves redirected to /media/fat/saves/OpenBOR_7533/ in utils.c. */
    dirExists(logsDir, 1);
#else
    dirExists(savesDir, 1);
    dirExists(logsDir, 1);
    dirExists(screenShotsDir, 1);
#endif

#ifdef ANDROID
    if(dirExists("/mnt/usbdrive/OpenBOR/Paks", 0))
        strcpy(paksDir, "/mnt/usbdrive/OpenBOR/Paks");
    else if(dirExists("/usbdrive/OpenBOR/Paks", 0))
        strcpy(paksDir, "/usbdrive/OpenBOR/Paks");
    else if(dirExists("/mnt/extsdcard/OpenBOR/Paks", 0))
        strcpy(paksDir, "/mnt/extsdcard/OpenBOR/Paks");
#endif

#ifdef MISTER_NATIVE_VIDEO
    /* Cart cache lives on tmpfs (/tmp). This is critical for load
     * speed: OpenBOR reads the PAK every time it starts, so the cache
     * has to be in RAM -- reading a 100+ MB PAK off SD takes minutes.
     * /tmp survives the Reset Pak exit+relaunch cycle (same Linux
     * session), and is cleared on reboot which is the right behaviour
     * -- after a reboot the user will re-pick through MiSTer's OSD,
     * the cart will stream via ioctl into DDR3, and we cache it here
     * fresh. */
    #define MISTER_PAK_CACHE "/tmp/openbor_current.pak"
    #define MISTER_S0_PATH   "/media/fat/config/OpenBOR.s0"
    {
        /* SC0 (mounted image + config) approach: user selects PAK
         * from OSD, MiSTer writes path to .s0 config INSTANTLY —
         * no ioctl streaming, no 2-minute wait, no RAM exhaustion.
         * ARM reads .s0 to get path, loads PAK from SD directly. */

        /* 1) Check for Reset Pak cache (in /tmp, survives exit+relaunch) */
        struct stat st;
        if (stat(MISTER_PAK_CACHE, &st) == 0 && st.st_size > 0) {
            strncpy(packfile, MISTER_PAK_CACHE, sizeof(packfile) - 1);
            packfile[sizeof(packfile) - 1] = 0;
            fprintf(stderr, "MiSTer: Reset Pak cache found: %s (%ld bytes)\n",
                    packfile, (long)st.st_size);
        }
        /* 2) Poll for .s0 (MiSTer creates it instantly when user picks from OSD) */
        else {
            char s0_path[256] = {0};

            /* Clear DDR3 framebuffer so the screen goes black during the wait
             * loop instead of showing the previous binary's last frame (which
             * happens because the FPGA keepalive keeps reading the last
             * written buffer). User-reported 2026-05-17: re-entering OpenBOR
             * after a PAK quit shows stale gameplay frames until a new PAK
             * picks. Writing one black frame fixes this. */
            {
                static unsigned char _black[NV_WIDTH * NV_HEIGHT * 2] = {0};
                NativeVideoWriter_WriteFrame(_black, NV_WIDTH, NV_HEIGHT,
                                              NV_WIDTH * 2, 16, NULL);
            }

            fprintf(stderr, "MiSTer: waiting for OSD PAK selection (.s0)...\n");
            while (1) {
                FILE *f = fopen(MISTER_S0_PATH, "r");
                if (f) {
                    if (fgets(s0_path, sizeof(s0_path), f)) {
                        char *nl = strchr(s0_path, '\n');
                        if (nl) *nl = 0;
                        char *cr = strchr(s0_path, '\r');
                        if (cr) *cr = 0;
                        /* MiSTer's OSD writes .s0 without truncating, so when
                         * a shorter PAK path overwrites a longer one, trailing
                         * bytes of the previous filename remain. Truncate at
                         * the first ".pak" extension to recover the real path. */
                        char *pakext = strstr(s0_path, ".pak");
                        if (pakext) pakext[4] = 0;
                        /* Strip trailing whitespace (some writers pad with spaces). */
                        int pl = (int)strlen(s0_path);
                        while (pl > 0 && (s0_path[pl-1] == ' ' || s0_path[pl-1] == '\t')) {
                            s0_path[--pl] = 0;
                        }
                    }
                    fclose(f);
                    if (strlen(s0_path) > 0) {
                        /* OSD picks write relative paths; MGL writes
                         * absolute. exFAT doesn't normalize //, so
                         * conditionally prepend /media/fat/ only for
                         * relative paths. */
                        if (s0_path[0] == '/')
                            snprintf(packfile, sizeof(packfile), "%s", s0_path);
                        else
                            snprintf(packfile, sizeof(packfile), "/media/fat/%s", s0_path);
                        fprintf(stderr, "MiSTer: OSD selected: %s\n", packfile);
                        break;
                    }
                }
                usleep(200000);  /* poll every 200ms */
            }
        }
    }
#else
    Menu();
#endif

#ifndef SKIP_CODE
    getPakName(pakname, -1);
    video_set_window_title(pakname);
#endif
#ifdef MISTER_NATIVE_VIDEO
    /* Save loaded path for swap detection and start watcher thread */
    strncpy(mister_loaded_path, packfile, sizeof(mister_loaded_path) - 1);
    /* Baseline the SC1 replay picker to the current .s1 MTIME so a pre-existing/
     * stale .s1 (the pick that armed THIS playback, or a value MiSTer restored on
     * core load) does NOT auto-replay. Only a NEWER .s1 mtime (a fresh OSD pick,
     * even re-picking the same .inp) starts a replay. */
    {
        struct stat s1b;
        mister_replay_mtime = (stat("/media/fat/config/OpenBOR.s1", &s1b) == 0)
                              ? (long)s1b.st_mtime : -1;
    }
    pthread_t swap_tid;
    pthread_create(&swap_tid, NULL, mister_swap_thread, NULL);
#endif

    fprintf(stderr, "MiSTer: entering openborMain()...\n");
    openborMain(argc, argv);
    fprintf(stderr, "MiSTer: openborMain() returned normally\n");

#ifdef MISTER_NATIVE_VIDEO
    mister_swap_requested = 1;
    pthread_join(swap_tid, NULL);
#endif

#ifdef MISTER_NATIVE_VIDEO
    NativeVideoWriter_Shutdown();
    NativeAudioWriter_Shutdown();
#endif

    borExit(0);
    return 0;
}
