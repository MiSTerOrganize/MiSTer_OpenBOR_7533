/*
 * audio_bench.c -- the MiSTer audio glue resample cost (sblaster_patch.c).
 * Engine mixes NN at 44100 (soundmix.c FIX_TO_INT); the glue ZOH-resamples
 * 44100 -> 48000 stereo into the DDR3 ring. Audio runs on its own core, so the
 * question is just "what fraction of a core does it cost?" -- measured here vs
 * the hard 48000 stereo-samples/sec budget.
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/audio_bench.c -o audio_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>

#define SRC_RATE 44100
#define DST_RATE 48000

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

/* ZOH (nearest/sample-hold) resample 44100->48000, interleaved stereo s16.
 * STEP uses the 64-bit-intermediate pattern (the negative-STEP trap fix). */
static void resample_zoh(const int16_t *src, int src_frames, int16_t *dst, int dst_frames){
    const uint32_t STEP = (uint32_t)(((uint64_t)SRC_RATE << 16) / DST_RATE);
    uint32_t phase = 0; int o;
    for(o=0;o<dst_frames;o++){
        int ip = phase >> 16;
        if(ip >= src_frames) ip = src_frames-1;
        dst[2*o]   = src[2*ip];
        dst[2*o+1] = src[2*ip+1];
        phase += STEP;
    }
}

int main(void){
    int SRC_F = SRC_RATE/10;      /* 0.1s of source */
    int DST_F = DST_RATE/10;      /* 0.1s of output */
    int REP = 100, r;            /* -> 10s of audio total per measure */
    int16_t *src = malloc((long)SRC_F*2*2), *dst = malloc((long)DST_F*2*2);
    int i; for(i=0;i<SRC_F*2;i++) src[i]=(int16_t)((i*131)&0x7fff);
    long out_frames = (long)DST_F*REP;

    printf("== audio_bench (A9) ZOH resample %d->%d stereo ==\n", SRC_RATE, DST_RATE);
    resample_zoh(src,SRC_F,dst,DST_F);
    double best=1e30; for(r=0;r<3;r++){ double t0=now_ns(); int k; for(k=0;k<REP;k++) resample_zoh(src,SRC_F,dst,DST_F); double dt=now_ns()-t0; if(dt<best)best=dt; }
    double ns_per_frame = best/out_frames;
    double ns_per_sec_audio = ns_per_frame * DST_RATE;   /* cost to produce 1s of audio */
    printf("resample: %.2f ns per stereo frame\n", ns_per_frame);
    printf("cost to produce 1s of audio: %.3f ms  =  %.3f%% of one 800MHz core\n",
           ns_per_sec_audio/1e6, ns_per_sec_audio/1e9*100.0);
    printf("(verdict: audio is negligible unless this is a large %% -- then revisit core-0 sharing.)\n");
    free(src); free(dst); return 0;
}
