/*
 * blend_bench.c -- OpenBOR_7533 sprite-kernel micro-benchmark for the MiSTer A9.
 *
 * Measures the per-pixel cost of OpenBOR's 16-bit sprite blit kernels on the
 * real Cortex-A9, WITHOUT loading a PAK or playing. The fps bottleneck on heavy
 * PAKs (He-Man) is the per-frame sprite pass; this isolates every lever so the
 * fps push is a tight edit -> build -> ssh -> number loop.
 *
 * v3 sections:
 *   1. BLEND modes  : arithmetic (divide) vs LUT (table) -- shipped as B3.
 *   2. KILL fp-call : per-pixel function-pointer dispatch vs a per-mode
 *                     specialized blit with the LUT inlined + table hoisted.
 *   3. COPY path    : the opaque (non-blend) blit -- the MAJORITY of real
 *                     frames. indexed-scalar (today) vs prebaked-16 scalar vs
 *                     prebaked-16 NEON (8-wide masked copy). NEON needs 16-bit
 *                     source (A9 has no gather for pal[idx]) -> measures the
 *                     payoff of a load-time sprite pre-decode to 565.
 *   4. CACHE        : single-table vs all-5-tables-interleaved (25 KB) to see
 *                     if L1 pressure is part of the ~94 ns/px LUT floor.
 *
 * Denominator for EVERY ns/px is the OPAQUE pixel count (the visible work), so
 * NEON's cost of touching transparent pixels is honestly included.
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/blend_bench.c -o blend_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <arm_neon.h>

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

/* ---- 16-bit blend-table builder (5120 bytes; layout matches pixelformat.c) ---- */
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

/* fixed pseudo-random sprite placement (deterministic across variants) */
#define SPR_X(n,fbw,sw) (((n)*97)%((fbw)-(sw)))
#define SPR_Y(n,fbh,sh) (((n)*61)%((fbh)-(sh)))

/* === BLEND path: per-pixel function-pointer dispatch (engine today) === */
static void blit_blend_fp(uint16_t *fb,int fbw,int fbh,const uint8_t *spr,int sw,int sh,
                          const uint16_t *pal, blend16fp bf, int nsprites){
    int n,row,col;
    for(n=0;n<nsprites;n++){
        int x=SPR_X(n,fbw,sw), y=SPR_Y(n,fbh,sh);
        for(row=0;row<sh;row++){
            uint16_t *d = fb + (long)(y+row)*fbw + x;
            const uint8_t *s = spr + (long)row*sw;
            for(col=0;col<sw;col++){ uint8_t idx=s[col]; if(idx) d[col]=bf(pal[idx],d[col]); }
        }
    }
}

/* === BLEND path: per-mode specialized blit, LUT inlined + table hoisted (#2) === */
#define MK_BLIT_LUT(NAME, BX) \
static void blit_lut_##NAME(uint16_t *fb,int fbw,int fbh,const uint8_t *spr,int sw,int sh, \
                            const uint16_t *pal,int nsprites){ \
    const unsigned char *tbl = blendtables[BX]; int n,row,col; \
    for(n=0;n<nsprites;n++){ int x=SPR_X(n,fbw,sw), y=SPR_Y(n,fbh,sh); \
        for(row=0;row<sh;row++){ uint16_t *d=fb+(long)(y+row)*fbw+x; const uint8_t *s=spr+(long)row*sw; \
            for(col=0;col<sw;col++){ uint8_t idx=s[col]; if(idx){ \
                uint16_t color1=pal[idx], color2=d[col]; \
                d[col]=(uint16_t)_color16(tbl[_ri],tbl[_gi],tbl[_bi]); } } } } }
MK_BLIT_LUT(screen,    BLEND_SCREEN)
MK_BLIT_LUT(multiply,  BLEND_MULTIPLY)
MK_BLIT_LUT(overlay,   BLEND_OVERLAY)
MK_BLIT_LUT(hardlight, BLEND_HARDLIGHT)
MK_BLIT_LUT(dodge,     BLEND_DODGE)
typedef void (*blit_lut_fp)(uint16_t*,int,int,const uint8_t*,int,int,const uint16_t*,int);

/* === COPY path: opaque indexed blit (engine today, alpha==0) === */
static void blit_copy_idx(uint16_t *fb,int fbw,int fbh,const uint8_t *spr,int sw,int sh,
                          const uint16_t *pal,int nsprites){
    int n,row,col;
    for(n=0;n<nsprites;n++){ int x=SPR_X(n,fbw,sw), y=SPR_Y(n,fbh,sh);
        for(row=0;row<sh;row++){ uint16_t *d=fb+(long)(y+row)*fbw+x; const uint8_t *s=spr+(long)row*sw;
            for(col=0;col<sw;col++){ uint8_t idx=s[col]; if(idx) d[col]=pal[idx]; } } }
}
/* === COPY path: prebaked 16-bit source, scalar (removes pal[] gather) === */
static void blit_copy16_scalar(uint16_t *fb,int fbw,int fbh,const uint16_t *spr16,int sw,int sh,int nsprites){
    int n,row,col;
    for(n=0;n<nsprites;n++){ int x=SPR_X(n,fbw,sw), y=SPR_Y(n,fbh,sh);
        for(row=0;row<sh;row++){ uint16_t *d=fb+(long)(y+row)*fbw+x; const uint16_t *s=spr16+(long)row*sw;
            for(col=0;col<sw;col++){ uint16_t v=s[col]; if(v) d[col]=v; } } }
}
/* === COPY path: prebaked 16-bit source, NEON 8-wide masked copy (#3) ===
 * transparent sentinel = 0 (565 black). mask=(src==0) -> keep dst, else src. */
static void blit_copy16_neon(uint16_t *fb,int fbw,int fbh,const uint16_t *spr16,int sw,int sh,int nsprites){
    uint16x8_t zero = vdupq_n_u16(0);
    int n,row,col;
    for(n=0;n<nsprites;n++){ int x=SPR_X(n,fbw,sw), y=SPR_Y(n,fbh,sh);
        for(row=0;row<sh;row++){ uint16_t *d=fb+(long)(y+row)*fbw+x; const uint16_t *s=spr16+(long)row*sw;
            for(col=0; col+8<=sw; col+=8){
                uint16x8_t sv=vld1q_u16(s+col), dv=vld1q_u16(d+col);
                uint16x8_t mask=vceqq_u16(sv,zero);     /* 0xFFFF where transparent */
                vst1q_u16(d+col, vbslq_u16(mask, dv, sv));
            }
            for(; col<sw; col++){ uint16_t v=s[col]; if(v) d[col]=v; }
        } }
}
/* === CACHE: cycle all 5 LUT tables across the sprite batch (25 KB working set) === */
static void blit_mixed5(uint16_t *fb,int fbw,int fbh,const uint8_t *spr,int sw,int sh,
                        const uint16_t *pal, blend16fp *modes5, int nsprites){
    int n,row,col;
    for(n=0;n<nsprites;n++){ blend16fp bf=modes5[n%5]; int x=SPR_X(n,fbw,sw), y=SPR_Y(n,fbh,sh);
        for(row=0;row<sh;row++){ uint16_t *d=fb+(long)(y+row)*fbw+x; const uint8_t *s=spr+(long)row*sw;
            for(col=0;col<sw;col++){ uint8_t idx=s[col]; if(idx) d[col]=bf(pal[idx],d[col]); } } }
}

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

/* time a statement block: 3 reps of NFRAMES calls, best-of; ns per opaque px */
#define TIME(STMT, OUT) do{ STMT; double _best=1e30; int _r,_f; \
    for(_r=0;_r<3;_r++){ double _t0=now_ns(); for(_f=0;_f<NFRAMES;_f++){ STMT; } \
        double _dt=now_ns()-_t0; if(_dt<_best)_best=_dt; } OUT=_best/(double)blended_px; }while(0)

int main(int argc,char**argv){
    int FBW=960,FBH=480, SW=64,SH=96;
    int NSPRITES=(argc>1)?atoi(argv[1]):150;
    int NFRAMES =(argc>2)?atoi(argv[2]):3;
    uint16_t *fb=(uint16_t*)malloc((long)FBW*FBH*2);
    uint8_t *spr=(uint8_t*)malloc((long)SW*SH);
    uint16_t *spr16=(uint16_t*)malloc((long)SW*SH*2);
    uint16_t pal[256]; int i,opaque=0;
    for(i=0;i<256;i++){ int r=(i*5)%31,g=(i*7)%63,b=(i*11)%31; pal[i]=(uint16_t)((b<<11)|(g<<5)|r); }
    /* sprite indices: ~20% transparent (idx 0). palette[0] forced nonzero so an
     * opaque pixel never collides with the NEON transparent sentinel (0). */
    pal[0]=0x0841;
    for(i=0;i<SW*SH;i++){ uint8_t v=(uint8_t)((i*131+7)%255); if((i%5)==0)v=0; if(v==0 && (i%5)!=0)v=1; spr[i]=v; if(v)opaque++; }
    for(i=0;i<SW*SH;i++){ uint8_t idx=spr[i]; spr16[i]= idx? pal[idx] : 0; }   /* prebaked, transparent=0 */
    for(i=0;i<FBW*FBH;i++) fb[i]=(uint16_t)(i*0x1234);

    blend16fp modes[6]={blend_screen16,blend_multiply16,blend_overlay16,blend_hardlight16,blend_dodge16,blend_half16};
    const char *names[6]={"screen","multiply","overlay","hardlight","dodge","half"};
    int bxof[6]={BLEND_SCREEN,BLEND_MULTIPLY,BLEND_OVERLAY,BLEND_HARDLIGHT,BLEND_DODGE,BLEND_HALF};
    blit_lut_fp luts[5]={blit_lut_screen,blit_lut_multiply,blit_lut_overlay,blit_lut_hardlight,blit_lut_dodge};
    long blended_px=(long)NSPRITES*opaque*NFRAMES;

    static unsigned char tblstore[6][5120];
    int m; for(m=0;m<6;m++) build_table(tblstore[bxof[m]], bxof[m]);

    printf("== blend_bench v3 (A9) fb=%dx%d sprite=%dx%d (%d opaque/%d = %.0f%%) nsprites=%d nframes=%d ==\n",
           FBW,FBH,SW,SH,opaque,SW*SH,100.0*opaque/(SW*SH),NSPRITES,NFRAMES);

    /* ---- 1+2: blend modes, arith vs LUT-fp vs LUT-inlined ---- */
    printf("\n[1+2] BLEND modes (ns per opaque px)\n");
    printf("%-10s %10s %10s %12s %10s %10s\n","mode","arith","LUT-fp","LUT-inline","fp->LUT","fp->inline");
    for(m=0;m<8;m++) blendtables[m]=0;
    double arith[6]; for(m=0;m<6;m++) TIME(blit_blend_fp(fb,FBW,FBH,spr,SW,SH,pal,modes[m],NSPRITES), arith[m]);
    for(m=0;m<6;m++) blendtables[bxof[m]]=tblstore[bxof[m]];
    double lutfp[6]; for(m=0;m<6;m++) TIME(blit_blend_fp(fb,FBW,FBH,spr,SW,SH,pal,modes[m],NSPRITES), lutfp[m]);
    double lutin[5]; for(m=0;m<5;m++) TIME(luts[m](fb,FBW,FBH,spr,SW,SH,pal,NSPRITES), lutin[m]);
    for(m=0;m<5;m++) printf("%-10s %10.2f %10.2f %12.2f %9.2fx %9.2fx\n",
        names[m],arith[m],lutfp[m],lutin[m],arith[m]/lutfp[m],lutfp[m]/lutin[m]);
    printf("%-10s %10.2f %10s %12s\n","half",arith[5],"(kept arith)","-");

    /* ---- 3: copy path (the frame majority) ---- */
    printf("\n[3] COPY path -- opaque sprite blit, no blend (ns per opaque px)\n");
    double c_idx,c16s,c16n;
    TIME(blit_copy_idx(fb,FBW,FBH,spr,SW,SH,pal,NSPRITES), c_idx);
    TIME(blit_copy16_scalar(fb,FBW,FBH,spr16,SW,SH,NSPRITES), c16s);
    TIME(blit_copy16_neon(fb,FBW,FBH,spr16,SW,SH,NSPRITES), c16n);
    printf("  indexed-scalar (today) : %7.2f\n", c_idx);
    printf("  prebaked16-scalar      : %7.2f   (%.2fx vs today -- pal[] gather removed)\n", c16s, c_idx/c16s);
    printf("  prebaked16-NEON 8-wide : %7.2f   (%.2fx vs today)\n", c16n, c_idx/c16n);

    /* ---- 4: cache pressure (single table vs all 5 interleaved) ---- */
    printf("\n[4] CACHE -- LUT working set (ns per opaque px)\n");
    double single_avg=(lutfp[0]+lutfp[1]+lutfp[2]+lutfp[3]+lutfp[4])/5.0, mixed;
    blend16fp modes5[5]={blend_screen16,blend_multiply16,blend_overlay16,blend_hardlight16,blend_dodge16};
    TIME(blit_mixed5(fb,FBW,FBH,spr,SW,SH,pal,modes5,NSPRITES), mixed);
    printf("  single-table avg (5 KB): %7.2f\n", single_avg);
    printf("  mixed 5-table (25 KB)  : %7.2f   (%.2fx -- >1 means L1 pressure)\n", mixed, mixed/single_avg);

    free(fb); free(spr); free(spr16); return 0;
}
