/*
 * blend_bench.c -- OpenBOR_7533 blend-kernel micro-benchmark for the MiSTer A9.
 *
 * Measures the per-pixel cost of OpenBOR's 16-bit blend kernel on the real
 * Cortex-A9, WITHOUT loading a PAK or playing. The fps bottleneck on heavy PAKs
 * (He-Man) is the per-frame blend pass; this isolates it so the fps push is a
 * tight edit -> build -> ssh -> number loop.
 *
 * Uses the EXACT blend macros + functions from engine/source/gamelib/
 * pixelformat.c (v7533). The functions check blendtables[] first:
 *   - tables NULL  -> ARITHMETIC path (per-pixel integer divides). This is what
 *                     our 16-bit build uses today (B3 patch nulls the tables).
 *   - tables built -> LUT path (3 byte lookups, NO divide). This benchmark
 *                     builds the tiny 16-bit tables and measures both, so we
 *                     know if re-enabling them for 16-bit is the fps win.
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/blend_bench.c -o blend_bench -lrt
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

#define BLEND_MULTIPLY 0
#define BLEND_SCREEN 1
#define BLEND_OVERLAY 2
#define BLEND_HARDLIGHT 3
#define BLEND_DODGE 4
#define BLEND_HALF 5
static unsigned char *blendtables[8] = {0};   /* NULL -> arithmetic path */

/* ---- the 6 blend functions, verbatim from pixelformat.c (both paths) ---- */
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

/* ---- 16-bit blend-table builder. 5120 bytes: [0..1023] = 5-bit red/blue
 * channel-pair results (index (c1<<5)|c2), [1024..5119] = 6-bit green
 * (index 1024+(c1<<6)|c2). dodge clamped at c1==max to avoid div-by-zero;
 * exact engine clamp differs slightly but is irrelevant to lookup SPEED. ---- */
static void build_table(unsigned char *tbl, int bx){
    int c1,c2,v;
    for(c1=0;c1<32;c1++) for(c2=0;c2<32;c2++){
        switch(bx){
            case BLEND_SCREEN:    v=_screen16(c1,c2,0x1F); break;
            case BLEND_MULTIPLY:  v=_multiply16(c1,c2,0x1F); break;
            case BLEND_OVERLAY:   v=_overlay16(c1,c2,0x0F,0x1F); break;
            case BLEND_HARDLIGHT: v=_hardlight16(c1,c2,0x0F,0x1F); break;
            case BLEND_DODGE:     v=(c1>=0x1F)?0x1F:_dodge16(c1,c2,0x1F); break;
            case BLEND_HALF:      v=(c1+c2)>>1; break;
            default: v=0;
        }
        if(v>0x1F) v=0x1F; if(v<0) v=0;
        tbl[(c1<<5)|c2]=(unsigned char)v;
    }
    for(c1=0;c1<64;c1++) for(c2=0;c2<64;c2++){
        switch(bx){
            case BLEND_SCREEN:    v=_screen16(c1,c2,0x3F); break;
            case BLEND_MULTIPLY:  v=_multiply16(c1,c2,0x3F); break;
            case BLEND_OVERLAY:   v=_overlay16(c1,c2,0x1F,0x3F); break;
            case BLEND_HARDLIGHT: v=_hardlight16(c1,c2,0x1F,0x3F); break;
            case BLEND_DODGE:     v=(c1>=0x3F)?0x3F:_dodge16(c1,c2,0x3F); break;
            case BLEND_HALF:      v=(c1+c2)>>1; break;
            default: v=0;
        }
        if(v>0x3F) v=0x3F; if(v<0) v=0;
        tbl[1024+((c1<<6)|c2)]=(unsigned char)v;
    }
}

static void blit(uint16_t *fb, int fbw, int fbh,
                 const uint8_t *spr, int sw, int sh,
                 const uint16_t *pal, blend16fp bf, int nsprites)
{
    int n,row,col;
    for(n=0;n<nsprites;n++){
        int x=(n*97)%(fbw-sw), y=(n*61)%(fbh-sh);
        for(row=0;row<sh;row++){
            uint16_t *d = fb + (long)(y+row)*fbw + x;
            const uint8_t *s = spr + (long)row*sw;
            for(col=0;col<sw;col++){ uint8_t idx=s[col]; if(idx) d[col]=bf(pal[idx],d[col]); }
        }
    }
}

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

static double measure(blend16fp bf, uint16_t *fb,int FBW,int FBH, uint8_t *spr,int SW,int SH,
                      uint16_t *pal, int NSPRITES,int NFRAMES, long blended_px){
    blit(fb,FBW,FBH,spr,SW,SH,pal,bf,NSPRITES); /* warmup */
    double best=1e30; int r,f;
    for(r=0;r<3;r++){ double t0=now_ns(); for(f=0;f<NFRAMES;f++) blit(fb,FBW,FBH,spr,SW,SH,pal,bf,NSPRITES);
        double dt=now_ns()-t0; if(dt<best)best=dt; }
    return best/blended_px;
}

int main(int argc,char**argv){
    int FBW=960,FBH=480, SW=64,SH=96;
    int NSPRITES=(argc>1)?atoi(argv[1]):150;
    int NFRAMES =(argc>2)?atoi(argv[2]):3;
    uint16_t *fb=(uint16_t*)malloc((long)FBW*FBH*2);
    uint8_t *spr=(uint8_t*)malloc((long)SW*SH);
    uint16_t pal[256]; int i,opaque=0;
    for(i=0;i<256;i++){ int r=(i*5)%31,g=(i*7)%63,b=(i*11)%31; pal[i]=(uint16_t)((b<<11)|(g<<5)|r); }
    for(i=0;i<SW*SH;i++){ uint8_t v=(uint8_t)((i*131+7)%255); if((i%5)==0)v=0; spr[i]=v; if(v)opaque++; }
    for(i=0;i<FBW*FBH;i++) fb[i]=(uint16_t)(i*0x1234);

    blend16fp modes[6]={blend_screen16,blend_multiply16,blend_overlay16,blend_hardlight16,blend_dodge16,blend_half16};
    const char *names[6]={"screen","multiply","overlay","hardlight","dodge","half"};
    int bxof[6]={BLEND_SCREEN,BLEND_MULTIPLY,BLEND_OVERLAY,BLEND_HARDLIGHT,BLEND_DODGE,BLEND_HALF};
    long blended_px=(long)NSPRITES*opaque*NFRAMES;

    static unsigned char tblstore[6][5120];
    int m; for(m=0;m<6;m++) build_table(tblstore[bxof[m]], bxof[m]);

    printf("== blend_bench (A9) fb=%dx%d sprite=%dx%d (%d opaque) nsprites=%d nframes=%d ==\n",
           FBW,FBH,SW,SH,opaque,NSPRITES,NFRAMES);
    printf("%-10s %12s %12s %9s\n","mode","arith ns/px","LUT ns/px","speedup");
    double arith[6],lut[6];
    for(m=0;m<8;m++) blendtables[m]=0;                      /* arithmetic path */
    for(m=0;m<6;m++) arith[m]=measure(modes[m],fb,FBW,FBH,spr,SW,SH,pal,NSPRITES,NFRAMES,blended_px);
    for(m=0;m<6;m++) blendtables[bxof[m]]=tblstore[bxof[m]];/* LUT path */
    for(m=0;m<6;m++) lut[m]=measure(modes[m],fb,FBW,FBH,spr,SW,SH,pal,NSPRITES,NFRAMES,blended_px);

    for(m=0;m<6;m++) printf("%-10s %12.2f %12.2f %8.2fx\n",names[m],arith[m],lut[m],arith[m]/lut[m]);
    free(fb); free(spr); return 0;
}
