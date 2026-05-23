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
 * engine kernel character (NN) at near-zero cost; anything more
 * sophisticated (linear, cubic, polyphase) would smooth already-
 * aliased NN-mixed data for marginal audible gain at real CPU cost.
 *
 * Architectural parity with OpenBOR_4086 (same kernel, same loop body
 * byte-for-byte modulo per-core history comments). 7533 was corrected
 * from polyphase windowed-sinc to ZOH 2026-05-21 (polyphase was based
 * on a now-superseded "SDL 2 upstream → polyphase" inference that
 * confused SDL2 transport-stage resampling with the engine mixer).
 * Soft-limiter declarations + dead polyphase table/function lingered
 * in this file until 2026-05-23 cleanup; file is now lean and matches
 * 4086 byte-for-byte in Stage 2 audio output.
 *
 * Implementation rules:
 *   - uint32_t accum (always positive — no negative-shift UB)
 *   - No cross-tick state (each tick self-contained, accum starts 0)
 *   - STEP shift via uint64_t intermediate (avoids int32 overflow at
 *     rate >= 32768 — the 2026-05-15 "loud buzzing" trap)
 *
 * Copyright (C) 2026 MiSTer Organize -- GPL-3.0
 */

#include "sblaster.h"
#include "soundmix.h"
#include "sdlport.h"
#include "native_audio_writer.h"

#include <pthread.h>
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

/* 256 output × 44100/48000 = 235.2 input frames needed per tick.
 * Request 236 (ceil) so the last src index (~235) stays in-bounds. */
#define IN_FRAMES_PER_TICK   236

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
    static int16_t in_buf[IN_FRAMES_PER_TICK * 2];   /* stereo S16 @ 44.1 kHz from engine */
    static int16_t out_buf[MISTER_AUDIO_CHUNK * 2];  /* stereo S16 @ 48 kHz for DDR3      */

    /* 16.16 step per output sample: (44100 << 16) / 48000 = 60211.
     * Cast to uint64_t before shift to avoid the int32 overflow trap. */
    const uint32_t STEP = (uint32_t)(((uint64_t)ENGINE_AUDIO_RATE << 16) / MISTER_AUDIO_RATE);

    while (audio_thread_run) {
        size_t free_frames = NativeAudioWriter_FreeFrames();

        if (free_frames < (size_t)MISTER_AUDIO_CHUNK) {
            audio_sleep_us(3000);
            continue;
        }

        /* Pull IN_FRAMES_PER_TICK fresh frames from the engine's stateful mixer. */
        update_sample((unsigned char *)in_buf, IN_FRAMES_PER_TICK * 4);

        /* Zero-order hold (nearest-neighbor) resample 44100 -> 48000 Hz.
         * Mirrors engine character per feedback_audio_type_from_engine_source.md
         * (engine/source/gamelib/soundmix.c at lines 483/527/552 uses
         * sptr16[FIX_TO_INT(fp_pos)] = shift-truncation NN at all three
         * sample-read sites). Polyphase windowed-sinc on top of NN-mixed
         * engine output is pure CPU waste for marginal benefit; ZOH at
         * wrapper preserves engine character at near-zero cost.
         * Architectural parity with OpenBOR_4086 (same kernel). */
        uint32_t accum = 0;
        int i;
        for (i = 0; i < MISTER_AUDIO_CHUNK; i++) {
            int ip = (int)(accum >> 16);
            if (ip >= IN_FRAMES_PER_TICK) ip = IN_FRAMES_PER_TICK - 1;
            out_buf[2 * i + 0] = in_buf[2 * ip + 0];
            out_buf[2 * i + 1] = in_buf[2 * ip + 1];
            accum += STEP;
        }

        NativeAudioWriter_Submit(out_buf, MISTER_AUDIO_CHUNK);
    }
    return NULL;
}

int SB_playstart(int bits, int samplerate) {
    (void)bits;
    (void)samplerate;

    if (started) return 1;

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
    audio_thread_run = 0;
    pthread_join(audio_thread, NULL);
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
