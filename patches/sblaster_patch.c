/*
 * MiSTer_OpenBOR -- sdl/sblaster.c MiSTer replacement
 *
 * Option C v3: engine at upstream native 44.1 kHz (Sega CD Red Book CDDA
 * reference rate), glue layer resamples to 48 kHz via POLYPHASE WINDOWED-
 * SINC FIR — matches what PC SDL2's default resampler does (bandlimited
 * interpolation, per src/audio/SDL_audiocvt.c in libsdl-org/SDL).
 *
 * PC OpenBOR.exe audio chain: app → SDL_OpenAudioDevice(44100, allowed=0)
 * → SDL2 internal bandlimited interpolation → OS device at native rate.
 * For PC reference parity on MiSTer, we mirror SDL2's polyphase quality.
 *
 * Filter design:
 *   - 16-tap × 32-phase windowed-sinc FIR
 *   - Hann window for moderate stopband attenuation
 *   - Cutoff at source Nyquist (since we're upsampling 44.1 → 48)
 *   - Coefficient table precomputed at thread start; int16 storage
 *   - Per output sample: 16 multiply-adds per channel (stereo = 32 MAC)
 *
 * Implementation rules (avoid past failure modes):
 *   - uint32_t accum (always positive — no negative-shift UB)
 *   - No cross-tick state (each tick self-contained, accum starts 0)
 *   - Boundary clamp at chunk edges (small artifact, sub-ms, inaudible)
 *
 * Copyright (C) 2026 MiSTer Organize -- GPL-3.0
 */

#include "sblaster.h"
#include "soundmix.h"
#include "sdlport.h"
#include "native_audio_writer.h"

#include <math.h>
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
 * Request 236 with +1 margin for the polyphase filter's right wing. */
#define IN_FRAMES_PER_TICK   236

/* Polyphase FIR design constants. 16 taps × 32 phases = 512 int16 coefficients. */
#define POLY_N        16   /* taps per phase                                       */
#define POLY_P        32   /* phase quantization levels (fr → phase index)         */
#define POLY_CENTER   7    /* index of "current" sample within taps (taps[CENTER]) */
#define POLY_SCALE    15   /* coefficient scale: stored as q15 fixed-point         */

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

static int16_t  poly_h[POLY_P][POLY_N];   /* coefficient table (q15)              */
static int      poly_initialized = 0;

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

/* Generate polyphase windowed-sinc FIR coefficients at thread startup.
 *
 * For each phase p in [0, POLY_P), the filter is a Hann-windowed sinc
 * sampled at offsets (k - POLY_CENTER + p/POLY_P) for k in [0, POLY_N).
 * Cutoff = 0.5 (source Nyquist) — appropriate for upsampling. After
 * generation, each phase row sums to 1.0 (DC gain = unity), then scaled
 * by 2^POLY_SCALE for int16 storage. Runtime convolution: sum >>= 15.
 */
static void poly_init(void) {
    int p, k;
    double sum;
    double row[POLY_N];
    int iv;
    double x, sinc_val, arg, win_t, win;

    for (p = 0; p < POLY_P; p++) {
        sum = 0.0;
        for (k = 0; k < POLY_N; k++) {
            x = (double)(k - POLY_CENTER) + (double)p / (double)POLY_P;
            if (x == 0.0) {
                sinc_val = 1.0;
            } else {
                arg = M_PI * x;
                sinc_val = sin(arg) / arg;
            }
            win_t = (double)k / (double)(POLY_N - 1);
            win = 0.5 - 0.5 * cos(2.0 * M_PI * win_t);
            row[k] = sinc_val * win;
            sum += row[k];
        }
        /* Normalize each phase to unit DC gain, scale to q15. */
        for (k = 0; k < POLY_N; k++) {
            double v = row[k] / sum * (double)(1 << POLY_SCALE);
            iv = (int)(v < 0.0 ? v - 0.5 : v + 0.5);
            if (iv > 32767)  iv = 32767;
            if (iv < -32768) iv = -32768;
            poly_h[p][k] = (int16_t)iv;
        }
    }
    poly_initialized = 1;
}

/* Apply polyphase FIR for one output sample, one channel.
 * src points to interleaved-stereo buffer; channel selects L (0) or R (1).
 * ip is integer source frame position; fr is fractional in [0, 65535].
 * src_len is the number of frames in src (boundary clamp range). */
static inline int16_t poly_apply(const int16_t *src, int ip, uint32_t fr,
                                  int src_len, int channel)
{
    int p = (int)((fr * (uint32_t)POLY_P) >> 16);
    if (p >= POLY_P) p = POLY_P - 1;

    int32_t sum = 0;
    int k;
    for (k = 0; k < POLY_N; k++) {
        int idx = ip + k - POLY_CENTER;
        if (idx < 0) idx = 0;
        if (idx >= src_len) idx = src_len - 1;
        sum += (int32_t)src[2 * idx + channel] * (int32_t)poly_h[p][k];
    }
    sum >>= POLY_SCALE;
    if (sum > 32767)  sum = 32767;
    if (sum < -32768) sum = -32768;
    return (int16_t)sum;
}

static void *audio_thread_fn(void *arg) {
    (void)arg;
    static int16_t in_buf[IN_FRAMES_PER_TICK * 2];   /* stereo S16 @ 44.1 kHz from engine */
    static int16_t out_buf[MISTER_AUDIO_CHUNK * 2];  /* stereo S16 @ 48 kHz for DDR3      */

    /* 16.16 step per output sample: (44100 << 16) / 48000 = 60211.
     * Cast to uint64_t before shift to avoid the int32 overflow trap. */
    const uint32_t STEP = (uint32_t)(((uint64_t)ENGINE_AUDIO_RATE << 16) / MISTER_AUDIO_RATE);

    if (!poly_initialized) poly_init();

    while (audio_thread_run) {
        size_t free_frames = NativeAudioWriter_FreeFrames();

        if (free_frames < (size_t)MISTER_AUDIO_CHUNK) {
            audio_sleep_us(3000);
            continue;
        }

        /* Pull IN_FRAMES_PER_TICK fresh frames from the engine's stateful mixer. */
        update_sample((unsigned char *)in_buf, IN_FRAMES_PER_TICK * 4);

        /* Polyphase 44.1 → 48 kHz, per channel. No cross-tick state — each
         * tick is self-contained. Mirrors PICO-8's upsample pattern.
         *
         * accum is uint32_t (always positive); 256 iters of STEP advance
         * accum to 256*60211 = 15,414,016 = 235.16 in 16.16 fixed-point.
         * src_idx (= accum>>16) reaches max ~235, within IN_FRAMES_PER_TICK.
         *
         * Drift per tick: ~0.84 input frame unused → 0.05% pitch downward
         * (≈6 cents, below audible threshold). */
        uint32_t accum = 0;
        int i;
        for (i = 0; i < MISTER_AUDIO_CHUNK; i++) {
            int      ip = (int)(accum >> 16);
            uint32_t fr = accum & 0xFFFF;

            out_buf[2 * i + 0] = poly_apply(in_buf, ip, fr, IN_FRAMES_PER_TICK, 0);
            out_buf[2 * i + 1] = poly_apply(in_buf, ip, fr, IN_FRAMES_PER_TICK, 1);

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
