/*
 * MiSTer_OpenBOR — Input Patch for sdl/control.c
 *
 * Bypasses SDL joystick input and reads directly from DDR3 where the
 * FPGA writes MiSTer controller state.
 *
 * PATCH: In sdl/control.c, replace the control_update() function.
 *
 * MiSTer joystick bitmask (from hps_io):
 *   bit 0  = Right       -> FLAG_MOVERIGHT
 *   bit 1  = Left        -> FLAG_MOVELEFT
 *   bit 2  = Down        -> FLAG_MOVEDOWN
 *   bit 3  = Up          -> FLAG_MOVEUP
 *
 * On this hardware + controller the bit-order after hps_io's jn
 * remapping lands with the right-side face button at bit 4 and the
 * bottom face button at bit 5 (verified by playtesting -- pause menu
 * confirm was landing on Xbox B instead of Xbox A). We route the
 * bottom button to FLAG_ATTACK so "confirm = bottom button" matches
 * MiSTer convention. The top-row pair is swapped for consistency.
 *
 *   bit 4  = Xbox B (right)  -> FLAG_ATTACK  (Attack -- primary, beat-em-up)
 *   bit 5  = Xbox A (bottom) -> FLAG_JUMP    (Jump -- AND pause menu confirm)
 *   bit 6  = Xbox Y (top)    -> FLAG_ATTACK2 (Attack2)
 *   bit 7  = Xbox X (left)   -> FLAG_SPECIAL (Special -- AND pause menu back)
 *   bit 8  = jn L            -> FLAG_ATTACK3 (Attack3 -- e.g. DD style switch)
 *   bit 9  = jn R            -> FLAG_ATTACK4 (Attack4)
 *   bit 10 = Start           -> FLAG_START
 *
 * Attack3/Attack4 added 2026-07 so OpenBOR games that use all 6 action
 * buttons (e.g. Ultimate/Legend of the Double Dragon shoulder-button style
 * switching) are fully playable. CONF_STR: J1,Attack,Jump,Special,Attack2,
 * Attack3,Attack4,Start / jn,A,B,X,Y,L,R,Start. Adding the two buttons before
 * Start shifts Start from bit 8 to bit 10 (the RBF + this binary must ship
 * together). The FPGA already writes the full 32-bit joystick word to DDR3,
 * so no RTL change beyond the CONF_STR labels was needed.
 *
 * Copyright (C) 2026 MiSTer Organize — GPL-3.0
 */

/* Replace the entire control_update() function with this: */

void control_update(s_playercontrols ** playercontrols, int numplayers)
{
    u64 k;
    unsigned i;
    int player;
    int t;
    s_playercontrols * pcontrols;

    /* DO NOT call getPads() — it polls SDL events and dispatches
     * SDL_QUIT to borShutdown(0,...). Under SDL2 dummy driver +
     * our cwd-less main, spurious SDL_QUIT events fire during
     * model loading and silently exit the engine mid-init,
     * producing a daemon-restart loop ("loading bar restarts
     * forever" symptom).
     *
     * We read joystick state from DDR3 below, so we don't need
     * keyboard state at all. Pump+discard SDL events so the SDL
     * internal queue stays drained without triggering shutdown. */
    {
        SDL_Event ev;
        while(SDL_PollEvent(&ev)) { /* swallow everything, including SDL_QUIT */ }
    }
    for(player = 0; player < numplayers; player++)
    {
        pcontrols = playercontrols[player];
        k = 0;

#ifdef MISTER_NATIVE_VIDEO
        {
            uint32_t joy = NativeVideoWriter_ReadJoystick(player);

            /* Map MiSTer joystick bits to OpenBOR flags */
            if (joy & 0x001) k |= FLAG_MOVERIGHT;
            if (joy & 0x002) k |= FLAG_MOVELEFT;
            if (joy & 0x004) k |= FLAG_MOVEDOWN;
            if (joy & 0x008) k |= FLAG_MOVEUP;
            if (joy & 0x010) k |= FLAG_ATTACK;     /* Xbox B (right)  = Attack (primary) */
            if (joy & 0x020) k |= FLAG_JUMP;       /* Xbox A (bottom) = Jump + pause confirm */
            if (joy & 0x040) k |= FLAG_ATTACK2;    /* Xbox Y (top)    = Attack2 */
            if (joy & 0x080) k |= FLAG_SPECIAL;    /* Xbox X (left)   = Special / pause back */
            if (joy & 0x100) k |= FLAG_ATTACK3;    /* jn L            = Attack3 */
            if (joy & 0x200) k |= FLAG_ATTACK4;    /* jn R            = Attack4 */
            if (joy & 0x400) k |= FLAG_START;      /* Start (shifted bit 8 -> bit 10) */
        }
#else
        /* Original SDL keyboard input */
        for(i = 0; i < 32; i++)
        {
            t = pcontrols->settings[i];
            if(t >= SDLK_FIRST && t < SDLK_LAST)
            {
                if(keystate[t]) k |= (1 << i);
            }
        }

        /* Original SDL joystick input */
        if(usejoy)
        {
            for(i = 0; i < 32; i++)
            {
                t = pcontrols->settings[i];
                if(t >= JOY_LIST_FIRST && t <= JOY_LIST_LAST)
                {
                    int portnum = (t - JOY_LIST_FIRST - 1) / JOY_MAX_INPUTS;
                    int shiftby = (t - JOY_LIST_FIRST - 1) % JOY_MAX_INPUTS;
                    if(portnum >= 0 && portnum <= 3)
                    {
                        if((joysticks[portnum].Data >> shiftby) & 1) k |= (1 << i);
                    }
                }
            }
        }
#endif
        pcontrols->kb_break = 0;
        pcontrols->newkeyflags = k & (~pcontrols->keyflags);
        pcontrols->keyflags = k;
    }
}
