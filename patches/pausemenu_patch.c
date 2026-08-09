/*
 * MiSTer_OpenBOR — Custom Pause Menu Patch for OpenBOR Build 7533
 *
 * Replacement for the stock pausemenu() function in openbor.c.
 *
 * REPLACES: The stock 2-item pause menu (Continue / End Game).
 *
 * PAUSE MENU:
 *   Continue   — resumes the game
 *   Options    — submenu: Music Volume / SFX Volume / Back
 *   Recording  — submenu: Record / Play Recording (the MiSTer title-anchored
 *                raw-input recorder), state-aware (shows Stop while active)
 *   Reset Pak  — restarts the PAK from its title screen
 *   Quit       — exits OpenBOR (daemon relaunches, user lands at PAK browser)
 *
 * CONTROLS:
 *   D-pad Up/Down  — navigate
 *   Xbox A (FLAG_JUMP) / Start (FLAG_START) — confirm
 *   X (FLAG_SPECIAL) / ESC — back / close menu
 *   D-pad Left/Right — adjust volume in Options
 *
 * RECORDING (the MiSTer title-anchored raw-input recorder — apply_patches.py):
 *   Record and Play both do a PAK reset (this menu writes /tmp/openbor_recmode
 *   {REC|PLAY} + /tmp/openbor_reset_marker, then exit(0); the daemon respawns and
 *   the PAK reloads to its title). The recorder hook (inputrefresh, right after
 *   control_update) arms on the first frame after respawn and captures/injects the
 *   RAW controller stream (playercontrolpointers[]->keyflags/newkeyflags) — which
 *   runs at the menus AND in-level — so the recording spans your title navigation
 *   THROUGH gameplay, and playback drives the menus into the level hands-free.
 *
 *   FLOW: pause -> Recording -> "Record" restarts the PAK and records everything
 *   you do from the title on. pause -> Recording -> "Stop Recording" writes the
 *   stream to /media/fat/replays/OpenBOR_7533/<pak>_<slot>.inp and resumes play.
 *   pause -> Recording -> "Play Recording" restarts + replays that slot
 *   hands-free; press ANY button to take over.
 *
 *   SLOTS: a BOUNDED library of MREC_SLOTS (8) per PAK, chosen with left/right on
 *   the "Slot N/8" item, exactly like savestates -- so no file browsing is
 *   needed. It replaced an unbounded <pak>_N library whose only access path was
 *   the OSD picker, which starts at the core's games/ home and would have forced
 *   the user to navigate up and out to reach the takes folder. Record OVERWRITES
 *   the chosen slot, which is the point, but never silently: the item shows
 *   "used" or "empty" beforehand and a notice names the slot afterwards. The
 *   chosen slot travels across the reset via /tmp/openbor_recslot, because this
 *   process exits before the take is written. 8 rather than savestates' 4: a take
 *   is an artifact you keep, not scratch you overwrite. The OSD SC1 "Load Replay"
 *   slot still plays an arbitrary file, for takes received from other people.
 *
 *   Header: magic "MREC" + container + engine_ver + build_date + pak_name + seed
 *   + len + crc32, then the identity section, then per-frame interval/keyflags.
 *
 *   DETERMINISM: OpenBOR seeds its RNG only inside the record/replay funcs (no
 *   srand during PAK/level load), so the recorder captures getseed() at Record and
 *   srand32()-restores it at Play — in-level RNG reproduces; menu nav is pure
 *   input so it is deterministic regardless. .inp files are build/arch specific.
 *
 *   🛑 Capture and injection are NOT gated on !_pause -- that gate existed and was
 *   removed (2026-08-01). The pause menu's own frames ARE recorded, and must be,
 *   or a replay reaches a menu the recorded stream has no way to close and hangs
 *   there. Do not reinstate it.
 *
 *   `mrec_mode` (int, openbor.c global; 0=idle 1=rec 2=play) is the single source
 *   of truth this menu reads. Take-over and end-of-stream set it to 0; there is
 *   no menu item that does (see the recmode == 2 branch).
 *
 * RESET PAK / QUIT: unchanged from the prior menu (see the case bodies).
 *
 * Copyright (C) 2026 MiSTer Organize — GPL-3.0
 */

extern int mrec_mode;  /* MiSTer raw-input recorder: 0=idle, 1=recording, 2=playing */
extern int mrec_slot;  /* 1..MREC_SLOTS bounded take slot, chosen on the idle
                        * submenu. Defined in openbor.c beside mrec_mode, along
                        * with MREC_SLOTS, both of which precede this function in
                        * the spliced file. */
/* A9: the pause menu is composited in DISPLAY space, not at the PAK's native
 * resolution. Drawn engine-side it went through WriteFrame's downscale with the
 * game image -- on He-Man (960x480 -> 320x224) the menu came out about a third
 * of its intended size. Same mistake as drawing the fps read-out on the vscreen.
 * The menu renders into its own display-sized surface and is handed to the
 * writer, which publishes it instead of downscaling. See native_video_writer.h.
 *
 * 🛑 Nothing above the function signature below survives: replace_function
 * splices this file from that signature ONWARD, so every extern in this block
 * is silently discarded. A9's three functions are declared at FUNCTION SCOPE
 * inside the body instead. The dry-run still exits 0 and the integrity gate
 * still reads 22/22 when a declaration goes missing, because neither inspects
 * the emitted C -- so grep the EMITTED openbor.c to prove it arrived.
 *
 * 🛑 And do NOT write the function's signature in a comment here. The splice
 * searches for that exact text, so a copy inside a comment becomes a FALSE
 * MARKER and the splice starts mid-sentence: the rest of the comment lands in
 * the output as bare code. That is not hypothetical -- this very comment used
 * to quote the signature while explaining the trap, and it broke the build
 * with "stray '`' in program". */

extern int mister_fps_overlay;  /* Options -> FPS Display. Drawn by
                                 * native_video_writer.c POST-downscale, into the
                                 * final 320x224 buffer, so it stays crisp on PAKs
                                 * that render at 960x480 and get squished 3x. */

void pausemenu()
{
    int pauselector = 0;
    int option_selector = 0;
    int rec_selector = 0;
    int in_options = 0;
    int in_recording = 0;
    int in_quitconfirm = 0, quit_selector = 0;
    int quit = 0;
    int controlp = 0, i;
    int newkeys;
    /* Hold-to-repeat for left/right (slot row, volume rows), so crossing 8
     * slots or a volume gauge does not need a tap per step.
     *
     * 🛑 MILLISECONDS, not loop iterations. This loop is UNCAPPED and runs
     * >400 iterations/sec, so an iteration-counted "15 delay, every 4th"
     * fired after ~37 ms and then ~100 times a second: one tap fast-
     * scrolled the whole row (reported on hardware). PICO-8 feels right
     * only because btnp() counts real 60 Hz frames. Same mistake as
     * counting a notice's lifetime in frames -- and the same fix, for the
     * same reason: a human-facing interval belongs on a wall clock.
     *
     * 250 ms then every 66 ms == PICO-8's 15-frame delay and 4-frame rate
     * at 60 Hz, so the two cores feel identical.
     *
     * Locals: this is a modal loop, so they cannot leak into the next
     * visit to the menu. */
    int rep_held = 0, lrkeys = 0;
    unsigned int rep_t0 = 0, rep_next = 0;
    char volbuf[64];
    s_set_entry *set = levelsets + current_set;
    /* A9 display-space overlay, defined in native_video_writer.c.
     *
     * 🛑 Declared at FUNCTION SCOPE on purpose. Two file-scope options both
     * fail: above this function they are discarded (replace_function splices
     * this file from the signature onward -- and note this comment does not
     * quote it, for the reason given above), and apply_patches.py's
     * openbor.c prelude lands AFTER pausemenu, so a declaration there does not
     * cover these call sites either -- which is why the pre-existing
     * NativeVideoWriter_Notice() calls below rely on implicit declaration. That
     * happens to work on ARM32 (pointer + int promote to the same ABI) but is
     * illegal C99 and one strict compiler away from breaking. Declaring here is
     * guaranteed to precede use. Anything new pausemenu calls should do this. */
    extern void NativeVideoWriter_SetOverlay(const void *pixels);
    extern void NativeVideoWriter_CaptureDisplay(void *dst);
    extern void NativeVideoWriter_GetDisplaySize(int *w, int *h);
    /* Notice() had the same problem and has had it since the recorder landed:
     * its only declaration is in that same too-late prelude, so every call
     * below resolved implicitly. It happened to work -- const char* + int
     * promote to the identical ARM32 calling convention and the return value is
     * discarded -- but "compiles to the right thing by luck" is not a property
     * to keep relying on. Declared properly now. */
    extern void NativeVideoWriter_Notice(const char *msg, int seconds);

    /* NULL menuscreen = fall back to the pre-A9 engine-space path. */
    s_screen *menuscreen = NULL;
    void     *menubg     = NULL;
    int       disp_w = 0, disp_h = 0;
    size_t    disp_bytes = 0;
    int       saved_hres = 0, saved_vres = 0;
    /* MiSTer Path B: match the 16-bit vscreen so copyscreen(pausebuffer,
     * vscreen) is not a no-op (copyscreen early-returns on format mismatch). */
    s_screen *pausebuffer = allocscreen(videomodes.hRes, videomodes.vRes, PIXEL_16);

    /* allocscreen returns NULL on malloc failure and copyscreen dereferences
     * dest->pixelformat immediately -- so an unchecked failure here is a
     * SIGSEGV. It is not a trivial allocation: a 960x480 PAK needs ~900 KB
     * CONTIGUOUS, requested fresh on every pause. And it is worst exactly when
     * it matters most: the recorder may be holding 70-140 MB and has just
     * fragmented the heap with doubling reallocs, and the pause menu is the
     * ONLY way to reach Stop Recording. Crashing here would lose the take.
     * Bail out cleanly instead -- the game simply stays unpaused. */
    if(!pausebuffer)
    {
        printf("[PAUSE] out of memory for the pause buffer -- not opening the menu\n");
        return;
    }

    /* A9: display-space surface for the menu, plus a pristine copy of the
     * background to rebuild it from each frame.
     *
     * The background is captured ONCE, here. Not per frame: the moment an
     * overlay is set, CaptureDisplay returns the overlay itself, so a per-frame
     * capture would draw the menu on top of the menu and smear it.
     *
     * Both allocations are optional. If either fails the menu falls back to the
     * pre-A9 engine-space path verbatim -- squished on a hi-res PAK, but
     * present. A pause menu that fails to open is far worse than one that is
     * small, because it is the only route to Stop Recording. */
    NativeVideoWriter_GetDisplaySize(&disp_w, &disp_h);
    disp_bytes = (size_t)disp_w * (size_t)disp_h * 2;
    menuscreen = allocscreen(disp_w, disp_h, PIXEL_16);
    menubg     = menuscreen ? malloc(disp_bytes) : NULL;
    if(menuscreen && !menubg)
    {
        freescreen(&menuscreen);
        menuscreen = NULL;
    }
    if(menuscreen)
    {
        NativeVideoWriter_CaptureDisplay(menubg);
        saved_hres = videomodes.hRes;
        saved_vres = videomodes.vRes;
    }
    else
    {
        printf("[PAUSE] no display-space surface -- menu falls back to engine space\n");
    }

    /* NOTE: this menu runs NORMALLY during playback -- there is deliberately no
     * mrec_mode == 2 early-return here.
     *
     * One was added and then removed (2026-08-01). It refused to open the menu
     * on an injected pause press, to stop a replay hanging inside a modal loop
     * the recorded stream could not close. But the engine pauses audio BEFORE
     * calling us --
     *     sound_pause_music(1); sound_pause_sample(1); pausemenu();
     * -- and it is THIS function's exit that calls sound_pause_music(0). An
     * early return therefore left the engine's audio paused for the rest of the
     * run: the user heard the pause sound, saw no menu, and then lost all
     * gameplay audio.
     *
     * The hang it was working around had a different cause: capture and inject
     * used to be gated on !_pause, so the menu's own frames were never recorded
     * and the stream had nothing able to close the menu. That gate is gone, so
     * the menu frames are recorded and replayed like any others -- the recorded
     * navigation drives this loop and the recorded Continue exits it, restoring
     * audio on the way out exactly as it did live. */

    copyscreen(pausebuffer, vscreen);
    spriteq_draw(pausebuffer, 0, MIN_INT, MAX_INT, 0, 0);
    spriteq_clear();
    /* 🛑 On the overlay path the frozen background comes from menubg, which is
     * ALREADY display-space. pausebuffer is PAK-native, so leaving it queued
     * would have spriteq_draw blit a 960x480 image into the 320x224 surface.
     * The queue then carries menu text only, which is exactly what we want to
     * render into the overlay each frame. */
    if(!menuscreen)
    {
        spriteq_add_screen(0, 0, MIN_INT, pausebuffer, NULL, 0);
    }
    spriteq_lock();

    for(i = 0; i < set->maxplayers; i++)
    {
        if(player[i].ent && (player[i].newkeys & FLAG_START))
        {
            controlp = i;
            break;
        }
    }

    sound_pause_music(1);
    sound_pause_sample(1);
    _pause = 2;
    bothnewkeys = 0;

    /* NOTE: the press that opened this menu is deliberately LEFT IN the
     * recording. An earlier fix dropped it -- reasoning that the engine does
     * not tick while paused, so the pause could be elided -- but that is only
     * true of the PAUSED frames. pausemenu() is called at the END of update(),
     * after inputrefresh() and after the while(_time < newtime) tick loop, so
     * the frame that opens this menu ticks in FULL and only then lands here.
     * Dropping it left the replay one input sample short and desynced
     * everything after the pause (user-reported 2026-08-01).
     * Nothing extra is needed: the mrec_mode == 2 guard above keeps a replay
     * out of this menu, so the frame replays as a normal ticking frame and the
     * next recorded frame follows -- correct, because the paused frames
     * contributed no ticks and were never captured in the first place. */

    while(!quit)
    {
        int recmode = mrec_mode;   /* 0=idle, 1=recording, 2=playing */
        /* The slot picker is on the IDLE list only, so the shape a recorded run
         * ever sees stays small and stable -- the invariant that keeps injected
         * navigation landing on the same items on playback.
         *
         * Playback is 1 item (Back), recording is 2 (Stop Recording, Back).
         * 🛑 There is deliberately NO "Stop Playback" -- see the recmode == 2
         * branch below. The counts differing across those two modes is safe:
         * the only recorded input that ever selects index 0 in this submenu is
         * the terminal Stop Recording that ended the take, and by the frame
         * after it _mr_pos >= _mr_len, so the replay ends either way. */
        int rec_items = (recmode == 0) ? 4 : (recmode == 2) ? 1 : 2;

        /* 🛑 A9: the centring macros read videomodes, not the target surface --
         *     _strmidx  centres on videomodes.hRes
         *     _liney    centres on videomodes.vRes
         * so text aimed at a 320x224 surface while videomodes still says
         * 960x480 lands at x~480: entirely off it. Point them at display space
         * for the queuing region below, and restore before update(1,0), which
         * renders for real and must see the true mode.
         *
         * The region is contiguous -- every _menutextmshift call sits between
         * here and that update() -- and contains only text queuing and
         * snprintf, nothing else that reads videomodes. */
        if(menuscreen)
        {
            videomodes.hRes = disp_w;
            videomodes.vRes = disp_h;
        }

        if(in_recording)
        {
            /* -- Recording submenu (state-aware) -- */
            _menutextmshift(pauseoffset[4], -3, 0, pauseoffset[5], pauseoffset[6], Tr("Recording"));

            if(recmode == 1)
            {
                _menutextmshift((rec_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], Tr("Stop Recording"));
                _menutextmshift((rec_selector == 1)?pauseoffset[1]:pauseoffset[0],  1, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
                _menutextmshift(pauseoffset[0], 3, 0, pauseoffset[2], pauseoffset[3], Tr("Recording..."));
            }
            else if(recmode == 2)
            {
                /* 🛑 NO "Stop Playback" ITEM. Do not add one back.
                 *
                 * A live player can never reach this branch: pressing pause
                 * during a replay trips take-over, mrec_mode drops to 0, and the
                 * menu draws its IDLE list instead. Taking over IS how a live
                 * pause reaches the engine at all -- see the injector note in
                 * apply_patches.py. So an item here is visible during a replay
                 * and can never be chosen by the person watching it, which is
                 * the definition of dead UI.
                 *
                 * It also is not needed to end a replay: take-over ends it on
                 * any button, and it ends itself at _mr_pos >= _mr_len. */
                _menutextmshift(pauseoffset[1], -1, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
                _menutextmshift(pauseoffset[0], 3, 0, pauseoffset[2], pauseoffset[3], Tr("Replaying..."));
            }
            else
            {
                /* Slot label shows whether the slot is occupied, so Record never
                 * surprises you: "Slot 3 (empty)" vs "Slot 3 (used)". Overwriting
                 * is the point of a bounded library, but it should be a choice you
                 * can see before you make it, not something you discover after. */
                char _sl[64]; char _sp[MAX_BUFFER_LEN]; char _sn[MAX_BUFFER_LEN]; FILE *_st;
                if(mrec_slot < 1 || mrec_slot > MREC_SLOTS) mrec_slot = 1;
                mrec_content_id(_sn, sizeof(_sn));
                snprintf(_sp, sizeof(_sp), "/media/fat/replays/OpenBOR_7533/%s_%d.inp", _sn, mrec_slot);
                _st = fopen(_sp, "rb");
                snprintf(_sl, sizeof(_sl), "Slot %d/%d %s", mrec_slot, MREC_SLOTS, _st ? "used" : "empty");
                if(_st) fclose(_st);

                /* Slot FIRST, matching PICO-8 (2026-08-07 parity decision).
                 * The cursor opens on index 0, so with Record there a single
                 * confirm press reset the PAK and started a recording -- while
                 * the same press on PICO-8 did nothing. Landing on the harmless
                 * row is the better default, so this core moved rather than the
                 * other one. */
                _menutextmshift((rec_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], _sl);
                _menutextmshift((rec_selector == 1)?pauseoffset[1]:pauseoffset[0],  0, 0, pauseoffset[2], pauseoffset[3], Tr("Record"));
                _menutextmshift((rec_selector == 2)?pauseoffset[1]:pauseoffset[0],  1, 0, pauseoffset[2], pauseoffset[3], Tr("Play Recording"));
                _menutextmshift((rec_selector == 3)?pauseoffset[1]:pauseoffset[0],  3, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
            }
        }
        else if(in_quitconfirm)
        {
            /* Quit would discard a recording in progress, and this core
             * exit(0)s -- the process is gone before a frame publishes, so
             * a notice afterwards is impossible. Ask first. Cursor starts
             * on Back: the destructive choice should never be the one a
             * stray confirm press lands on. */
            /* 🛑 TWO ROWS, ALWAYS, in this order, in every recorder mode.
             * A replay injects presses by POSITION. This confirm used to
             * appear only while recording, so replaying a run in which the
             * user chose Quit and then Back sent that second press to Quit
             * itself and exited to a black screen -- reported on hardware.
             * Same rule as the Recording submenu: the shape is fixed, only
             * the title below is allowed to change. */
            _menutextmshift(pauseoffset[4], -3, 0, pauseoffset[5], pauseoffset[6],
                            mrec_mode ? Tr("Lose recording") : Tr("Are you sure"));
            /* 🛑 LETTERS AND DIGITS ONLY in menu text. The PAK supplies the
             * font as a 16x16 sheet, and a glyph it lacks renders as a solid
             * box -- a quit label ending in a question mark rendered with a
             * green block after its last letter on hardware. Every string
             * this menu has ever shipped uses letters,
             * digits and : / %% ; treat anything else as unavailable. Same
             * kind of constraint as the 14-char width cap, and just as
             * invisible until a real PAK draws it. */
            _menutextmshift((quit_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
            _menutextmshift((quit_selector == 1)?pauseoffset[1]:pauseoffset[0],  0, 0, pauseoffset[2], pauseoffset[3], Tr("Quit"));
        }
        else if(!in_options)
        {
            /* -- Main pause menu: Continue / Options / Recording / Reset Pak / Quit -- */
            _menutextmshift(pauseoffset[4], -3, 0, pauseoffset[5], pauseoffset[6], Tr("Pause"));
            _menutextmshift((pauselector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], Tr("Continue"));
            _menutextmshift((pauselector == 1)?pauseoffset[1]:pauseoffset[0],  0, 0, pauseoffset[2], pauseoffset[3], Tr("Options"));
            _menutextmshift((pauselector == 2)?pauseoffset[1]:pauseoffset[0],  1, 0, pauseoffset[2], pauseoffset[3], Tr("Recording"));
            _menutextmshift((pauselector == 3)?pauseoffset[1]:pauseoffset[0],  2, 0, pauseoffset[2], pauseoffset[3], Tr("Reset Pak"));
            _menutextmshift((pauselector == 4)?pauseoffset[1]:pauseoffset[0],  3, 0, pauseoffset[2], pauseoffset[3], Tr("Quit"));
        }
        else
        {
            /* -- Options submenu: Music Volume / SFX Volume / Back -- */
            _menutextmshift(pauseoffset[4], -3, 0, pauseoffset[5], pauseoffset[6], Tr("Options"));

            snprintf(volbuf, sizeof(volbuf), "Music: %ld", (long)savedata.musicvol);
            _menutextmshift((option_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], volbuf);

            snprintf(volbuf, sizeof(volbuf), "SFX: %ld", (long)savedata.effectvol);
            _menutextmshift((option_selector == 1)?pauseoffset[1]:pauseoffset[0],  0, 0, pauseoffset[2], pauseoffset[3], volbuf);

            snprintf(volbuf, sizeof(volbuf), "FPS: %s", mister_fps_overlay ? "On" : "Off");
            _menutextmshift((option_selector == 2)?pauseoffset[1]:pauseoffset[0],  1, 0, pauseoffset[2], pauseoffset[3], volbuf);

            _menutextmshift((option_selector == 3)?pauseoffset[1]:pauseoffset[0],  3, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
        }

        /* Render the queued menu text into the display-space surface, on top of
         * the pristine background, and publish that as the overlay.
         *
         * Rebuilt from menubg every frame rather than drawn once: the selector
         * moves, volumes change, the slot row updates. That is fine and is NOT
         * the notice's repaint race -- this surface is private memory, not the
         * buffer the FPGA is scanning. WriteFrame copies it into the back buffer
         * as a whole frame, so what the FPGA sees is still only ever a complete
         * publish. (And that copy starts at nv_top, so it cannot rewrite a live
         * notice band.)
         *
         * videomodes is restored BEFORE update(1,0): that call renders for real
         * and every path inside it must see the true PAK resolution. */
        if(menuscreen)
        {
            memcpy(menuscreen->data, menubg, disp_bytes);
            spriteq_draw(menuscreen, 0, MIN_INT, MAX_INT, 0, 0);
            spriteq_clear();
            videomodes.hRes = saved_hres;
            videomodes.vRes = saved_vres;
            NativeVideoWriter_SetOverlay(menuscreen->data);
        }

        update(1, 0);

        newkeys = player[controlp].newkeys;
        {   /* lrkeys = a fresh press, OR a held direction that has passed
             * the delay and landed on a repeat frame. Only left/right
             * repeat: up/down through a 4-item list does not need it, and
             * confirm/back must never fire twice from one press. */
            int held = (int)(player[controlp].keys & (FLAG_MOVELEFT | FLAG_MOVERIGHT));
            int fire = 0;
            unsigned int now = timer_gettick();
            if(held && held == rep_held)
            {
                if(now - rep_t0 >= 250u && now >= rep_next)
                {
                    fire = held;
                    rep_next = now + 66u;
                }
            }
            else { rep_held = held; rep_t0 = now; rep_next = now + 250u; }
            lrkeys = newkeys | fire;
        }

        if(in_quitconfirm)
        {
            if(newkeys & (FLAG_MOVEUP | FLAG_MOVEDOWN))
            {
                quit_selector = !quit_selector;
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }
            if(newkeys & (FLAG_JUMP | FLAG_START))
            {
                sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);
                if(quit_selector == 1)
                {
                    remove("/tmp/openbor_current.pak");
                    remove("/media/fat/config/OpenBOR.s0");
                    exit(0);
                }
                in_quitconfirm = 0;
                pauselector = 4;   /* back on the Quit row */
            }
            if(newkeys & (FLAG_SPECIAL | FLAG_ESC))
            {
                sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);
                in_quitconfirm = 0;
                pauselector = 4;
            }
        }
        else if(in_recording)
        {
            /* -- Recording submenu input handling -- */
            if(newkeys & FLAG_MOVEUP)
            {
                rec_selector = (rec_selector + rec_items - 1) % rec_items;
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }
            if(newkeys & FLAG_MOVEDOWN)
            {
                rec_selector = (rec_selector + 1) % rec_items;
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }
            /* Left/right adjust the slot. Cycling forward with JUMP alone would
             * mean up to seven presses to step back one, which is why savestate
             * UIs use a two-directional control. JUMP still cycles forward, so
             * the item is usable on a pad with no working left/right. */
            if(recmode == 0 && rec_selector == 0)
            {
                if(lrkeys & FLAG_MOVELEFT)
                {
                    mrec_slot = (mrec_slot <= 1) ? MREC_SLOTS : mrec_slot - 1;
                    sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
                }
                if(lrkeys & FLAG_MOVERIGHT)
                {
                    mrec_slot = (mrec_slot >= MREC_SLOTS) ? 1 : mrec_slot + 1;
                    sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
                }
            }

            if(newkeys & (FLAG_JUMP | FLAG_START))
            {
                sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);

                if(recmode == 0)
                {
                    if(rec_selector == 1)   /* Record */
                    {
                        /* Title-anchored: queue a RECORD marker + Reset-Pak restart.
                         * On respawn the recorder (openbor.c) arms at the first frame
                         * (the PAK title) and captures your raw controller stream from
                         * the title through gameplay. The marker survives the daemon
                         * respawn. Play replays that whole stream hands-free. */
                        {
                            FILE *_rm = fopen("/tmp/openbor_recmode", "w");
                            if(_rm) { fputs("REC", _rm); fclose(_rm); }
                            /* Carry the slot: this process is about to exit, so a
                             * choice held only in memory would be lost and every
                             * take would land in the fallback slot. */
                            _rm = fopen("/tmp/openbor_recslot", "w");
                            if(_rm) { fprintf(_rm, "%d", mrec_slot); fclose(_rm); }
                            _rm = fopen("/tmp/openbor_reset_marker", "w");
                            if(_rm) fclose(_rm);
                        }
                        exit(0);
                    }
                    else if(rec_selector == 2)  /* Play Recording */
                    {
                        /* Same restart: the recorder loads the saved stream + restores
                         * the RNG seed, then drives the menus into the level and plays
                         * back hands-free. Press any button to take control. */
                        {
                            FILE *_rm;
                            int _arm = 1;   /* cleared by any refusal below; see the
                                             * "MUST NOT break" note further down */
                            /* Resolve WHICH take now, at selection time, and hand the
                             * path to the handler. The handler has to restore that
                             * take's save snapshot before the binary starts -- the
                             * engine reads <pak>.sav during startup, long before the
                             * recorder arms and could work out the filename itself.
                             * The OSD "Load Replay" path already writes this file;
                             * writing it here too means both routes look identical to
                             * the handler. */
                            char _pb[MAX_BUFFER_LEN], _pn[MAX_BUFFER_LEN];
                            int _pmx = 0;
                            strcpy(_pb, "/media/fat/replays/OpenBOR_7533/");
                            mrec_content_id(_pn, MAX_BUFFER_LEN);
                            strcat(_pb, _pn);
                            /* ENUMERATE, never probe. This used to walk _1.._9999 and
                             * give up after 8 consecutive misses, so a take received
                             * from someone else and dropped into an empty Replays/ --
                             * say FinalFight_37.inp -- was invisible here, and the next
                             * Record wrote _1 and shadowed it. The engine side already
                             * enumerates with readdir; this was the last consumer still
                             * probing, which meant the two disagreed about which take
                             * "the highest" is. */
                            /* Honour the PICKED slot. mrec_highest() is the
                             * fallback for when nothing has been chosen -- it was
                             * being used UNCONDITIONALLY, which made the picker
                             * decorative: with only slot 1 occupied, choosing
                             * slot 2 and pressing Play opened slot 1 (user-reported
                             * 2026-08-04). Record already carried mrec_slot across
                             * the reset; Play never read it at all.
                             *
                             * The row-draw above normalises mrec_slot into
                             * 1..MREC_SLOTS before this item can be reached, so in
                             * practice the fallback only covers a Play arriving by
                             * some path that never drew the row. */
                            _pmx = (mrec_slot >= 1 && mrec_slot <= MREC_SLOTS)
                                       ? mrec_slot : mrec_highest(_pn);
                            /* 🛑 REFUSING TO ARM MUST NOT `break`.
                             *
                             * These two refusal paths used to break, with a
                             * comment claiming they left you in the menu. They
                             * did not: the nearest enclosing loop is the menu's
                             * own `while(!quit)`, so break EXITED the pause menu
                             * -- and the post-loop cleanup (_pause/spriteq/
                             * freescreen) does NOT resume audio. The
                             * sound_pause_music(0)/sound_pause_sample(0) pair
                             * lives only inside the deliberate menu actions, so
                             * leaving by any other route left music paused for
                             * the rest of the session while sound effects kept
                             * playing -- exactly as reported 2026-08-04 after
                             * picking an empty slot.
                             *
                             * A flag instead. The menu loop continues normally,
                             * the notice is on screen to say why, and the audio
                             * pairing is never broken because we never leave by
                             * a side door. */
                            /* Also require the LIBRARY to be non-empty. mrec_slot is
                             * normalised to >=1 on every row draw (see :339), so _pmx > 0
                             * alone was always true and the "No recording for this game"
                             * else below was unreachable -- a PAK with no takes at all
                             * reported "Slot 1 is empty". True, but not the useful answer.
                             * The engine-side copy of this was fixed the same way. */
                            if(_pmx > 0 && mrec_highest(_pn) > 0)
                            {
                                char _pp[MAX_BUFFER_LEN]; char _pw[128];
                                sprintf(_pp, "%s_%d.inp", _pb, _pmx);
                                /* An empty PICKED slot must say so and stay put.
                                 * Arming anyway would reset the PAK and then
                                 * report "no recording for this PAK" from the next
                                 * process -- both a wasted reset and, whenever some
                                 * OTHER slot does hold a take, untrue. */
                                {
                                    FILE *_pf = fopen(_pp, "rb");
                                    if(!_pf)
                                    {
                                        char _pe[64];
                                        snprintf(_pe, sizeof(_pe), "Slot %d is empty", _pmx);
                                        printf("[REPLAY] slot %d is empty -- not arming\n", _pmx);
                                        NativeVideoWriter_Notice(_pe, 4);
                                        _arm = 0;
                                    }
                                    else fclose(_pf);
                                }
                                /* Validate BEFORE the reset. This used to arm and
                                 * exit(0) unconditionally, so a damaged take -- or
                                 * one for a PAK that has since been modified --
                                 * cost the user their session and explained itself
                                 * only in the next process. Refusing here leaves
                                 * the pause menu on screen to carry the reason. */
                                if(_arm && !mrec_probe_take(_pp, _pw, (int)sizeof(_pw)))
                                {
                                    printf("[REPLAY] not arming: %s\n", _pw);
                                    NativeVideoWriter_Notice(_pw, 6);
                                    _arm = 0;
                                }
                                if(_arm)
                                {
                                    _rm = fopen("/tmp/openbor_playfile", "w");
                                    if(_rm) { fputs(_pp, _rm); fclose(_rm); }
                                }
                            }
                            else
                            {
                                /* Nothing anywhere in this PAK's library. Say so
                                 * here rather than resetting to find out. */
                                printf("[REPLAY] no recording for this PAK -- not arming\n");
                                NativeVideoWriter_Notice("No recording for this game", 4);
                                _arm = 0;
                            }
                            if(_arm)
                            {
                                _rm = fopen("/tmp/openbor_recmode", "w");
                                if(_rm) { fputs("PLAY", _rm); fclose(_rm); }
                                _rm = fopen("/tmp/openbor_reset_marker", "w");
                                if(_rm) fclose(_rm);
                                exit(0);
                            }
                        }
                    }
                    else if(rec_selector == 0)
                    {
                        /* Slot row: chosen with LEFT/RIGHT. Confirm deliberately
                         * does nothing.
                         *
                         * It used to fall through to the Back branch below --
                         * the branch was a bare `else`, so it swallowed the slot
                         * row as well as Back. Pressing the confirm button on
                         * the slot you had just picked therefore closed the
                         * submenu, which reads as the button REJECTING the
                         * choice (user-reported 2026-08-04). Doing nothing is
                         * the honest response: the row is already showing the
                         * chosen slot, and Record/Play are what act on it.
                         *
                         * Kept as an explicit index rather than restoring the
                         * bare `else`, so any item added between the slot row
                         * and Back cannot silently inherit Back's behaviour the
                         * same way. */
                        (void)0;
                    }
                    else if(rec_selector == 3)   /* Back */
                    {
                        in_recording = 0;
                        pauselector = 2;   /* highlight the Recording entry */
                    }
                }
                else if(recmode == 1)   /* recording: Stop Recording / Back */
                {
                    if(rec_selector == 0)   /* Stop Recording */
                    {   /* Signal the recorder to flush the .inp, then return to
                         * the MAIN pause menu -- matching PICO-8 (2026-08-07
                         * parity decision). It used to close the menu and resume
                         * play, so the two cores ended a take in different
                         * places. Staying open lets the user see the slot row
                         * flip to "used" and play it straight back, and the menu
                         * remains the one place recording is controlled from.
                         *
                         * Audio stays paused BECAUSE the menu stays open: the
                         * resume calls belong to the paths that actually leave,
                         * and calling them here would unpause the game under an
                         * open menu. */
                        FILE *_rs = fopen("/tmp/openbor_recstop", "w");
                        if(_rs) fclose(_rs);
                        in_recording = 0;
                        pauselector = 2;   /* highlight the Recording entry */
                    }
                    else   /* Back */
                    {
                        in_recording = 0;
                        pauselector = 2;
                    }
                }
                else   /* playing: Back is the only item -- no Stop Playback, see above */
                {
                    in_recording = 0;
                    pauselector = 2;
                }
            }

            if(newkeys & (FLAG_SPECIAL | FLAG_ESC))
            {
                in_recording = 0;
                pauselector = 2;
                sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);
            }
        }
        else if(!in_options)
        {
            /* -- Main pause menu input handling (5 entries) -- */
            if(newkeys & FLAG_MOVEUP)
            {
                pauselector = (pauselector + 4) % 5;
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }
            if(newkeys & FLAG_MOVEDOWN)
            {
                pauselector = (pauselector + 1) % 5;
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            if(newkeys & (FLAG_JUMP | FLAG_START))
            {
                sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);
                switch(pauselector)
                {
                case 0:  /* Continue */
                    quit = 1;
                    sound_pause_music(0);
                    sound_pause_sample(0);
                    break;

                case 1:  /* Options */
                    in_options = 1;
                    option_selector = 0;
                    break;

                case 2:  /* Recording */
                    in_recording = 1;
                    rec_selector = 0;
                    break;

                case 3:  /* Reset Pak -- write marker so _handler.sh keeps .s0,
                          * then exit; the daemon relaunch re-mounts the PAK. */
                    {
                        FILE *_m;
                        /* Reset RESTARTS a recording rather than silently
                         * ending it. Previously only the reset marker was
                         * written, so the respawn found no recmode marker and
                         * came up idle: the take vanished with no warning and
                         * no log line. The bad case is not losing it -- it is
                         * not NOTICING, and playing on believing you are still
                         * recording. A recording is title-anchored, so it
                         * always begins at the PAK's start, and Reset returns
                         * you to exactly that point: "Reset while recording"
                         * is therefore almost always "let me do that run
                         * again", and re-arming gives a clean re-take.
                         * Playback is deliberately NOT re-armed -- Reset during
                         * a replay is a manual intervention, and take-over
                         * already covers stopping one. */
                        if (mrec_mode == 1)
                        {
                            _m = fopen("/tmp/openbor_recmode", "w");
                            if (_m) { fputs("REC", _m); fclose(_m); }
                            /* Carry the slot, exactly as Record does above.
                             * Without this the re-armed take fell back to the
                             * default slot, so Stop Recording wrote a slot the
                             * user never chose and destroyed whatever was in it:
                             * recorded to slot 2, Reset, Stop -> overwrote slot 1.
                             * Reported on hardware 2026-08-07; PICO-8 had the
                             * same omission on the same path. */
                            if(mrec_slot < 1 || mrec_slot > MREC_SLOTS) mrec_slot = 1;
                            _m = fopen("/tmp/openbor_recslot", "w");
                            if (_m) { fprintf(_m, "%d", mrec_slot); fclose(_m); }
                            printf("[REC] reset while recording -- restarting the take in slot %d\n",
                                   mrec_slot);
                        }
                        else if (mrec_mode == 2)
                        {
                            printf("[REC] reset during playback -- playback ended\n");
                        }
                        _m = fopen("/tmp/openbor_reset_marker", "w");
                        if (_m) fclose(_m);
                    }
                    exit(0);
                    break;

                case 4:  /* Quit -- delete .s0 + cache so the relaunch shows the
                          * OSD PAK browser. */
                    /* ALWAYS confirm -- see the confirm branch for why this
                     * must not depend on mrec_mode. */
                    in_quitconfirm = 1;
                    quit_selector = 0;
                    break;
                    remove("/tmp/openbor_current.pak");
                    remove("/media/fat/config/OpenBOR.s0");
                    /* .s1 intentionally NOT removed — the binary baselines .s1's
                     * mtime at startup, so a stale .s1 never auto-replays and a
                     * fresh OSD pick (newer mtime) always triggers. */
                    exit(0);
                    break;
                }
            }

            if(newkeys & (FLAG_SPECIAL | FLAG_ESC))
            {
                quit = 1;
                sound_pause_music(0);
                sound_pause_sample(0);
                sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);
            }
        }
        else
        {
            /* -- Options submenu input handling -- */
            if(newkeys & FLAG_MOVEUP)
            {
                option_selector = (option_selector + 3) % 4;
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }
            if(newkeys & FLAG_MOVEDOWN)
            {
                option_selector = (option_selector + 1) % 4;
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            if(lrkeys & FLAG_MOVELEFT)
            {
                if(option_selector == 0 && savedata.musicvol >= 10)
                {
                    savedata.musicvol -= 10;
                    sound_volume_music(savedata.musicvol, savedata.musicvol);
                }
                else if(option_selector == 1 && savedata.effectvol >= 10)
                {
                    savedata.effectvol -= 10;
                }
                else if(option_selector == 2)
                {
                    mister_fps_overlay = 0;
                }
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            if(lrkeys & FLAG_MOVERIGHT)
            {
                if(option_selector == 0 && savedata.musicvol <= 90)
                {
                    savedata.musicvol += 10;
                    sound_volume_music(savedata.musicvol, savedata.musicvol);
                }
                else if(option_selector == 1 && savedata.effectvol <= 110)
                {
                    savedata.effectvol += 10;
                }
                else if(option_selector == 2)
                {
                    mister_fps_overlay = 1;
                }
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            if(newkeys & (FLAG_JUMP | FLAG_START))
            {
                if(option_selector == 2)  /* FPS Display -- toggle */
                {
                    mister_fps_overlay = !mister_fps_overlay;
                    sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
                }
                else if(option_selector == 3)  /* Back */
                {
                    in_options = 0;
                    pauselector = 1;
                    sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);
                }
            }

            if(newkeys & (FLAG_SPECIAL | FLAG_ESC))
            {
                in_options = 0;
                pauselector = 1;
                sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);
            }
        }
    }

    _pause = 0;
    bothnewkeys = 0;

    /* 🛑 Clear the overlay BEFORE freeing the surface it points at. The writer
     * borrows the pointer -- it does not copy it -- and WriteFrame runs from the
     * engine thread on the very next frame, so freeing first leaves it reading
     * freed memory for exactly one frame. That is the kind of use-after-free
     * that shows as an intermittent garbled frame and gets blamed on the FPGA.
     *
     * Restore videomodes unconditionally too. Every loop exit already restores
     * it before update(1,0), so this is belt and braces -- but leaving the
     * engine running at 320x224 would mis-size every subsequent draw, and it is
     * one assignment to be certain. */
    NativeVideoWriter_SetOverlay(NULL);
    if(menuscreen)
    {
        videomodes.hRes = saved_hres;
        videomodes.vRes = saved_vres;
        freescreen(&menuscreen);
    }
    if(menubg)
    {
        free(menubg);
        menubg = NULL;
    }

    spriteq_unlock();
    spriteq_clear();
    freescreen(&pausebuffer);
}
