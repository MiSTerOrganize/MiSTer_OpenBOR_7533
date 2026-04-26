/*
 * MiSTer_OpenBOR — Custom Pause Menu Patch for OpenBOR Build 3979
 *
 * This file contains a replacement for the stock pausemenu() function in
 * openbor.c (line 13485 in the rofl0r/openbor svn-branch commit 3b0a718).
 *
 * REPLACES: The stock 2-item pause menu (Continue / End Game).
 *
 * NEW PAUSE MENU:
 *   Continue   — resumes the game
 *   Options    — opens submenu: Music Volume / SFX Volume / Back
 *   Reset Pak  — restarts the PAK from its title screen
 *   Quit       — exits OpenBOR (daemon relaunches, user lands at PAK browser)
 *
 * CONTROLS:
 *   D-pad Up/Down  — navigate menu entries
 *   Xbox A (bottom, FLAG_JUMP in this core's mapping) -- confirm / select entry
 *   X Button (FLAG_SPECIAL) — back (closes menu from main, or back to main from Options)
 *   Start Button (FLAG_START) — also confirms
 *   D-pad Left/Right — adjust Music/SFX volume in Options submenu
 *
 * RESET PAK IMPLEMENTATION:
 *   Sets endgame = 2 — this is the same mechanism OpenBOR's stock "End Game"
 *   uses to exit the game loop. Control returns from playgame() back to
 *   openborMain(), which redraws the PAK's main menu (Start Game / Options /
 *   How To / Hall of Fame / Quit). This is the PAK's "beginning" state, same
 *   concept as PICO-8's Reset Cart. Unlike stock End Game, we do NOT zero
 *   the player lives, so the game-over sequence does not play.
 *
 * QUIT IMPLEMENTATION:
 *   Calls exit(0). The OpenBOR daemon sees the process exit and relaunches
 *   the binary, which lands the user at OpenBOR's PAK browser (the file
 *   listing of /media/fat/games/OpenBOR/Paks/). Same behavior as pressing
 *   SELECT during gameplay.
 *
 * HOW TO APPLY:
 *   Replace the entire stock pausemenu() function in openbor.c starting at
 *   line 13485 with the function below. The function signature is
 *   identical: void pausemenu(void).
 *
 *   The Reset Pak implementation relies on the 'endgame' global variable
 *   which is already declared in openbor.c at line 423. No new globals
 *   required. No changes to openborMain() required.
 *
 * Copyright (C) 2026 MiSTer Organize — GPL-3.0
 */

void pausemenu()
{
    int pauselector = 0;
    int option_selector = 0;
    int in_options = 0;
    int quit = 0;
    int controlp = 0, i;
    int newkeys;
    char volbuf[64];
    s_set_entry *set = levelsets + current_set;
    s_screen *pausebuffer = allocscreen(videomodes.hRes, videomodes.vRes, screenformat);

    copyscreen(pausebuffer, vscreen);
    spriteq_draw(pausebuffer, 0, MIN_INT, MAX_INT, 0, 0);
    spriteq_clear();
    spriteq_add_screen(0, 0, MIN_INT, pausebuffer, NULL, 0);
    spriteq_lock();

    /* Find which player opened the pause menu (matches stock behavior) */
    for(i = 0; i < set->maxplayers; i++)
    {
        if(player[i].ent && (player[i].newkeys & FLAG_START))
        {
            controlp = i;
            break;
        }
    }

    pause = 2;
    bothnewkeys = 0;

    while(!quit)
    {
        if(!in_options)
        {
            /* -- Main pause menu: Continue / Options / Reset Pak / Quit -- */
            _menutextm(3, -3, 0, Tr("Pause"));
            _menutextm((pauselector == 0), -1, 0, Tr("Continue"));
            _menutextm((pauselector == 1),  0, 0, Tr("Options"));
            _menutextm((pauselector == 2),  1, 0, Tr("Reset Pak"));
            _menutextm((pauselector == 3),  2, 0, Tr("Quit"));
        }
        else
        {
            /* -- Options submenu: Music Volume / SFX Volume / Back -- */
            _menutextm(3, -3, 0, Tr("Options"));

            snprintf(volbuf, sizeof(volbuf), "Music Volume: %ld", (long)savedata.musicvol);
            _menutextm((option_selector == 0), -1, 0, volbuf);

            snprintf(volbuf, sizeof(volbuf), "SFX Volume: %ld", (long)savedata.effectvol);
            _menutextm((option_selector == 1),  0, 0, volbuf);

            _menutextm((option_selector == 2),  2, 0, Tr("Back"));
        }

        update(1, 0);

        newkeys = player[controlp].newkeys;

        if(!in_options)
        {
            /* -- Main pause menu input handling -- */

            /* D-pad up/down — navigate, wraps at 4 entries */
            if(newkeys & FLAG_MOVEUP)
            {
                pauselector = (pauselector + 3) % 4;
                sound_play_sample(SAMPLE_BEEP, 0, savedata.effectvol, savedata.effectvol, 100);
            }
            if(newkeys & FLAG_MOVEDOWN)
            {
                pauselector = (pauselector + 1) % 4;
                sound_play_sample(SAMPLE_BEEP, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            /* Xbox A (FLAG_JUMP in this mapping) or Start -- confirm selection */
            if(newkeys & (FLAG_JUMP | FLAG_START))
            {
                sound_play_sample(SAMPLE_BEEP2, 0, savedata.effectvol, savedata.effectvol, 100);
                switch(pauselector)
                {
                case 0:  /* Continue — resume game */
                    quit = 1;
                    sound_pause_music(0);
                    sound_pause_sample(0);
                    break;

                case 1:  /* Options — enter submenu */
                    in_options = 1;
                    option_selector = 0;
                    break;

                case 2:  /* Reset Pak -- restart same PAK fresh.
                          * The .current.pak cache lives on SD; just exit
                          * and the daemon relaunch picks up the same file
                          * via sdlport_patch's stat() check. */
                    exit(0);
                    break;

                case 3:  /* Quit -- delete .s0 and cache so the relaunch
                          * has no PAK to load, showing the OSD menu. */
                    remove("/tmp/openbor_current.pak");
                    remove("/media/fat/config/OpenBOR_4086.s0");
                    exit(0);
                    break;
                }
            }

            /* X button (Special) or ESC — close menu (same as Continue) */
            if(newkeys & (FLAG_SPECIAL | FLAG_ESC))
            {
                quit = 1;
                sound_pause_music(0);
                sound_pause_sample(0);
                sound_play_sample(SAMPLE_BEEP2, 0, savedata.effectvol, savedata.effectvol, 100);
            }
        }
        else
        {
            /* -- Options submenu input handling -- */

            /* D-pad up/down — navigate 3 entries */
            if(newkeys & FLAG_MOVEUP)
            {
                option_selector = (option_selector + 2) % 3;
                sound_play_sample(SAMPLE_BEEP, 0, savedata.effectvol, savedata.effectvol, 100);
            }
            if(newkeys & FLAG_MOVEDOWN)
            {
                option_selector = (option_selector + 1) % 3;
                sound_play_sample(SAMPLE_BEEP, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            /* D-pad left — decrease volume (Music or SFX, depending on selection) */
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
                sound_play_sample(SAMPLE_BEEP, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            /* D-pad right — increase volume */
            if(newkeys & FLAG_MOVERIGHT)
            {
                if(option_selector == 0 && savedata.musicvol <= 90)
                {
                    savedata.musicvol += 10;
                    sound_volume_music(savedata.musicvol, savedata.musicvol);
                }
                else if(option_selector == 1 && savedata.effectvol <= 110)
                {
                    /* Effect volume default is 120, allow up to 120 max */
                    savedata.effectvol += 10;
                }
                sound_play_sample(SAMPLE_BEEP, 0, savedata.effectvol, savedata.effectvol, 100);
            }

            /* Xbox A (FLAG_JUMP) or Start -- confirm (only Back does anything) */
            if(newkeys & (FLAG_JUMP | FLAG_START))
            {
                if(option_selector == 2)  /* Back */
                {
                    in_options = 0;
                    pauselector = 1;  /* return highlight to Options entry */
                    sound_play_sample(SAMPLE_BEEP2, 0, savedata.effectvol, savedata.effectvol, 100);
                }
            }

            /* X button or ESC — back to main pause menu */
            if(newkeys & (FLAG_SPECIAL | FLAG_ESC))
            {
                in_options = 0;
                pauselector = 1;  /* return highlight to Options entry */
                sound_play_sample(SAMPLE_BEEP2, 0, savedata.effectvol, savedata.effectvol, 100);
            }
        }
    }

    pause = 0;
    bothnewkeys = 0;
    spriteq_unlock();
    spriteq_clear();
    freescreen(&pausebuffer);
}
