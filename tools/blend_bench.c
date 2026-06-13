/*
 * blend_bench.c -- OpenBOR_7533 blend-kernel micro-benchmark for the MiSTer A9.
 *
 * Purpose: measure the per-pixel cost of OpenBOR's 16-bit blend kernel on the
 * real Cortex-A9, WITHOUT loading a PAK or playing. The fps bottleneck on heavy
 * PAKs (He-Man) is the per-frame blend pass; this isolates that cost so the fps
 * push is a tight edit -> build -> ssh -> number loop.
 *
 * It uses the EXACT blend macros + functions from engine/source/gamelib/
 * pixelformat.c (v7533), with blendtables NULL -> the arithmetic path, which is
 * what our 16-bit build (B3 patch) uses. So optimizations validated here
 * transfer 1:1 to the engine kernel.
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/blend_bench.c -o blend_bench -lrt
 * Run:        ssh / WinSCP: ./blend_bench
 *
 * Output: per blend mode, ns/blended-pixel (calibration-independent -- the
 * thing optimizations target) + a ms/frame estimate for a representative
 * sprite load. Calibrate the load once against a real He-Man [BLD] reading.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

/* ---- blend macros, verbatim from pixelformat.c v7533 (lines 378-393) ---- */
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
#define _screen16(c1,c2,m) ((((c1)^(m))*((c2)^(m))/(m))^(m))
#define _hardlight16(c1,c2,m,m2) ((c1)<(m)?_multiply16(((c1)<<1),(c2),(m2)):_screen16((((c1)-(m))<<1),(c2),(m2)))
#define _overlay16(c1,c2,m,m2) ((c2)<(m)?_multiply16(((c2)<<1),(c1),(m2)):_screen16((((c2)-(m))<<1),(c1),(m2)))
#define _dodge16(c1,c2,m) ((c2)*(m)/((m)-(c1)))
#define _color16(r,g,b) ( ((b)<<11)|((g)<<5)|(r) )

typedef uint16_t (*blend16fp)(uint16_t, uint16_t);

/* blendtables all NULL -> functions take the arithmetic path (our 16-bit build) */
static unsigned char *blendtables[8] = {0};
#define BLEND_MULTIPLY 0
#define BLEND_SCREEN 1
#define BLEND_OVERLAY 2
#define BLEND_HARDLIGHT 3
#define BLEND_DODGE 4
#define BLEND_HALF 5

/* ---- the 6 blend functions, verbatim arithmetic path from pixelformat.c ---- */
uint16_t blend_multiply16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_MULTIPLY])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_multiply16(_r1,_r2,0x1F), _multiply16(_g1,_g2,0x3F), _multiply16(_b1,_b2,0x1F));
}
uint16_t blend_screen16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_SCREEN])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_screen16(_r1,_r2,0x1F), _screen16(_g1,_g2,0x3F), _screen16(_b1,_b2,0x1F));
}
uint16_t blend_overlay16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_OVERLAY])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_overlay16(_r1,_r2,0x0F,0x1F), _overlay16(_g1,_g2,0x1F,0x3F), _overlay16(_b1,_b2,0x0F,0x1F));
}
uint16_t blend_hardlight16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_HARDLIGHT])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_hardlight16(_r1,_r2,0x0F,0x1F), _hardlight16(_g1,_g2,0x1F,0x3F), _hardlight16(_b1,_b2,0x0F,0x1F));
}
uint16_t blend_dodge16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_DODGE])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_dodge16(_r1,_r2,0x1F), _dodge16(_g1,_g2,0x3F), _dodge16(_b1,_b2,0x1F));
}
uint16_t blend_half16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_HALF])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16((_r1+_r2)>>1, (_g1+_g2)>>1, (_b1+_b2)>>1);
}

/* ---- representative blit: 8-bit indexed sprite -> 16-bit fb, per-pixel blend.
 * Mirrors putsprite_x8p16's inner loop (index -> palette lookup -> dest read ->
 * blend -> dest write), skipping transparent (idx 0). The dest fb is large
 * (He-Man native) so dest reads/writes hit memory, matching the real cache
 * behavior that makes this memory-bound. ---- */
static void blit(uint16_t *fb, int fbw, int fbh,
                 const uint8_t *spr, int sw, int sh,
                 const uint16_t *pal, blend16fp bf, int nsprites)
{
    int n, row, col;
    for(n=0; n<nsprites; n++){
        int x = (n*97) % (fbw - sw);
        int y = (n*61) % (fbh - sh);
        for(row=0; row<sh; row++){
            uint16_t *d = fb + (long)(y+row)*fbw + x;
            const uint8_t *s = spr + (long)row*sw;
            for(col=0; col<sw; col++){
                uint8_t idx = s[col];
                if(idx) d[col] = bf(pal[idx], d[col]);
            }
        }
    }
}

static double now_ns(void){
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (double)t.tv_sec*1e9 + (double)t.tv_nsec;
}

int main(int argc, char **argv){
    /* He-Man native framebuffer; large enough that dest is memory-bound. */
    int FBW = 960, FBH = 480;
    /* Representative character sprite. ~80% opaque (real chars have transparent
     * borders); tune SW/SH/NSPRITES at calibration to match real [BLD]. */
    int SW = 64, SH = 96;
    int NSPRITES = (argc>1)? atoi(argv[1]) : 1200; /* sprites per frame */
    int NFRAMES  = (argc>2)? atoi(argv[2]) : 30;

    uint16_t *fb  = (uint16_t*)malloc((long)FBW*FBH*2);
    uint8_t  *spr = (uint8_t*)malloc((long)SW*SH);
    uint16_t pal[256];
    int i;
    /* channels kept below max (r,b<=30, g<=62) so blend_dodge16's (m-c1)
     * divisor is never 0 -- avoids SIGFPE on synthetic data; per-pixel cost
     * (the integer divide) is identical regardless of the divisor value. */
    for(i=0;i<256;i++){ int r=(i*5)%31, g=(i*7)%63, b=(i*11)%31;
        pal[i] = (uint16_t)((b<<11)|(g<<5)|r); }
    int opaque=0;
    for(i=0;i<SW*SH;i++){ uint8_t v = (uint8_t)((i*131+7)%255); /* 0..254 */
        if((i%5)==0) v=0;                /* ~20% transparent */
        spr[i]=v; if(v) opaque++; }
    for(i=0;i<FBW*FBH;i++) fb[i]=(uint16_t)(i*0x1234);

    blend16fp modes[6] = {blend_screen16,blend_multiply16,blend_overlay16,
                          blend_hardlight16,blend_dodge16,blend_half16};
    const char *names[6] = {"screen","multiply","overlay","hardlight","dodge","half"};

    long blended_px = (long)NSPRITES * opaque * NFRAMES; /* blended pixels per run */

    printf("== blend_bench (A9) : fb=%dx%d sprite=%dx%d (%d opaque/%d) nsprites=%d nframes=%d ==\n",
           FBW,FBH,SW,SH,opaque,SW*SH,NSPRITES,NFRAMES);
    printf("%-10s  %10s  %12s\n","mode","ms/frame","ns/blend-px");
    int m;
    for(m=0;m<6;m++){
        /* warmup */
        blit(fb,FBW,FBH,spr,SW,SH,pal,modes[m],NSPRITES);
        double best=1e30; int r;
        for(r=0;r<3;r++){
            double t0=now_ns();
            int f; for(f=0;f<NFRAMES;f++) blit(fb,FBW,FBH,spr,SW,SH,pal,modes[m],NSPRITES);
            double dt=now_ns()-t0; if(dt<best) best=dt;
        }
        double ms_per_frame = best/1e6/NFRAMES;
        double ns_per_px    = best/blended_px;
        printf("%-10s  %10.2f  %12.3f\n", names[m], ms_per_frame, ns_per_px);
    }
    free(fb); free(spr);
    return 0;
}
