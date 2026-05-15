/*
 * MiSTer_OpenBOR -- sdl/sblaster.c MiSTer replacement
 *
 * Replaces OpenBOR's SDL-audio backend with a pthread that drains
 * update_sample() into the DDR3 audio ring buffer at the FPGA's 48 kHz
 * consumption rate. No SDL_OpenAudio, no ALSA.
 *
 * Upstream renders at its native 44.1 kHz (Red Book audio rate — matches
 * the Sega CD reference architecture OpenBOR PAKs are designed around).
 * This file resamples 44.1 → 48 kHz via cubic Hermite (4-tap Catmull-Rom)
 * before submitting to the DDR3 ring. PAK samples authored at exactly
 * 44.1 kHz (CDDA tracks, Red-Book music) pass through the engine mixer
 * bit-perfect (fp_period = 1.0 → no engine-stage resample); only the
 * single 44.1 → 48 boundary stage applies. Per the audio quality ladder:
 * 16-bit recorded audio with treble + transients → cubic, not linear.
 *
 * PATCH: when BUILD_MISTER is defined, replace the ENTIRE contents of
 * sdl/sblaster.c with this file. apply_patches.py handles the swap.
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

/* OpenBOR's mixer renders into a caller-supplied byte buffer. We use it
 * to fetch 44.1 kHz signed-16 stereo PCM from the engine. */
extern void update_sample(unsigned char *buf, int size);

#define ENGINE_AUDIO_RATE    44100
#define MISTER_AUDIO_RATE    48000
#define MISTER_AUDIO_CHUNK   256                      /* output frames per tick (48 kHz)            */
#define MISTER_CHUNK_BYTES   (MISTER_AUDIO_CHUNK * 4) /* stereo S16                                  */

/* Input request size per tick. 256 output samples at 48 kHz consume
 * 256 * 44100/48000 = 235.2 input samples on average. Request 236 (round up)
 * so the engine renders just over what we need. Long-term: avg engine rate
 * = 187.5 ticks/sec × 236 frames = 44,250 frames/sec, slightly above 44,100
 * (~0.34% / ~6 cents pitch shift, near-inaudible). The FPGA's ring-full
 * sleep gate naturally throttles us — if we over-produce, the ring fills
 * and we sleep, letting the engine "catch its breath". Drift is bounded
 * by the ring capacity. */
#define IN_FRAMES_PER_TICK   236
#define IN_BUF_FRAMES        (IN_FRAMES_PER_TICK + 4) /* +4 for Hermite s2 lookahead with margin   */

static int              started;
static int              voicevol = 15;
static pthread_t        audio_thread;
static volatile int     audio_thread_run;

/* Resample state — carries 3 input samples and fractional phase across
 * tick boundaries so cubic Hermite reads from a continuous stream. */
static int16_t  prev_tail_l[3] = {0, 0, 0};
static int16_t  prev_tail_r[3] = {0, 0, 0};
static int32_t  resamp_phase   = 0;  /* signed 16.16 fixed-point; may go slightly negative across ticks */

static void audio_sleep_us(long us) {
    struct timespec ts;
    ts.tv_sec  = us / 1000000L;
    ts.tv_nsec = (us % 1000000L) * 1000L;
    nanosleep(&ts, NULL);
}

/* Cubic Hermite (4-tap Catmull-Rom). t is 16.16 fractional position in
 * [0, 65536) representing the interpolation weight between s0 and s1.
 * sm1, s0, s1, s2 are int (range fits int16). Returns int16-range value
 * clamped (Hermite can overshoot ~50% of adjacent-sample delta on sharp
 * transients). Cost on Cortex-A9 NEON: 3 smull + 3 add per call. */
static inline int hermite4(int sm1, int s0, int s1, int s2, uint32_t t)
{
    int a0_2 = -sm1 + 3*s0 - 3*s1 + s2;
    int a1_2 = 2*sm1 - 5*s0 + 4*s1 - s2;
    int a2_2 = -sm1 + s1;
    int a3_2 = 2*s0;
    int t2 = (int)(((long long)t * t) >> 16);
    int t3 = (int)(((long long)t2 * t) >> 16);
    int x2 = (int)(((long long)a0_2 * t3) >> 16)
           + (int)(((long long)a1_2 * t2) >> 16)
           + (int)(((long long)a2_2 * (int)t) >> 16)
           +  a3_2;
    int out = x2 >> 1;
    if (out >  32767) out =  32767;
    if (out < -32768) out = -32768;
    return out;
}

/* Sample lookup for the resampler: idx is relative to the current tick's
 * in_buf. Negative idx reads from prev_tail (the previous tick's last 3
 * samples), saved at tick end for cross-boundary Hermite continuity. idx
 * beyond IN_FRAMES_PER_TICK clamps to the last input sample (only happens
 * if STEP × MISTER_AUDIO_CHUNK exceeds our requested input count — bounded). */
static inline int read_sample_l(const int16_t *in_buf, int idx)
{
    if (idx >= 0 && idx < IN_FRAMES_PER_TICK) return in_buf[2 * idx];
    if (idx >= -3 && idx < 0)                 return prev_tail_l[3 + idx];
    if (idx >= IN_FRAMES_PER_TICK)            return in_buf[2 * (IN_FRAMES_PER_TICK - 1)];
    return 0;
}
static inline int read_sample_r(const int16_t *in_buf, int idx)
{
    if (idx >= 0 && idx < IN_FRAMES_PER_TICK) return in_buf[2 * idx + 1];
    if (idx >= -3 && idx < 0)                 return prev_tail_r[3 + idx];
    if (idx >= IN_FRAMES_PER_TICK)            return in_buf[2 * (IN_FRAMES_PER_TICK - 1) + 1];
    return 0;
}

static void *audio_thread_fn(void *arg) {
    (void)arg;
    static int16_t in_buf[IN_BUF_FRAMES * 2];      /* stereo S16 from engine @ 44.1 kHz */
    static int16_t out_buf[MISTER_AUDIO_CHUNK * 2];/* stereo S16 @ 48 kHz for DDR3 ring */

    /* 16.16 step per output sample: (ENGINE_AUDIO_RATE / MISTER_AUDIO_RATE) in fixed-point.
     * (44100 << 16) / 48000 = 60293, i.e. ~0.91875 input samples per output sample. */
    const int32_t STEP = ((int32_t)ENGINE_AUDIO_RATE << 16) / MISTER_AUDIO_RATE;

    while (audio_thread_run) {
        size_t free_frames = NativeAudioWriter_FreeFrames();

        if (free_frames < (size_t)MISTER_AUDIO_CHUNK) {
            audio_sleep_us(3000);
            continue;
        }

        /* Render 44.1 kHz input from the engine. update_sample is stateful —
         * each call returns the NEXT N samples in the stream, so consecutive
         * ticks are continuous. We render IN_FRAMES_PER_TICK frames; the +4
         * extra slots in in_buf are unused this tick (kept as headroom in case
         * we ever need to dynamically request more for boundary handling). */
        update_sample((unsigned char *)in_buf, IN_FRAMES_PER_TICK * 4);

        /* Cubic Hermite 44.1 → 48 kHz resample. Phase is signed 16.16 and may
         * start slightly negative if the previous tick's phase ended past
         * IN_FRAMES_PER_TICK (carries fractional position into the next tick's
         * frame; negative ip values read from prev_tail). */
        int32_t phase = resamp_phase;
        for (int i = 0; i < MISTER_AUDIO_CHUNK; i++) {
            int      ip = phase >> 16;                      /* arithmetic right-shift; may be negative */
            uint32_t fr = (uint32_t)(phase - (ip << 16));   /* [0, 65535] regardless of phase sign     */

            int sm1_l = read_sample_l(in_buf, ip - 1);
            int s0_l  = read_sample_l(in_buf, ip);
            int s1_l  = read_sample_l(in_buf, ip + 1);
            int s2_l  = read_sample_l(in_buf, ip + 2);
            int sm1_r = read_sample_r(in_buf, ip - 1);
            int s0_r  = read_sample_r(in_buf, ip);
            int s1_r  = read_sample_r(in_buf, ip + 1);
            int s2_r  = read_sample_r(in_buf, ip + 2);

            out_buf[2 * i + 0] = (int16_t)hermite4(sm1_l, s0_l, s1_l, s2_l, fr);
            out_buf[2 * i + 1] = (int16_t)hermite4(sm1_r, s0_r, s1_r, s2_r, fr);

            phase += STEP;
        }

        /* Save the last 3 input samples as tail for next tick's Hermite lookback.
         * Indices IN_FRAMES_PER_TICK-3, -2, -1 of THIS tick's in_buf become
         * "stream positions -3, -2, -1" relative to NEXT tick's in_buf[0]. */
        for (int k = 0; k < 3; k++) {
            int src = IN_FRAMES_PER_TICK - 3 + k;
            prev_tail_l[k] = in_buf[2 * src];
            prev_tail_r[k] = in_buf[2 * src + 1];
        }

        /* Shift phase into next tick's coordinate frame. Subtract IN_FRAMES_PER_TICK
         * because that's how far the engine's stream advanced this tick.
         * Typical end-of-tick phase = ~235.5 (16.16); subtracting 236 leaves
         * a small negative phase (~-0.5) meaning "next tick's first output
         * sample interpolates between prev_tail[2] and in_buf[0]". */
        resamp_phase = phase - ((int32_t)IN_FRAMES_PER_TICK << 16);

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
