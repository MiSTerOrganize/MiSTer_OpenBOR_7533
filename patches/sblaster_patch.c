/*
 * MiSTer_OpenBOR_7533 -- sdl/sblaster.c MiSTer replacement
 *
 * Audio Stage 2: engine renders at upstream native 44.1 kHz (Sega CD
 * Red Book CDDA reference rate); glue layer resamples to 48 kHz via
 * ZERO-ORDER HOLD (sample-and-hold / nearest-neighbor). Engine-source-
 * driven choice per the NON-NEGOTIABLE rule in
 * feedback_audio_type_from_engine_source.md: upstream OpenBOR's mixer
 * (engine/source/gamelib/soundmix.c lines 483/527/552) uses
 * sptr16[FIX_TO_INT(fp_pos)] = shift-truncation nearest-neighbor at
 * all three sample-read sites. The wrapper resampler matches the
 * engine kernel character (NN) at near-zero cost.
 *
 * Implementation rules:
 *   - uint32_t accum (always positive -- no negative-shift UB)
 *   - THE PHASE CARRIES ACROSS TICKS, and so does any unread frame.
 *     This rule is the REVERSE of what it used to say ("no cross-tick
 *     state, accum starts 0"), because that self-contained tick was the
 *     bug: 256 outputs need 235.2 inputs, the fixed request of 236 could
 *     not be fractional, and the leftover was advanced past by the
 *     stateful mixer and lost. Simulated at 44250 Hz effective against
 *     44100 emitted -- 0.34% fast, as a discontinuity at every tick
 *     boundary, 187.5 times a second.
 *   - STEP shift via uint64_t intermediate (avoids int32 overflow at
 *     rate >= 32768 -- the 2026-05-15 "loud buzzing" trap)
 *
 * DIVERGES FROM OpenBOR_4086, which still has the self-contained tick.
 * 4086 is archived and takes no further updates, so this is a recorded
 * divergence rather than a parity gap; the loop bodies are no longer
 * byte-for-byte identical and are not expected to be.
 *
 * Copyright (C) 2026 MiSTer Organize -- GPL-3.0
 */

/* Step J (v3.1 perf): _GNU_SOURCE for pthread_setaffinity_np + CPU_SET. */
#ifndef _GNU_SOURCE
#define _GNU_SOURCE 1
#endif

#include "sblaster.h"
#include "soundmix.h"
#include "sdlport.h"
#include "native_audio_writer.h"

#include <pthread.h>
#include <sched.h>  /* Step J: CPU_ZERO / CPU_SET / cpu_set_t */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

/* OpenBOR's mixer renders stereo S16 PCM at 44.1 kHz upstream native. */
extern void update_sample(unsigned char *buf, int size);

#define ENGINE_AUDIO_RATE    44100
#define MISTER_AUDIO_RATE    48000
#define MISTER_AUDIO_CHUNK   256                      /* output frames per tick (48 kHz)   */
#define MISTER_CHUNK_BYTES   (MISTER_AUDIO_CHUNK * 4) /* stereo S16                          */

/* 256 output x 44100/48000 = 235.2 input frames per tick -- a FRACTION, which
 * is the whole problem. The engine mixer is STATEFUL: update_sample(n) advances
 * it by n frames whether or not we read them, so frames requested and not read
 * are generated and thrown away.
 *
 * Requesting a fixed 236 (ceil) and resetting the phase each tick dropped
 * exactly one frame per tick, forever: the highest index reached at i=255 is
 * (255*STEP)>>16 = 234, so frame 235 was never read. Simulated over 2000 ticks:
 * the mixer advanced 236.0 frames/tick = 44250.0 Hz effective against the
 * 44100 Hz we emit -- a 0.34% clock error delivered as a discontinuity at the
 * tick boundary, 187.5 times a second.
 *
 * The buffer is now sized to hold a tick's worth plus the frame that can be
 * carried over; the per-tick count is computed from the live phase. */
#define IN_BUF_FRAMES        240

static int              started;
static int              voicevol = 15;
static pthread_t        audio_thread;
static volatile int     audio_thread_run;

static void audio_sleep_us(long us) {
    struct timespec ts;
    ts.tv_sec  = us / 1000000L;
    ts.tv_nsec = (us % 1000000L) * 1000L;
    nanosleep(&ts, NULL);
}

static void *audio_thread_fn(void *arg) {
    (void)arg;
    static int16_t in_buf[IN_BUF_FRAMES * 2];        /* stereo S16 @ 44.1 kHz from engine */
    /* 🛑 BOTH PERSIST ACROSS TICKS. accum is the 16.16 phase into in_buf and
     * have is how many frames it still holds; resetting either reintroduces the
     * dropped-frame bug this pair exists to fix. */
    static uint32_t accum = 0;
    static int      have  = 0;
    static int16_t out_buf[MISTER_AUDIO_CHUNK * 2];  /* stereo S16 @ 48 kHz for DDR3      */

    /* Step J (affinity INVERSION 2026-06-13): pin audio thread to core 1. */
    /* Render/engine now runs on core 0 (native_video_writer.c) because the */
    /* blend pass is memory-bound and core 0 has ~1.85x core 1's DDR3 read */
    /* bandwidth. Audio is light (~6.7 ticks/sec, each ~3ms), so it moves to */
    /* core 1 and stops contending with the render loop. The handler launches */
    /* with taskset 0x03 (both cores), so this pin succeeds. */
    {
        cpu_set_t cpuset;
        CPU_ZERO(&cpuset);
        CPU_SET(1, &cpuset);
        pthread_setaffinity_np(pthread_self(), sizeof(cpuset), &cpuset);
    }

    /* 16.16 step per output sample: (44100 << 16) / 48000 = 60211.
     * Cast to uint64_t before shift to avoid the int32 overflow trap. */
    const uint32_t STEP = (uint32_t)(((uint64_t)ENGINE_AUDIO_RATE << 16) / MISTER_AUDIO_RATE);

    while (audio_thread_run) {
        size_t free_frames = NativeAudioWriter_FreeFrames();

        if (free_frames < (size_t)MISTER_AUDIO_CHUNK) {
            audio_sleep_us(3000);
            continue;
        }

        /* Ask the mixer for exactly what this tick's phase will reach, and no
         * more. The highest index used is (accum + 255*STEP)>>16, so that plus
         * one is the frame count; anything beyond it would be advanced past and
         * discarded. `have` is what already sits in the buffer, carried from
         * last tick. */
        {
            int need = (int)((accum + (uint32_t)(MISTER_AUDIO_CHUNK - 1) * STEP) >> 16) + 1;
            if (need > IN_BUF_FRAMES) need = IN_BUF_FRAMES;   /* cannot happen; bound anyway */
            if (need > have) {
                update_sample((unsigned char *)(in_buf + 2 * have), (need - have) * 4);
                have = need;
            }
        }

        /* Zero-order hold (nearest-neighbor) resample 44100 -> 48000 Hz.
         * Mirrors engine character per feedback_audio_type_from_engine_source.md
         * (engine/source/gamelib/soundmix.c at lines 483/527/552 uses
         * sptr16[FIX_TO_INT(fp_pos)] = shift-truncation NN at all three
         * sample-read sites). */
        int i;
        for (i = 0; i < MISTER_AUDIO_CHUNK; i++) {
            int ip = (int)(accum >> 16);
            if (ip >= have) ip = have - 1;
            out_buf[2 * i + 0] = in_buf[2 * ip + 0];
            out_buf[2 * i + 1] = in_buf[2 * ip + 1];
            accum += STEP;
        }

        /* Retire the whole frames the phase passed, keep the remainder for the
         * next tick, and rebase the phase onto what is left. This is what makes
         * the input stream continuous across the boundary -- without it the
         * fractional 0.2 of a frame was dropped every tick. At most one frame
         * ever moves, so the memmove is a few bytes. */
        {
            int consumed = (int)(accum >> 16);
            if (consumed > have) consumed = have;
            if (consumed > 0) {
                memmove(in_buf, in_buf + 2 * consumed,
                        (size_t)(have - consumed) * 2 * sizeof(int16_t));
                have -= consumed;
                accum -= (uint32_t)consumed << 16;
            }
        }

        NativeAudioWriter_Submit(out_buf, MISTER_AUDIO_CHUNK);
    }
    return NULL;
}

int SB_playstart(int bits, int samplerate) {
    (void)bits;
    (void)samplerate;

    if (started) return 1;

    /* OB_TEST deterministic trace mode (debugging-methodology component 1):
     * keep the DDR3 audio thread OFF -- the video_copy_screen trace block
     * pulls the mixer synchronously per presented frame, which is what the
     * AUDIOCRC hashes. Async pulls here would race it. Test runs are
     * silent on hardware; determinism over audibility. */
    if (getenv("OB_TEST")) { started = 1; return 1; }

    if (!NativeAudioWriter_IsActive()) {
        return 0;
    }

    audio_thread_run = 1;
    if (pthread_create(&audio_thread, NULL, audio_thread_fn, NULL) != 0) {
        audio_thread_run = 0;
        return 0;
    }
    started = 1;
    return 1;
}

void SB_playstop(void) {
    if (!started) return;
    /* OB_TEST gate in SB_playstart sets started without spawning the
     * thread -- only join a thread that was actually created. */
    if (audio_thread_run) {
        audio_thread_run = 0;
        pthread_join(audio_thread, NULL);
    }
    started = 0;
}

void SB_setvolume(char dev, char volume) {
    if (dev == SB_VOICEVOL) voicevol = volume;
}

void SB_updatevolume(int volume) {
    voicevol += volume;
    if (voicevol > 15) voicevol = 15;
    if (voicevol < 0)  voicevol = 0;
}
