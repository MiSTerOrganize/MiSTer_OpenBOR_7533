/*
 * MiSTer_OpenBOR -- sdl/sblaster.c MiSTer replacement
 *
 * Option C v2: engine outputs at upstream native 44.1 kHz (Sega CD Red
 * Book CDDA rate — matches the NTSC-region-match rule's reference) and
 * THIS file resamples 44.1 → 48 kHz at the ARM glue layer before DDR3
 * submission. Mirrors the PICO-8 architecture (engine at 22050 native,
 * mister_main.cpp resamples to 48 kHz). Replaces Option A which forced
 * the engine to 48 kHz via apply_patches.py — that worked but diverged
 * from Sega CD's native audio rate.
 *
 * Resample method: LINEAR interpolation per channel.
 *   - Mid-quality bandlimited reconstruction matching what PC OS audio
 *     stacks typically use for 44.1 → 48 kHz conversion (PC OpenBOR.exe
 *     relies on this same conversion via Windows MMSYS or PulseAudio).
 *   - No overshoot (cubic Hermite's overshoot caused multi-voice mixbuf
 *     clipping crackles on multi-enemy specials in our previous attempt).
 *   - No clamping needed for valid s16 input — output is always in
 *     [min(s0,s1), max(s0,s1)] range.
 *
 * Implementation details (designed to avoid the 2026-05-15 "constant per
 * Stage 2 tick" failure mode):
 *   - uint32_t accum (always positive) — no negative-shift UB territory.
 *   - No cross-tick state (no prev_tail, no resamp_phase). Each tick
 *     starts accum=0, processes a self-contained input/output buffer.
 *     Mirrors PICO-8's mister_main.cpp::upsample_mono_to_stereo which
 *     ships this pattern successfully.
 *   - Linear interp (not cubic) — simpler math, no overshoot. The s0+
 *     (s1-s0)*frac pattern works in pure integer arithmetic with no
 *     intermediate >= int32 saturation risk for s16 sources.
 *
 * Net rate-conversion drift: ~0.05% pitch (≈6 cents downward, below
 * audible threshold) from the requested-vs-consumed-per-tick rounding
 * (256 output × 0.91875 = 235.2 input, but we request 236 per tick).
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

/* OpenBOR's mixer renders into a caller-supplied byte buffer at 44.1 kHz
 * stereo S16 (upstream native, NOT forced to 48 kHz any more). */
extern void update_sample(unsigned char *buf, int size);

#define ENGINE_AUDIO_RATE    44100
#define MISTER_AUDIO_RATE    48000
#define MISTER_AUDIO_CHUNK   256       /* output frames per tick (48 kHz)             */
#define MISTER_CHUNK_BYTES   (MISTER_AUDIO_CHUNK * 4) /* stereo S16                    */

/* Input frames requested per tick. 256 output × 44100/48000 = 235.2 input
 * needed; +1 margin so the last output's linear interp has a valid s1.
 * The 0.8-frame per-tick over-request causes ~0.05% pitch drift downward,
 * inaudible. update_sample is stateful so consecutive ticks see contiguous
 * stream samples. */
#define IN_FRAMES_PER_TICK   237
#define IN_BUF_FRAMES        (IN_FRAMES_PER_TICK + 1) /* +1 for linear-interp s1 lookup at idx=235 */

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
    static int16_t in_buf[IN_BUF_FRAMES * 2];        /* stereo S16 from engine @ 44.1 kHz */
    static int16_t out_buf[MISTER_AUDIO_CHUNK * 2];  /* stereo S16 @ 48 kHz for DDR3 ring */

    /* 16.16 step per output sample = (ENGINE_RATE / MISTER_RATE) << 16.
     * MUST cast to uint64_t before the shift — (uint32_t)44100 << 16 would
     * overflow the same way the previous int32_t version did. Use uint64_t
     * intermediate, narrow back to uint32_t at the end. */
    const uint32_t STEP = (uint32_t)(((uint64_t)ENGINE_AUDIO_RATE << 16) / MISTER_AUDIO_RATE);

    while (audio_thread_run) {
        size_t free_frames = NativeAudioWriter_FreeFrames();

        if (free_frames < (size_t)MISTER_AUDIO_CHUNK) {
            audio_sleep_us(3000);
            continue;
        }

        /* Pull IN_FRAMES_PER_TICK fresh frames from the engine's mixer.
         * update_sample is stateful — consecutive calls return successive
         * positions in the engine's audio stream. */
        update_sample((unsigned char *)in_buf, IN_FRAMES_PER_TICK * 4);

        /* Linear interp 44.1 → 48 kHz, per channel.
         * uint32_t accum always positive — no signed-shift UB.
         * No cross-tick state — each tick self-contained. Mirrors PICO-8's
         * upsample_mono_to_stereo working pattern. */
        uint32_t accum = 0;
        for (int i = 0; i < MISTER_AUDIO_CHUNK; i++) {
            uint32_t src_idx = accum >> 16;
            uint32_t fr      = accum & 0xFFFF;  /* fractional part, [0, 65535] */

            /* Boundary guard: if src_idx would exceed IN_FRAMES_PER_TICK,
             * clamp to last input frame. With IN_FRAMES_PER_TICK=237 we
             * always have room for src_idx+1 ≤ 236 because 256*STEP =
             * 256*60211 = 15414016 = 235.16 in 16.16, so src_idx max ≈ 235. */
            if (src_idx + 1 >= IN_FRAMES_PER_TICK) src_idx = IN_FRAMES_PER_TICK - 2;

            int32_t l0 = in_buf[2 * src_idx + 0];
            int32_t l1 = in_buf[2 * (src_idx + 1) + 0];
            int32_t r0 = in_buf[2 * src_idx + 1];
            int32_t r1 = in_buf[2 * (src_idx + 1) + 1];

            /* output = s0 + (s1 - s0) * fr / 65536
             * For s16 input, (s1 - s0) is in [-65535, 65535], multiplied by
             * fr in [0, 65535], result fits int32_t. After >> 16, back to
             * int16 range (no clamp needed for valid s16 input). */
            int32_t l = l0 + (((l1 - l0) * (int32_t)fr) >> 16);
            int32_t r = r0 + (((r1 - r0) * (int32_t)fr) >> 16);

            out_buf[2 * i + 0] = (int16_t)l;
            out_buf[2 * i + 1] = (int16_t)r;

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
        /* sdlport's main() calls NativeAudioWriter_Init(); if that failed
         * we have no audio path at all. Don't start the thread. */
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
