/*
 * scale_bench.c v2 -- OpenBOR_7533 PAK-squish scaling-method micro-benchmark (A9).
 *
 * Measures per-frame COST of each downscale method at the 3 real PAK->framebuffer
 * ratios, WITHOUT playing. v2 adds the OPTIMIZED variants so we see the real
 * affordable cost (not the naive upper bound):
 *   - bilinear-opt : RGB565 mask-lerp (split 0xF81F red+blue / 0x07E0 green,
 *                    packed 2-half arithmetic; no per-channel unpack/divide).
 *   - box-opt      : packed-accumulate (sum the two 565 halves in 32-bit, one
 *                    divide per half) instead of per-tap unpack.
 * Both are still scalar (no NEON) -- a fair method-vs-method comparison; all
 * would gain a similar NEON multiplier on top.
 *
 * Dest = fixed 320x224. 16-bit RGB565 throughout (matches the PIXEL_16 vscreen).
 * Denominator = output pixels (320x224). QUALITY (which looks best) still needs
 * PC-reference eyeballs; this is the cost side only.
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

/* 565 mask-lerp: w is a 5-bit weight (0..32). One mul per half, no unpack. */
static inline uint16_t lerp565(uint16_t a, uint16_t b, uint32_t w){
    uint32_t iw=32-w;
    uint32_t arb=a&0xF81F, ag=a&0x07E0, brb=b&0xF81F, bg=b&0x07E0;
    uint32_t rb=((arb*iw + brb*w) >> 5) & 0xF81F;
    uint32_t g =((ag*iw  + bg*w ) >> 5) & 0x07E0;
    return (uint16_t)(rb | g);
}

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

/* --- bilinear NAIVE: per-channel unpack + lerp + repack (upper-bound cost) --- */
static void scale_bilinear_naive(const uint16_t*src, int sw, int sh, uint16_t*dst){
    uint32_t fx=((uint32_t)(sw-1)<<16)/(DW>1?DW-1:1), fy=((uint32_t)(sh-1)<<16)/(DH>1?DH-1:1);
    int x,y;
    for(y=0;y<DH;y++){
        uint32_t syf=(uint32_t)y*fy; int sy=syf>>16; int wy=(syf>>8)&0xFF; if(sy>=sh-1){sy=sh-1;wy=0;}
        const uint16_t*r0=src+(long)sy*sw, *r1=r0+(wy?sw:0); uint16_t*drow=dst+(long)y*DW;
        for(x=0;x<DW;x++){
            uint32_t sxf=(uint32_t)x*fx; int sx=sxf>>16; int wx=(sxf>>8)&0xFF; if(sx>=sw-1){sx=sw-1;wx=0;}
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

/* --- bilinear OPT: RGB565 mask-lerp (3 lerp565 per output px) --- */
static void scale_bilinear_opt(const uint16_t*src, int sw, int sh, uint16_t*dst){
    uint32_t fx=((uint32_t)(sw-1)<<16)/(DW>1?DW-1:1), fy=((uint32_t)(sh-1)<<16)/(DH>1?DH-1:1);
    int x,y;
    for(y=0;y<DH;y++){
        uint32_t syf=(uint32_t)y*fy; int sy=syf>>16; uint32_t wy=(syf>>11)&0x1F; if(sy>=sh-1){sy=sh-1;wy=0;}
        const uint16_t*r0=src+(long)sy*sw, *r1=r0+(wy?sw:0); uint16_t*drow=dst+(long)y*DW;
        for(x=0;x<DW;x++){
            uint32_t sxf=(uint32_t)x*fx; int sx=sxf>>16; uint32_t wx=(sxf>>11)&0x1F; if(sx>=sw-1){sx=sw-1;wx=0;}
            int x1=sx+(wx?1:0);
            uint16_t top=lerp565(r0[sx],r0[x1],wx), bot=lerp565(r1[sx],r1[x1],wx);
            drow[x]=lerp565(top,bot,wy);
        }
    }
}

/* --- box NAIVE: per-tap unpack + per-channel divide (upper-bound cost) --- */
static void scale_box_naive(const uint16_t*src, int sw, int sh, uint16_t*dst){
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

/* --- box OPT: packed-accumulate the two 565 halves, one divide each --- */
static void scale_box_opt(const uint16_t*src, int sw, int sh, uint16_t*dst){
    int x,y;
    for(y=0;y<DH;y++){
        int sy0=(y*sh)/DH, sy1=((y+1)*sh)/DH; if(sy1<=sy0)sy1=sy0+1; if(sy1>sh)sy1=sh;
        uint16_t*drow=dst+(long)y*DW;
        for(x=0;x<DW;x++){
            int sx0=(x*sw)/DW, sx1=((x+1)*sw)/DW; if(sx1<=sx0)sx1=sx0+1; if(sx1>sw)sx1=sw;
            uint32_t rb=0,g=0; int n=0,yy,xx;
            for(yy=sy0;yy<sy1;yy++){ const uint16_t*srow=src+(long)yy*sw;
                for(xx=sx0;xx<sx1;xx++){ uint16_t c=srow[xx]; rb+=c&0xF81F; g+=c&0x07E0; n++; } }
            drow[x]=(uint16_t)((((rb/n)&0xF81F)) | (((g/n)&0x07E0)));
        }
    }
}

typedef void (*scalefn)(const uint16_t*,int,int,uint16_t*);
static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

int main(int argc,char**argv){
    int NF=(argc>1)?atoi(argv[1]):20;
    struct { int w,h; const char*name; } res[3]={
        {320,240,"320x240"}, {480,272,"480x272"}, {960,480,"960x480"} };
    uint16_t*dst=(uint16_t*)malloc((long)DW*DH*2);
    uint16_t*src[3]; int i,k;
    for(i=0;i<3;i++){ src[i]=(uint16_t*)malloc((long)res[i].w*res[i].h*2);
        for(k=0;k<res[i].w*res[i].h;k++) src[i][k]=(uint16_t)(k*0x1234+k/7); }
    double opx=(double)DW*DH;

    scalefn fns[5]={scale_nn,scale_bilinear_naive,scale_bilinear_opt,scale_box_naive,scale_box_opt};
    const char *names[5]={"NN (today)","bilinear-naive","bilinear-OPT","box-naive","box-OPT"};

    printf("== scale_bench v2 (A9) dest=%dx%d nframes=%d -- ns/output-px (ms/frame) ==\n",DW,DH,NF);
    printf("(frame budget @15fps=66.7ms  @30fps=33.3ms; box taps scale with downscale ratio)\n");
    printf("%-16s %16s %16s %16s\n","method","320x240 (1.1x)","480x272 (1.5x)","960x480 (3x He-Man)");
    int m,r,f;
    for(m=0;m<5;m++){
        printf("%-16s",names[m]);
        for(i=0;i<3;i++){
            double best=1e30,t0,dt;
            fns[m](src[i],res[i].w,res[i].h,dst);
            for(r=0;r<3;r++){ t0=now_ns(); for(f=0;f<NF;f++) fns[m](src[i],res[i].w,res[i].h,dst); dt=now_ns()-t0; if(dt<best)best=dt; }
            double nspx=best/(NF*opx);
            printf(" %6.1f (%5.2fms)",nspx,nspx*opx/1e6);
        }
        printf("\n");
    }
    for(i=0;i<3;i++) free(src[i]);
    free(dst); return 0;
}
