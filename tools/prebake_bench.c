/*
 * prebake_bench.c -- settles the "#1 prebake" question for the blend-bound case.
 *
 * He-Man is ~65% blend, and the blend inner loop does dst = blend(palette[idx], dst)
 * -- a per-pixel palette GATHER (8-bit index -> 16-bit LUT). If sprites were
 * pre-decoded to 16-bit at load, the blend becomes dst = blend(src16[i], dst):
 * no gather. This bench measures whether that removes enough memory traffic to
 * matter, for both the cheap (multiply) and expensive (dodge) LUT modes:
 *
 *   blend INDEXED  : color1 = pal[src8[i]]   (current -- gather)
 *   blend PREBAKED : color1 = src16[i]        (proposed -- direct 16-bit load)
 *
 * Plus the one-time PREBAKE cost (dst16[i]=pal[src8[i]] throughput) -> extrapolated
 * to a He-Man-scale sprite-pixel count, to check it doesn't blow up load time.
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/prebake_bench.c -o prebake_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>

#define _b1 (color1>>11)
#define _g1 ((color1&0x7E0)>>5)
#define _r1 (color1&0x1F)
#define _b2 (color2>>11)
#define _g2 ((color2&0x7E0)>>5)
#define _r2 (color2&0x1F)
#define _bi ((_b1<<5)|_b2)
#define _gi (((_g1<<6)|_g2)+1024)
#define _ri ((_r1<<5)|_r2)
#define _multiply16(c1,c2,m) ((c1)*(c2)/(m))
#define _color16(r,g,b) ( ((b)<<11)|((g)<<5)|(r) )

static void build_mul_tbl(unsigned char *t){
    int c1,c2,v;
    for(c1=0;c1<32;c1++)for(c2=0;c2<32;c2++){ v=_multiply16(c1,c2,0x1F); if(v>0x1F)v=0x1F; t[(c1<<5)|c2]=(unsigned char)v; }
    for(c1=0;c1<64;c1++)for(c2=0;c2<64;c2++){ v=_multiply16(c1,c2,0x3F); if(v>0x3F)v=0x3F; t[1024+((c1<<6)|c2)]=(unsigned char)v; }
}

/* blend inner loop, INDEXED source (current engine: palette[idx] gather) */
static void blend_indexed(const uint8_t *src,int n,const uint16_t *pal,uint16_t *dst,const unsigned char *tbl){
    int i; for(i=0;i<n;i++){ uint16_t color1=pal[src[i]], color2=dst[i]; dst[i]=(uint16_t)_color16(tbl[_ri],tbl[_gi],tbl[_bi]); }
}
/* blend inner loop, PREBAKED 16-bit source (no gather) */
static void blend_prebaked(const uint16_t *src16,int n,uint16_t *dst,const unsigned char *tbl){
    int i; for(i=0;i<n;i++){ uint16_t color1=src16[i], color2=dst[i]; dst[i]=(uint16_t)_color16(tbl[_ri],tbl[_gi],tbl[_bi]); }
}
/* one-time prebake convert: 8bpp index -> 16bpp */
static void prebake(const uint8_t *src,int n,const uint16_t *pal,uint16_t *dst16){
    int i; for(i=0;i<n;i++) dst16[i]=pal[src[i]];
}

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

int main(int argc,char**argv){
    int N = (argc>1)?atoi(argv[1]):4915;   /* ~one He-Man sprite worth of opaque px */
    int REP = (argc>2)?atoi(argv[2]):2000; /* sprites-per-window-ish */
    uint8_t *src = (uint8_t*)malloc(N);
    uint16_t pal[256], *src16=(uint16_t*)malloc(N*2), *dst=(uint16_t*)malloc(N*2);
    int i,r; for(i=0;i<256;i++) pal[i]=(uint16_t)(i*0x0123+7);
    for(i=0;i<N;i++){ src[i]=(uint8_t)((i*131+7)&0xFF); src16[i]=pal[src[i]]; }
    for(i=0;i<N;i++) dst[i]=(uint16_t)(i*0x2531+3);
    static unsigned char tbl[5120]; build_mul_tbl(tbl);
    long px=(long)N*REP;

    printf("== prebake_bench (A9) N=%d REP=%d (%ld px/measure) ==\n",N,REP,px);
    printf("blend inner loop (multiply LUT), ns per blended px:\n");
    double t0,dt,bi=1e30,bp=1e30;
    blend_indexed(src,N,pal,dst,tbl); for(r=0;r<3;r++){ t0=now_ns(); int k; for(k=0;k<REP;k++) blend_indexed(src,N,pal,dst,tbl); dt=now_ns()-t0; if(dt<bi)bi=dt; } bi/=px;
    blend_prebaked(src16,N,dst,tbl); for(r=0;r<3;r++){ t0=now_ns(); int k; for(k=0;k<REP;k++) blend_prebaked(src16,N,dst,tbl); dt=now_ns()-t0; if(dt<bp)bp=dt; } bp/=px;
    printf("  INDEXED  (pal[idx] gather) : %6.2f ns/px\n", bi);
    printf("  PREBAKED (direct 16-bit)   : %6.2f ns/px   (%.2fx vs indexed)\n", bp, bi/bp);

    /* one-time prebake convert throughput + He-Man-scale load-cost extrapolation */
    double bc=1e30;
    prebake(src,N,pal,src16); for(r=0;r<3;r++){ t0=now_ns(); int k; for(k=0;k<REP;k++) prebake(src,N,pal,src16); dt=now_ns()-t0; if(dt<bc)bc=dt; } bc/=px;
    printf("\nprebake convert (8bpp->16bpp): %6.2f ns/px\n", bc);
    /* He-Man: ~hundreds of sprites; assume ~10M total sprite pixels as an upper bound */
    double hm_px = 10e6;
    printf("  one-time load cost @ %.0fM sprite px: %.1f ms (added to load)\n", hm_px/1e6, bc*hm_px/1e6);
    printf("\nverdict hint: if PREBAKED blend is materially faster AND load cost is small,\n");
    printf("prebake is the blend-bound (He-Man) fps lever -- gated by flash/remap safety.\n");
    free(src);free(src16);free(dst); return 0;
}
