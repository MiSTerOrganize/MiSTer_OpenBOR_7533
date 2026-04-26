/*
 * MiSTer_OpenBOR -- sdl/sblaster.c MiSTer replacement
 *
 * Replaces OpenBOR 3979's SDL-audio backend with a pthread that drains
 * update_sample() into the DDR3 audio ring buffer. No SDL_OpenAudio,
 * no ALSA. The FPGA pulls samples from the ring at 48 kHz.
 *
 * PATCH: when BUILD_MISTER is defined, replace the ENTIRE contents of
 * sdl/sblaster.c with this file. apply_patches.py handles the swap.
 *
 * OpenBOR's mixer exposes update_sample(stream, len_bytes) -- it writes
 * interleaved PCM into the caller-supplied buffer. We force 16-bit
 * stereo @ 48 kHz to match the FPGA pipeline.
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

/* OpenBOR calls update_sample() to render mixer output into a byte buffer. */
extern void update_sample(unsigned char *buf, int size);

#define MISTER_AUDIO_RATE    48000
#define MISTER_AUDIO_CHUNK   256        /* frames per wake-up ~5.3 ms */
#define MISTER_CHUNK_BYTES   (MISTER_AUDIO_CHUNK * 4)  /* stereo S16 */

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
    static uint8_t chunk[MISTER_CHUNK_BYTES];

    while (audio_thread_run) {
        /* How much room does the FPGA ring have? */
        size_t free_frames = NativeAudioWriter_FreeFrames();

        /* Render and submit up to one chunk per wake-up. Stop early if
         * the ring is nearly full -- we'll fill more on the next tick. */
        if (free_frames >= MISTER_AUDIO_CHUNK) {
            update_sample(chunk, MISTER_CHUNK_BYTES);
            NativeAudioWriter_Submit((const int16_t *)chunk, MISTER_AUDIO_CHUNK);
        }

        /* Target: keep the ring comfortably full without spinning. With
         * a 64 KiB / 48 kHz ring (~341 ms), waking every ~3 ms gives us
         * large headroom and very low CPU. */
        audio_sleep_us(3000);
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
