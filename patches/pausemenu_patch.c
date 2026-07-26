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
 *   Recording  — submenu: Record / Play Recording (the engine's .inp input
 *                recorder), state-aware (shows Stop while active)
 *   Reset Pak  — restarts the PAK from its title screen
 *   Quit       — exits OpenBOR (daemon relaunches, user lands at PAK browser)
 *
 * CONTROLS:
 *   D-pad Up/Down  — navigate
 *   Xbox A (FLAG_JUMP) / Start (FLAG_START) — confirm
 *   X (FLAG_SPECIAL) / ESC — back / close menu
 *   D-pad Left/Right — adjust volume in Options
 *
 * RECORDING (the engine's built-in .inp input recorder):
 *   The recorder is per-PAK — it saves/loads
 *   /media/fat/saves/OpenBOR_7533/<pak>.inp via getPakName(...,3) + the engine's
 *   "Saves" base path (left in playrecstatus->path empty so getBasePath fills it
 *   with a trailing slash — avoids the recorder's path-slash quirk).
 *
 *   FLOW: In a level, pause -> Recording -> "Record" -> the menu closes and your
 *   inputs are recorded as you play. Pause -> Recording -> "Stop Recording"
 *   writes the .inp. Later, pause -> Recording -> "Play Recording" replays it.
 *
 *   LIMITATION (engine behaviour, not ours): the recorder only captures/replays
 *   IN-LEVEL player inputs — it does not record menu/title navigation, so replay
 *   drives an already-loaded level; it cannot play back from the title screen.
 *   Recording is paused while the pause menu is open (the engine gates it on
 *   !_pause), so menu navigation is never recorded. .inp files are build/arch
 *   specific.
 *
 * RESET PAK / QUIT: unchanged from the prior menu (see the case bodies).
 *
 * The recorder globals (playrecstatus, A_REC_STOP/REC/PLAY, stopRecordInputs,
 * getPakName) are all in openbor.c scope, since this function is injected in
 * place of the stock pausemenu(). playrecstatus is non-NULL during gameplay
 * (init_input_recorder() runs unconditionally at engine startup). The recorder's
 * stopRecordInputs() double-free is fixed in apply_patches.py (step: recorder
 * double-free) so "Stop Recording" writes the .inp cleanly.
 *
 * Copyright (C) 2026 MiSTer Organize — GPL-3.0
 */

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
    s_screen *pausebuffer = allocscreen(videomodes.hRes, videomodes.vRes, PIXEL_32);

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

    while(!quit)
    {
        int recmode = playrecstatus ? playrecstatus->status : A_REC_STOP;
        int rec_items = (recmode == A_REC_STOP) ? 3 : 2;

        if(in_recording)
        {
            /* -- Recording submenu (state-aware) -- */
            _menutextmshift(pauseoffset[4], -3, 0, pauseoffset[5], pauseoffset[6], Tr("Recording"));

            if(recmode == A_REC_REC)
            {
                _menutextmshift((rec_selector == 0)?pauseoffset[1]:pauseoffset[0], -1, 0, pauseoffset[2], pauseoffset[3], Tr("Stop Recording"));
                _menutextmshift((rec_selector == 1)?pauseoffset[1]:pauseoffset[0],  1, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
                _menutextmshift(pauseoffset[0], 3, 0, pauseoffset[2], pauseoffset[3], Tr("Recording your inputs..."));
            }
            else if(recmode == A_REC_PLAY)
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

            _menutextmshift((option_selector == 2)?pauseoffset[1]:pauseoffset[0],  2, 0, pauseoffset[2], pauseoffset[3], Tr("Back"));
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

                if(recmode == A_REC_STOP)
                {
                    if(rec_selector == 0 && playrecstatus)   /* Record */
                    {
                        /* Reset-to-start (NES-TAS style): OpenBOR's .inp needs the
                         * SAME start state for record AND replay, so both begin from
                         * a fresh level load. Queue a RECORD marker + trigger the
                         * Reset-Pak restart; the level-load hook (openbor.c) arms
                         * A_REC_REC the instant the PAK's first level loads, and the
                         * marker survives the daemon respawn. */
                        stopRecordInputs();               /* clear any prior state */
                        {
                            FILE *_rm = fopen("/tmp/openbor_recmode", "w");
                            if(_rm) { fputs("REC", _rm); fclose(_rm); }
                            _rm = fopen("/tmp/openbor_reset_marker", "w");
                            if(_rm) fclose(_rm);
                        }
                        exit(0);
                    }
                    else if(rec_selector == 1 && playrecstatus)  /* Play Recording */
                    {
                        /* Same reset-to-start: queue a PLAY marker + restart so the
                         * replay begins from the identical fresh level-1 state the
                         * recording started from (deterministic; RNG reseeded from
                         * the .inp header). */
                        stopRecordInputs();
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
                        stopRecordInputs();
                        rec_selector = 0;   /* submenu returns to the idle (Record/Play) items */
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
                option_selector = (option_selector + 2) % 3;
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }
            if(newkeys & FLAG_MOVEDOWN)
            {
                option_selector = (option_selector + 1) % 3;
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
                sound_play_sample(global_sample_list.beep, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            if(newkeys & (FLAG_JUMP | FLAG_START))
            {
                if(option_selector == 2)  /* Back */
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
