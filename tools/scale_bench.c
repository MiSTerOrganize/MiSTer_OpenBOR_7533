/*
 * scale_bench.c -- OpenBOR_7533 PAK-squish scaling-method micro-benchmark (A9).
 *
 * Measures the per-frame COST of each downscale method at the 3 real PAK->
 * framebuffer ratios, WITHOUT playing. Answers "can we afford a better scaler?"
 * The QUALITY axis (which looks best) still needs PC-reference eyeballs -- this
 * is purely the cost side of the tradeoff.
 *
 * Dest is the fixed 320x224 framebuffer. Sources are the 3 native PAK sizes.
 * 16-bit RGB565 throughout (matches the PIXEL_16 vscreen). All scalers scalar +
 * anisotropic; the shipped NN is NEON-optimised so its ABSOLUTE cost is lower,
 * but the bilinear/box DELTAS show the method overhead (all 3 are NEON-able).
 *
 *   NN       : 1 tap  (current 16bpp WriteFrame path -- src_x_table)
 *   bilinear : 4 taps (constant, regardless of ratio)
 *   box-avg  : ratio_x*ratio_y taps (scales with downscale -- the area filter)
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/scale_bench.c -o scale_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>

#define DW 320
#define DH 224

static inline void unpack(uint16_t c, int*r, int*g, int*b){ *r=c&0x1F; *g=(c>>5)&0x3F; *b=(c>>11)&0x1F; }
static inline uint16_t pack(int r, int g, int b){ return (uint16_t)((b<<11)|(g<<5)|r); }

/* --- nearest-neighbour, anisotropic (matches the 16bpp WriteFrame src_x_table) --- */
static void scale_nn(const uint16_t*src, int sw, int sh, uint16_t*dst){
    uint16_t xtab[DW]; int x,y;
    for(x=0;x<DW;x++){ int sx=(x*sw)/DW; if(sx>=sw)sx=sw-1; xtab[x]=(uint16_t)sx; }
    for(y=0;y<DH;y++){
        int sy=(y*sh)/DH; if(sy>=sh)sy=sh-1;
        const uint16_t*srow=src+(long)sy*sw; uint16_t*drow=dst+(long)y*DW;
        for(x=0;x<DW;x++) drow[x]=srow[xtab[x]];
    }
}

/* --- bilinear, anisotropic (4-tap, 8-bit fractional weights) --- */
static void scale_bilinear(const uint16_t*src, int sw, int sh, uint16_t*dst){
    uint32_t fx=((uint32_t)(sw-1)<<16)/(DW>1?DW-1:1);
    uint32_t fy=((uint32_t)(sh-1)<<16)/(DH>1?DH-1:1);
    int x,y;
    for(y=0;y<DH;y++){
        uint32_t syf=(uint32_t)y*fy; int sy=syf>>16; int wy=(syf>>8)&0xFF;
        if(sy>=sh-1){ sy=sh-1; wy=0; }
        const uint16_t*r0=src+(long)sy*sw; const uint16_t*r1=r0+(wy?sw:0);
        uint16_t*drow=dst+(long)y*DW;
        for(x=0;x<DW;x++){
            uint32_t sxf=(uint32_t)x*fx; int sx=sxf>>16; int wx=(sxf>>8)&0xFF;
            if(sx>=sw-1){ sx=sw-1; wx=0; }
            int x1=sx+(wx?1:0);
            int r00,g00,b00,r01,g01,b01,r10,g10,b10,r11,g11,b11;
            unpack(r0[sx],&r00,&g00,&b00); unpack(r0[x1],&r01,&g01,&b01);
            unpack(r1[sx],&r10,&g10,&b10); unpack(r1[x1],&r11,&g11,&b11);
            int rt=(r00*(256-wx)+r01*wx)>>8, gt=(g00*(256-wx)+g01*wx)>>8, bt=(b00*(256-wx)+b01*wx)>>8;
            int rb=(r10*(256-wx)+r11*wx)>>8, gb=(g10*(256-wx)+g11*wx)>>8, bb=(b10*(256-wx)+b11*wx)>>8;
            int r=(rt*(256-wy)+rb*wy)>>8, g=(gt*(256-wy)+gb*wy)>>8, b=(bt*(256-wy)+bb*wy)>>8;
            drow[x]=pack(r,g,b);
        }
    }
}

/* --- box / area average, anisotropic (averages the full src block per dst px) --- */
static void scale_box(const uint16_t*src, int sw, int sh, uint16_t*dst){
    int x,y;
    for(y=0;y<DH;y++){
        int sy0=(y*sh)/DH, sy1=((y+1)*sh)/DH; if(sy1<=sy0)sy1=sy0+1; if(sy1>sh)sy1=sh;
        uint16_t*drow=dst+(long)y*DW;
        for(x=0;x<DW;x++){
            int sx0=(x*sw)/DW, sx1=((x+1)*sw)/DW; if(sx1<=sx0)sx1=sx0+1; if(sx1>sw)sx1=sw;
            int rs=0,gs=0,bs=0,n=0,yy,xx;
            for(yy=sy0;yy<sy1;yy++){ const uint16_t*srow=src+(long)yy*sw;
                for(xx=sx0;xx<sx1;xx++){ int r,g,b; unpack(srow[xx],&r,&g,&b); rs+=r; gs+=g; bs+=b; n++; } }
            drow[x]=pack(rs/n,gs/n,bs/n);
        }
    }
}

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

int main(int argc,char**argv){
    int NF=(argc>1)?atoi(argv[1]):20;
    struct { int w,h; const char*name; } res[3]={
        {320,240,"320x240 (ATOV/modern)"},
        {480,272,"480x272 (PDC2/Avengers)"},
        {960,480,"960x480 (He-Man)"} };
    uint16_t*dst=(uint16_t*)malloc((long)DW*DH*2);
    double opx=(double)DW*DH;
    printf("== scale_bench (A9) dest=%dx%d nframes=%d -- ns per OUTPUT px (ms/frame in parens) ==\n",DW,DH,NF);
    printf("(frame budget @ 15 fps = 66.7 ms; @ 30 fps = 33.3 ms -- scaling should be a sliver)\n");
    printf("%-24s %16s %16s %16s\n","source res","NN","bilinear","box-avg");
    int i,r,f;
    for(i=0;i<3;i++){
        int sw=res[i].w, sh=res[i].h; uint16_t*src=(uint16_t*)malloc((long)sw*sh*2);
        int k; for(k=0;k<sw*sh;k++) src[k]=(uint16_t)(k*0x1234+k/7);
        double bn=1e30,bb=1e30,bx=1e30,t0,dt;
        scale_nn(src,sw,sh,dst); scale_bilinear(src,sw,sh,dst); scale_box(src,sw,sh,dst); /* warm */
        for(r=0;r<3;r++){ t0=now_ns(); for(f=0;f<NF;f++) scale_nn(src,sw,sh,dst);       dt=now_ns()-t0; if(dt<bn)bn=dt; }
        for(r=0;r<3;r++){ t0=now_ns(); for(f=0;f<NF;f++) scale_bilinear(src,sw,sh,dst); dt=now_ns()-t0; if(dt<bb)bb=dt; }
        for(r=0;r<3;r++){ t0=now_ns(); for(f=0;f<NF;f++) scale_box(src,sw,sh,dst);      dt=now_ns()-t0; if(dt<bx)bx=dt; }
        double tn=bn/(NF*opx), tb=bb/(NF*opx), tx=bx/(NF*opx);
        printf("%-24s %6.2f (%5.3fms) %6.2f (%5.3fms) %6.2f (%5.3fms)\n",
            res[i].name, tn, tn*opx/1e6, tb, tb*opx/1e6, tx, tx*opx/1e6);
        free(src);
    }
    free(dst); return 0;
}
