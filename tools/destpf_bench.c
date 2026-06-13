/*
 * destpf_bench.c -- does a dest-write PLD prefetch help the scattered framebuffer
 * access (the ~94ns/px memory floor)? Tests both He-Man-relevant paths on a real
 * 960x480 DDR3-spilling framebuffer with scattered sprite placement:
 *   - OPAQUE copy  (d[x]=pal[idx])           -- the dominant alpha=-1 path
 *   - BLEND  (LUT)  (d[x]=blend(pal[idx],d))  -- the screen/multiply minority
 * each WITHOUT vs WITH __builtin_prefetch(&d[x+16], 1, 0).
 *
 * If the A9 HW prefetcher already covers the (sequential-within-row) writes,
 * this shows ~no change -> skip it. If it helps the strided/scattered access,
 * it's a free win to fold into the ship build.
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/destpf_bench.c -o destpf_bench -lrt
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

static void build_mul(unsigned char*t){ int c1,c2,v;
    for(c1=0;c1<32;c1++)for(c2=0;c2<32;c2++){v=_multiply16(c1,c2,0x1F);if(v>0x1F)v=0x1F;t[(c1<<5)|c2]=v;}
    for(c1=0;c1<64;c1++)for(c2=0;c2<64;c2++){v=_multiply16(c1,c2,0x3F);if(v>0x3F)v=0x3F;t[1024+((c1<<6)|c2)]=v;} }

#define SPR_X(n,fbw,sw) (((n)*97)%((fbw)-(sw)))
#define SPR_Y(n,fbh,sh) (((n)*61)%((fbh)-(sh)))

/* OPAQUE copy, no prefetch */
static void copy_np(uint16_t*fb,int fbw,int fbh,const uint8_t*spr,int sw,int sh,const uint16_t*pal,int n){
    int k,row,col; for(k=0;k<n;k++){int x=SPR_X(k,fbw,sw),y=SPR_Y(k,fbh,sh);
        for(row=0;row<sh;row++){uint16_t*d=fb+(long)(y+row)*fbw+x;const uint8_t*s=spr+(long)row*sw;
            for(col=0;col<sw;col++){uint8_t idx=s[col];if(idx)d[col]=pal[idx];}}} }
/* OPAQUE copy, dest prefetch */
static void copy_pf(uint16_t*fb,int fbw,int fbh,const uint8_t*spr,int sw,int sh,const uint16_t*pal,int n){
    int k,row,col; for(k=0;k<n;k++){int x=SPR_X(k,fbw,sw),y=SPR_Y(k,fbh,sh);
        for(row=0;row<sh;row++){uint16_t*d=fb+(long)(y+row)*fbw+x;const uint8_t*s=spr+(long)row*sw;
            for(col=0;col<sw;col++){__builtin_prefetch(&d[col+16],1,0);uint8_t idx=s[col];if(idx)d[col]=pal[idx];}}} }
/* BLEND (LUT), no prefetch */
static void blend_np(uint16_t*fb,int fbw,int fbh,const uint8_t*spr,int sw,int sh,const uint16_t*pal,const unsigned char*tbl,int n){
    int k,row,col; for(k=0;k<n;k++){int x=SPR_X(k,fbw,sw),y=SPR_Y(k,fbh,sh);
        for(row=0;row<sh;row++){uint16_t*d=fb+(long)(y+row)*fbw+x;const uint8_t*s=spr+(long)row*sw;
            for(col=0;col<sw;col++){uint8_t idx=s[col];if(idx){uint16_t color1=pal[idx],color2=d[col];d[col]=(uint16_t)_color16(tbl[_ri],tbl[_gi],tbl[_bi]);}}}} }
/* BLEND (LUT), dest prefetch */
static void blend_pf(uint16_t*fb,int fbw,int fbh,const uint8_t*spr,int sw,int sh,const uint16_t*pal,const unsigned char*tbl,int n){
    int k,row,col; for(k=0;k<n;k++){int x=SPR_X(k,fbw,sw),y=SPR_Y(k,fbh,sh);
        for(row=0;row<sh;row++){uint16_t*d=fb+(long)(y+row)*fbw+x;const uint8_t*s=spr+(long)row*sw;
            for(col=0;col<sw;col++){__builtin_prefetch(&d[col+16],1,0);uint8_t idx=s[col];if(idx){uint16_t color1=pal[idx],color2=d[col];d[col]=(uint16_t)_color16(tbl[_ri],tbl[_gi],tbl[_bi]);}}}} }

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

int main(int argc,char**argv){
    int FBW=960,FBH=480,SW=64,SH=96, N=(argc>1)?atoi(argv[1]):150, F=(argc>2)?atoi(argv[2]):3, r,f,i,op=0;
    uint16_t*fb=malloc((long)FBW*FBH*2); uint8_t*spr=malloc(SW*SH); uint16_t pal[256];
    for(i=0;i<256;i++)pal[i]=(uint16_t)(i*0x0123+7);
    for(i=0;i<SW*SH;i++){uint8_t v=(uint8_t)((i*131+7)%255);if((i%5)==0)v=0;spr[i]=v;if(v)op++;}
    for(i=0;i<FBW*FBH;i++)fb[i]=(uint16_t)(i*0x1234);
    static unsigned char tbl[5120]; build_mul(tbl);
    long px=(long)N*op*F;
    printf("== destpf_bench (A9) fb=%dx%d scattered, %ld opaque-px/measure ==\n",FBW,FBH,px);
    #define M(CALL,OUT) do{CALL;double _mb=1e30;for(r=0;r<3;r++){double t0=now_ns();for(f=0;f<F;f++)CALL;double dt=now_ns()-t0;if(dt<_mb)_mb=dt;}OUT=_mb/px;}while(0)
    double a,b;
    M(copy_np(fb,FBW,FBH,spr,SW,SH,pal,N),a); M(copy_pf(fb,FBW,FBH,spr,SW,SH,pal,N),b);
    printf("OPAQUE copy : no-pf %6.2f  pf %6.2f  ns/px  (%.2fx)\n",a,b,a/b);
    M(blend_np(fb,FBW,FBH,spr,SW,SH,pal,tbl,N),a); M(blend_pf(fb,FBW,FBH,spr,SW,SH,pal,tbl,N),b);
    printf("BLEND (LUT) : no-pf %6.2f  pf %6.2f  ns/px  (%.2fx)\n",a,b,a/b);
    printf("(>1.05x = prefetch helps -> fold into ship; ~1.00x = HW prefetcher already covers it -> skip.)\n");
    free(fb);free(spr); return 0;
}
