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
 *   stream to /media/fat/games/OpenBOR/Replays/<pak>_N.inp (numbered library --
 *   never overwrites; magic "MREC4" + engine_ver + build_date + pak_name + seed
 *   + len + per-frame interval/keyflags) and resumes play. pause -> Recording ->
 *   "Play Recording" restarts + replays the highest-numbered take hands-free;
 *   press ANY button to take over. The OSD SC1 "Load Replay" slot plays any file.
 *
 *   DETERMINISM: OpenBOR seeds its RNG only inside the record/replay funcs (no
 *   srand during PAK/level load), so the recorder captures getseed() at Record and
 *   srand32()-restores it at Play — in-level RNG reproduces; menu nav is pure
 *   input so it is deterministic regardless. Recording/injection are gated on
 *   !_pause, so the pause menu itself is never recorded. .inp files are build/arch
 *   specific.
 *
 *   `mrec_mode` (int, openbor.c global; 0=idle 1=rec 2=play) is the single source
 *   of truth this menu reads. Stop Playback sets it to 0 directly.
 *
 * RESET PAK / QUIT: unchanged from the prior menu (see the case bodies).
 *
 * Copyright (C) 2026 MiSTer Organize — GPL-3.0
 */

extern int mrec_mode;  /* MiSTer raw-input recorder: 0=idle, 1=recording, 2=playing */
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
    int quit = 0;
    int controlp = 0, i;
    int newkeys;
    char volbuf[64];
    s_set_entry *set = levelsets + current_set;
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

    /* A pause press can only reach here DURING PLAYBACK if it was INJECTED: a
     * live press is caught by the recorder's take-over first, which ends
     * playback before the engine acts on the press. And an injected one must be
     * refused -- while this menu is open the recorder captures and injects
     * NOTHING (update(1,0) below does still call inputrefresh every iteration,
     * but both the capture and the inject are gated on !_pause and _pause is 2
     * in here), so the replay would hang with nothing in the stream able to
     * close it. Recordings can still CONTAIN such a press (the frame that opens the
     * menu ticks in full, so it is deliberately kept) -- this guard is what makes
     * that safe, by refusing to open the menu instead of altering the stream. */
    if(mrec_mode == 2)
    {
        freescreen(&pausebuffer);
        return;
    }

    copyscreen(pausebuffer, vscreen);
    spriteq_draw(pausebuffer, 0, MIN_INT, MAX_INT, 0, 0);
    spriteq_clear();
    spriteq_add_screen(0, 0, MIN_INT, pausebuffer, NULL, 0);
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
        int rec_items = (recmode == 0) ? 3 : 2;

        if(in_recording)
        {
            /* -- Recording submenu (state-aware) -- */
            _menutextmshift(pauseoffset[4], -3, 0, pauseoffset[5], pauseoffset[6], Tr("Recording"));

            if(recmode == 1)
            {
                _menutextmshift((rec_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], Tr("Stop Recording"));
                _menutextmshift((rec_selector == 1)?pauseoffset[1]:pauseoffset[0],  1, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
                _menutextmshift(pauseoffset[0], 3, 0, pauseoffset[2], pauseoffset[3], Tr("Recording your inputs..."));
            }
            else if(recmode == 2)
            {
                _menutextmshift((rec_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], Tr("Stop Playback"));
                _menutextmshift((rec_selector == 1)?pauseoffset[1]:pauseoffset[0],  1, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
                _menutextmshift(pauseoffset[0], 3, 0, pauseoffset[2], pauseoffset[3], Tr("Replaying a recording..."));
            }
            else
            {
                _menutextmshift((rec_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], Tr("Record"));
                _menutextmshift((rec_selector == 1)?pauseoffset[1]:pauseoffset[0],  0, 0, pauseoffset[2], pauseoffset[3], Tr("Play Recording"));
                _menutextmshift((rec_selector == 2)?pauseoffset[1]:pauseoffset[0],  2, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
            }
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

            snprintf(volbuf, sizeof(volbuf), "Music Volume: %ld", (long)savedata.musicvol);
            _menutextmshift((option_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], volbuf);

            snprintf(volbuf, sizeof(volbuf), "SFX Volume: %ld", (long)savedata.effectvol);
            _menutextmshift((option_selector == 1)?pauseoffset[1]:pauseoffset[0],  0, 0, pauseoffset[2], pauseoffset[3], volbuf);

            snprintf(volbuf, sizeof(volbuf), "FPS Display: %s", mister_fps_overlay ? "On" : "Off");
            _menutextmshift((option_selector == 2)?pauseoffset[1]:pauseoffset[0],  1, 0, pauseoffset[2], pauseoffset[3], volbuf);

            _menutextmshift((option_selector == 3)?pauseoffset[1]:pauseoffset[0],  3, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
        }

        update(1, 0);

        newkeys = player[controlp].newkeys;

        if(in_recording)
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

            if(newkeys & (FLAG_JUMP | FLAG_START))
            {
                sound_play_sample(global_sample_list.beep_2, 0, savedata.effectvol, savedata.effectvol, 100);

                if(recmode == 0)
                {
                    if(rec_selector == 0)   /* Record */
                    {
                        /* Title-anchored: queue a RECORD marker + Reset-Pak restart.
                         * On respawn the recorder (openbor.c) arms at the first frame
                         * (the PAK title) and captures your raw controller stream from
                         * the title through gameplay. The marker survives the daemon
                         * respawn. Play replays that whole stream hands-free. */
                        {
                            FILE *_rm = fopen("/tmp/openbor_recmode", "w");
                            if(_rm) { fputs("REC", _rm); fclose(_rm); }
                            _rm = fopen("/tmp/openbor_reset_marker", "w");
                            if(_rm) fclose(_rm);
                        }
                        exit(0);
                    }
                    else if(rec_selector == 1)  /* Play Recording */
                    {
                        /* Same restart: the recorder loads the saved stream + restores
                         * the RNG seed, then drives the menus into the level and plays
                         * back hands-free. Press any button to take control. */
                        {
                            FILE *_rm = fopen("/tmp/openbor_recmode", "w");
                            if(_rm) { fputs("PLAY", _rm); fclose(_rm); }
                            _rm = fopen("/tmp/openbor_reset_marker", "w");
                            if(_rm) fclose(_rm);
                        }
                        exit(0);
                    }
                    else   /* Back */
                    {
                        in_recording = 0;
                        pauselector = 2;   /* highlight the Recording entry */
                    }
                }
                else   /* recording or playing: Stop / Back */
                {
                    if(rec_selector == 0)   /* Stop Recording / Stop Playback */
                    {
                        if(recmode == 1)
                        {   /* signal the recorder to flush the .inp, then resume play */
                            FILE *_rs = fopen("/tmp/openbor_recstop", "w");
                            if(_rs) fclose(_rs);
                        }
                        else
                        {   /* stop playback immediately (recorder frees next frame) */
                            mrec_mode = 0;
                        }
                        quit = 1;   /* close the pause menu, back to gameplay */
                        sound_pause_music(0);
                        sound_pause_sample(0);
                    }
                    else   /* Back */
                    {
                        in_recording = 0;
                        pauselector = 2;
                    }
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
                        FILE *_m = fopen("/tmp/openbor_reset_marker", "w");
                        if (_m) fclose(_m);
                    }
                    exit(0);
                    break;

                case 4:  /* Quit -- delete .s0 + cache so the relaunch shows the
                          * OSD PAK browser. */
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

            if(newkeys & FLAG_MOVELEFT)
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

            if(newkeys & FLAG_MOVERIGHT)
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
    spriteq_unlock();
    spriteq_clear();
    freescreen(&pausebuffer);
}
