/*
 * putscreen_bench.c -- OpenBOR_7533 putscreen/putother fps-bucket micro-benchmark
 * for the MiSTer A9. Measures the per-pixel cost of the FULL-SCREEN background-layer
 * blit kernel (engine putscreenx8p32, gamelib/screen32.c) on the real Cortex-A9,
 * WITHOUT loading a PAK or playing.
 *
 * run pinned to core 0:  taskset 0x01 ./putscreen_bench
 *
 * Why this exists: v12 [SP2] profiling showed putother is ~144 ms/frame on heavy
 * PAKs (He-Man) and is 100%% putscreen -- full-screen background-layer blits,
 * ~48 ms per call, 3 calls/frame. The opaque no-blend no-key path was already
 * NEON+unrolled (Step 22b). The remaining cost is the BLEND and/or COLOR-KEYED
 * full-screen path. This isolates those so the fps push is a tight
 * edit -> build -> ssh -> number loop.
 *
 * KERNEL replicated verbatim from putscreenx8p32 (screen32.c):
 *   - 8-bit indexed SOURCE layer, 32-bit (PIXEL_32) DEST screen, 256-entry u32 palette/remap.
 *   - Per pixel: idx = sp[i]; color = remap[idx]; (this is the pal[] gather)
 *   - COLOR KEY  : if(!idx) continue;            (skip transparent source index 0)
 *   - PLAIN copy : dp[i] = remap[idx];           (no key, no blend -- the fast path)
 *   - BLEND      : dp[i] = blendfp(remap[idx], dp[i]);
 *       blendfp is the engine's 32-bit blend (pixelformat.c blend_*32). It uses a
 *       256x256 per-channel LUT when blendtables[mode] is set (the shipped path), else
 *       per-channel arithmetic. We bench the LUT path (what ships) for the canonical
 *       MULTIPLY/SCREEN modes. Per channel via LUT:  out = tbl[(src_ch<<8)|dst_ch].
 *       Packing: _color(r,g,b) = (b<<16)|(g<<8)|r  (matches engine RGB32 byte order).
 *
 * Full-screen layer: SOURCE == screen size (a background layer is a full-frame blit),
 * so cw = FBW, ch = FBH, one blit covers the whole screen (matches the real call).
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/putscreen_bench.c -o putscreen_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

/* ---- 32-bit blend, verbatim from pixelformat.c v7533 (lines 131-146, 200-235) ----
 * color1 = front (source) colour, color2 = bg (dest) colour. RGB32 byte order:
 *   r = byte0, g = byte1, b = byte2.  Per-channel 256x256 LUT index = (src_ch<<8)|dst_ch. */
#define bs (color1 & 0xFF)
#define gs ((color1 & 0xFF00) >> 8)
#define rs (color1 >> 16)
#define bd (color2 & 0xFF)
#define gd ((color2 & 0xFF00) >> 8)
#define rd (color2 >> 16)
#define bi ((bs << 8) | bd)
#define gi ((gs << 8) | gd)
#define ri ((rs << 8) | rd)
#define _multiply(c1, c2) (((c1) * (c2)) >> 8)
#define _screen(c1, c2)   ((((c1) ^ 255) * ((c2) ^ 255) / 255) ^ 255)
#define _color(r, g, b)   (((b) << 16) | ((g) << 8) | (r))

#define BLEND_SCREEN   0
#define BLEND_MULTIPLY 1
static unsigned char *blendtables[2] = {0};   /* NULL -> arithmetic path; set -> LUT */

typedef uint32_t (*blend32fp)(uint32_t, uint32_t);

uint32_t blend_multiply32(uint32_t color1, uint32_t color2)
{
    unsigned char *tbl;
    if((tbl = blendtables[BLEND_MULTIPLY]))
        return _color(tbl[ri], tbl[gi], tbl[bi]);
    return _color(_multiply(color1 & 0xFF, color2 & 0xFF),
                  _multiply((color1 & 0xFF00) >> 8, (color2 & 0xFF00) >> 8),
                  _multiply(color1 >> 16, color2 >> 16));
}
uint32_t blend_screen32(uint32_t color1, uint32_t color2)
{
    unsigned char *tbl;
    if((tbl = blendtables[BLEND_SCREEN]))
        return _color(tbl[ri], tbl[gi], tbl[bi]);
    return _color(_screen(color1 & 0xFF, color2 & 0xFF),
                  _screen((color1 & 0xFF00) >> 8, (color2 & 0xFF00) >> 8),
                  _screen(color1 >> 16, color2 >> 16));
}

/* 256x256 per-channel blend LUT builder (65536 bytes; layout matches pixelformat.c) */
static void build_tbl32(unsigned char *tbl, int mode)
{
    unsigned i, j; int v;
    for(i = 0; i < 256; i++)
        for(j = 0; j < 256; j++)
        {
            v = (mode == BLEND_SCREEN) ? (int)_screen(i, j) : (int)_multiply(i, j);
            if(v > 255) v = 255; if(v < 0) v = 0;
            tbl[(i << 8) | j] = (unsigned char)v;
        }
}

/* === PLAIN copy: full-screen layer, no key, no blend (engine fast path) === */
static void put_copy(uint32_t *dp, int dw, const uint8_t *sp, int sw, int cw, int ch, const uint32_t *remap)
{
    int row, i;
    for(row = 0; row < ch; row++)
    {
        for(i = 0; i < cw; i++) dp[i] = remap[sp[i]];
        sp += sw; dp += dw;
    }
}

/* === COLOR-KEYED copy: skip transparent source index 0, no blend === */
static void put_copy_key(uint32_t *dp, int dw, const uint8_t *sp, int sw, int cw, int ch, const uint32_t *remap)
{
    int row, i;
    for(row = 0; row < ch; row++)
    {
        for(i = 0; i < cw; i++) { uint8_t idx = sp[i]; if(idx) dp[i] = remap[idx]; }
        sp += sw; dp += dw;
    }
}

/* === BLEND (no key): full-screen layer blended over dest via blendfp === */
static void put_blend(uint32_t *dp, int dw, const uint8_t *sp, int sw, int cw, int ch,
                      const uint32_t *remap, blend32fp bf)
{
    int row, i;
    for(row = 0; row < ch; row++)
    {
        for(i = 0; i < cw; i++) dp[i] = bf(remap[sp[i]], dp[i]);
        sp += sw; dp += dw;
    }
}

/* === BLEND + COLOR KEY: the slowest path (skip transparent, blend the rest) === */
static void put_blend_key(uint32_t *dp, int dw, const uint8_t *sp, int sw, int cw, int ch,
                          const uint32_t *remap, blend32fp bf)
{
    int row, i;
    for(row = 0; row < ch; row++)
    {
        for(i = 0; i < cw; i++) { uint8_t idx = sp[i]; if(idx) dp[i] = bf(remap[idx], dp[i]); }
        sp += sw; dp += dw;
    }
}

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (double)t.tv_sec*1e9 + (double)t.tv_nsec; }

/* best-of-7, denominator = total pixels touched (full-screen layer = cw*ch) */
#define BESTN 7
#define TIME(STMT, OUT) do{ STMT; double _best=1e30; int _r,_f; \
    for(_r=0;_r<BESTN;_r++){ double _t0=now_ns(); for(_f=0;_f<NREPS;_f++){ STMT; } \
        double _dt=now_ns()-_t0; if(_dt<_best)_best=_dt; } OUT=_best/((double)px*NREPS); }while(0)

int main(int argc, char **argv)
{
    /* full-screen background-layer resolutions (cost scales with pixel count) */
    int res[3][2] = { {320,224}, {480,272}, {960,480} };
    const char *resname[3] = { "320x224", "480x272", "960x480 (He-Man)" };
    int NREPS = (argc > 1) ? atoi(argv[1]) : 3;
    int ri_, i;

    /* 256-entry palette/remap (u32 ARGB). entry 0 is the color-key sentinel.
     * palette[0] forced nonzero so an OPAQUE pixel's color is never confused with key. */
    uint32_t remap[256];
    for(i = 0; i < 256; i++)
    {
        int r = (i * 5) & 0xFF, g = (i * 7) & 0xFF, b = (i * 11) & 0xFF;
        remap[i] = _color(r, g, b);
    }
    remap[0] = 0x00204060;

    /* build the two shipped blend LUTs (the path the engine actually takes) */
    static unsigned char tbl_screen[65536], tbl_multiply[65536];
    build_tbl32(tbl_screen,   BLEND_SCREEN);
    build_tbl32(tbl_multiply, BLEND_MULTIPLY);

    printf("== putscreen_bench (A9) -- putscreenx8p32 full-screen layer, 8bpp src -> 32bpp dest ==\n");
    printf("   blend path = 256x256 LUT (shipped). ~25%% source pixels are transparent (key skip).\n");
    printf("   ns/px denominator = full layer (cw*ch); best-of-%d, %d reps/measure.\n\n", BESTN, NREPS);

    printf("%-18s %12s %12s %12s %12s\n",
           "resolution", "copy", "copy+key", "blend", "blend+key");

    for(ri_ = 0; ri_ < 3; ri_++)
    {
        int FBW = res[ri_][0], FBH = res[ri_][1];
        long px = (long)FBW * FBH;
        uint32_t *dp = (uint32_t *)malloc((long)FBW * FBH * 4);   /* 32-bit dest screen */
        uint8_t  *sp = (uint8_t  *)malloc((long)FBW * FBH);       /* 8-bit indexed source layer */

        /* deterministic synthetic data: ~25%% transparent (index 0), rest 1..255 */
        for(i = 0; i < FBW * FBH; i++)
        {
            uint8_t v = (uint8_t)((i * 131 + 7) & 0xFF);
            if((i & 3) == 0) v = 0;                 /* ~25%% transparent for the key path */
            else if(v == 0) v = 1;                  /* keep non-key pixels opaque */
            sp[i] = v;
        }
        for(i = 0; i < FBW * FBH; i++) dp[i] = (uint32_t)(i * 0x01234567u);

        /* full-screen layer: source width == dest width, cw=FBW ch=FBH */
        double t_copy, t_copyk, t_blend, t_blendk;
        TIME(put_copy(dp, FBW, sp, FBW, FBW, FBH, remap), t_copy);
        TIME(put_copy_key(dp, FBW, sp, FBW, FBW, FBH, remap), t_copyk);

        blendtables[BLEND_MULTIPLY] = tbl_multiply;   /* LUT path (shipped) */
        TIME(put_blend(dp, FBW, sp, FBW, FBW, FBH, remap, blend_multiply32), t_blend);
        TIME(put_blend_key(dp, FBW, sp, FBW, FBW, FBH, remap, blend_multiply32), t_blendk);

        printf("%-18s %10.2f   %10.2f   %10.2f   %10.2f\n",
               resname[ri_], t_copy, t_copyk, t_blend, t_blendk);

        free(dp); free(sp);
    }

    /* per-frame cost at the He-Man native res (3 putscreen calls/frame, full layer) */
    {
        int FBW = 960, FBH = 480;
        long px = (long)FBW * FBH;
        uint32_t *dp = (uint32_t *)malloc((long)FBW * FBH * 4);
        uint8_t  *sp = (uint8_t  *)malloc((long)FBW * FBH);
        for(i = 0; i < FBW * FBH; i++){ uint8_t v=(uint8_t)((i*131+7)&0xFF); if((i&3)==0)v=0; else if(v==0)v=1; sp[i]=v; }
        for(i = 0; i < FBW * FBH; i++) dp[i] = (uint32_t)(i * 0x01234567u);
        double t_copy, t_blendk;
        TIME(put_copy(dp, FBW, sp, FBW, FBW, FBH, remap), t_copy);
        blendtables[BLEND_MULTIPLY] = tbl_multiply;
        TIME(put_blend_key(dp, FBW, sp, FBW, FBW, FBH, remap, blend_multiply32), t_blendk);
        printf("\n[per-frame estimate @ 960x480, 3 calls/frame]\n");
        printf("  copy      : %6.2f ms/frame\n", t_copy   * px * 3.0 / 1e6);
        printf("  blend+key : %6.2f ms/frame\n", t_blendk * px * 3.0 / 1e6);
        free(dp); free(sp);
    }

    return 0;
}
