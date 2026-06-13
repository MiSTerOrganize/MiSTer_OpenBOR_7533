/*
 * verify_bench.c -- OpenBOR_7533 kernel CORRECTNESS + REGRESSION harness (A9).
 *
 * Proves the optimized 16-bit kernels are BIT-IDENTICAL to their references --
 * the claims made for B3 / #1 / #2 -- without playing a PAK. Doubles as the
 * regression gate: it (a) cross-checks optimized vs reference IN-BINARY (catches
 * a correctness regression in an optimization), and (b) prints golden FNV hashes
 * of reference outputs (catches a reference drift across builds). Nonzero exit
 * on ANY mismatch, so CI / a script can gate on it.
 *
 *   VERIFY 1 : LUT blend == arithmetic blend -- EXHAUSTIVE per channel pair,
 *              all 6 modes (r/b: 32x32, g: 64x64) + random full-colour fuzz.
 *   VERIFY 2 : #1 NEON 8x copy == scalar copy (all 256 indices, 8x + remainder).
 *   VERIFY 3 : #2 inlined-LUT blit == fp-dispatch blit (per-pixel blend).
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/verify_bench.c -o verify_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <arm_neon.h>

/* ---- blend macros, verbatim from pixelformat.c v7533 ---- */
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
static unsigned char *blendtables[8] = {0};

uint16_t blend_multiply16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_MULTIPLY])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_multiply16(_r1,_r2,0x1F), _multiply16(_g1,_g2,0x3F), _multiply16(_b1,_b2,0x1F)); }
uint16_t blend_screen16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_SCREEN])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_screen16(_r1,_r2,0x1F), _screen16(_g1,_g2,0x3F), _screen16(_b1,_b2,0x1F)); }
uint16_t blend_overlay16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_OVERLAY])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_overlay16(_r1,_r2,0x0F,0x1F), _overlay16(_g1,_g2,0x1F,0x3F), _overlay16(_b1,_b2,0x0F,0x1F)); }
uint16_t blend_hardlight16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_HARDLIGHT])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_hardlight16(_r1,_r2,0x0F,0x1F), _hardlight16(_g1,_g2,0x1F,0x3F), _hardlight16(_b1,_b2,0x0F,0x1F)); }
uint16_t blend_dodge16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_DODGE])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16(_dodge16(_r1,_r2,0x1F), _dodge16(_g1,_g2,0x3F), _dodge16(_b1,_b2,0x1F)); }
uint16_t blend_half16(uint16_t color1, uint16_t color2){
    unsigned char *tbl; if((tbl=blendtables[BLEND_HALF])) return _color16(tbl[_ri],tbl[_gi],tbl[_bi]);
    return _color16((_r1+_r2)>>1, (_g1+_g2)>>1, (_b1+_b2)>>1); }

/* table builder (matches engine create_*16_tbl layout) */
static void build_table(unsigned char *tbl, int bx){
    int c1,c2,v;
    for(c1=0;c1<32;c1++) for(c2=0;c2<32;c2++){
        switch(bx){
            case BLEND_SCREEN: v=_screen16(c1,c2,0x1F); break; case BLEND_MULTIPLY: v=_multiply16(c1,c2,0x1F); break;
            case BLEND_OVERLAY: v=_overlay16(c1,c2,0x0F,0x1F); break; case BLEND_HARDLIGHT: v=_hardlight16(c1,c2,0x0F,0x1F); break;
            case BLEND_DODGE: v=(c1>=0x1F)?0x1F:_dodge16(c1,c2,0x1F); break; case BLEND_HALF: v=(c1+c2)>>1; break; default: v=0; }
        if(v>0x1F)v=0x1F; if(v<0)v=0; tbl[(c1<<5)|c2]=(unsigned char)v; }
    for(c1=0;c1<64;c1++) for(c2=0;c2<64;c2++){
        switch(bx){
            case BLEND_SCREEN: v=_screen16(c1,c2,0x3F); break; case BLEND_MULTIPLY: v=_multiply16(c1,c2,0x3F); break;
            case BLEND_OVERLAY: v=_overlay16(c1,c2,0x1F,0x3F); break; case BLEND_HARDLIGHT: v=_hardlight16(c1,c2,0x1F,0x3F); break;
            case BLEND_DODGE: v=(c1>=0x3F)?0x3F:_dodge16(c1,c2,0x3F); break; case BLEND_HALF: v=(c1+c2)>>1; break; default: v=0; }
        if(v>0x3F)v=0x3F; if(v<0)v=0; tbl[1024+((c1<<6)|c2)]=(unsigned char)v; }
}

/* #1: scalar copy (engine stock) vs NEON 8x copy (vst1q_u16) */
static void copy_scalar(const uint8_t*idx,int n,const uint16_t*pal,uint16_t*dst){ int i; for(i=0;i<n;i++) dst[i]=pal[idx[i]]; }
static void copy_neon(const uint8_t*idx,int n,const uint16_t*pal,uint16_t*dst){
    const uint8_t*d=idx; uint16_t*o=dst; int count=n;
    while(count>=8){ uint16_t p0=pal[d[0]],p1=pal[d[1]],p2=pal[d[2]],p3=pal[d[3]],p4=pal[d[4]],p5=pal[d[5]],p6=pal[d[6]],p7=pal[d[7]];
#ifdef __ARM_NEON
        vst1q_u16((uint16_t*)o, (uint16x8_t){p0,p1,p2,p3,p4,p5,p6,p7});
#else
        o[0]=p0;o[1]=p1;o[2]=p2;o[3]=p3;o[4]=p4;o[5]=p5;o[6]=p6;o[7]=p7;
#endif
        o+=8; d+=8; count-=8; }
    while(count>0){ *o++=pal[*d++]; count--; }
}

/* #2: fp-dispatch blit vs inlined-LUT blit (per-pixel) */
static void blit_fp(const uint8_t*idx,int n,const uint16_t*pal,uint16_t*dst,blend16fp fp){ int i; for(i=0;i<n;i++) dst[i]=fp(pal[idx[i]],dst[i]); }
static void blit_inlined(const uint8_t*idx,int n,const uint16_t*pal,uint16_t*dst,const unsigned char*tbl){
    int i; for(i=0;i<n;i++){ uint16_t color1=pal[idx[i]],color2=dst[i]; dst[i]=(uint16_t)_color16(tbl[_ri],tbl[_gi],tbl[_bi]); } }

static uint32_t fnv1a(const void*p,size_t n){ const uint8_t*b=(const uint8_t*)p; uint32_t h=2166136261u; size_t i; for(i=0;i<n;i++){h^=b[i];h*=16777619u;} return h; }

/* exhaustive per-channel + fuzz: LUT == arith for one mode */
static int verify_blend_mode(const char*name,blend16fp fp,int bx,unsigned char*tbl){
    int mism=0,c1,c2; uint32_t seed=0xC0FFEE ^ (uint32_t)bx;
    for(c1=0;c1<32;c1++)for(c2=0;c2<32;c2++){ uint16_t a=(uint16_t)c1,b=(uint16_t)c2;       /* R */
        blendtables[bx]=NULL; uint16_t x=fp(a,b); blendtables[bx]=tbl; uint16_t y=fp(a,b);
        if(x!=y){ if(mism<2)printf("    [%s] R c1=%d c2=%d arith=%04x lut=%04x\n",name,c1,c2,x,y); mism++; } }
    for(c1=0;c1<64;c1++)for(c2=0;c2<64;c2++){ uint16_t a=(uint16_t)(c1<<5),b=(uint16_t)(c2<<5); /* G */
        blendtables[bx]=NULL; uint16_t x=fp(a,b); blendtables[bx]=tbl; uint16_t y=fp(a,b);
        if(x!=y){ if(mism<2)printf("    [%s] G c1=%d c2=%d arith=%04x lut=%04x\n",name,c1,c2,x,y); mism++; } }
    for(c1=0;c1<32;c1++)for(c2=0;c2<32;c2++){ uint16_t a=(uint16_t)(c1<<11),b=(uint16_t)(c2<<11); /* B */
        blendtables[bx]=NULL; uint16_t x=fp(a,b); blendtables[bx]=tbl; uint16_t y=fp(a,b);
        if(x!=y){ if(mism<2)printf("    [%s] B c1=%d c2=%d arith=%04x lut=%04x\n",name,c1,c2,x,y); mism++; } }
    int i; for(i=0;i<200000;i++){ seed=seed*1103515245u+12345u; uint16_t a=(uint16_t)(seed>>8);
        seed=seed*1103515245u+12345u; uint16_t b=(uint16_t)(seed>>8);                             /* fuzz */
        blendtables[bx]=NULL; uint16_t x=fp(a,b); blendtables[bx]=tbl; uint16_t y=fp(a,b);
        if(x!=y){ if(mism<2)printf("    [%s] fuzz a=%04x b=%04x arith=%04x lut=%04x\n",name,a,b,x,y); mism++; } }
    blendtables[bx]=NULL;
    printf("  %-10s %s (5120 channel pairs + 200k fuzz)\n",name, mism?"FAIL":"PASS");
    return mism;
}

int main(void){
    int total=0; int m;
    static unsigned char tbl[6][5120];
    blend16fp modes[6]={blend_screen16,blend_multiply16,blend_overlay16,blend_hardlight16,blend_dodge16,blend_half16};
    const char*names[6]={"screen","multiply","overlay","hardlight","dodge","half"};
    int bxof[6]={BLEND_SCREEN,BLEND_MULTIPLY,BLEND_OVERLAY,BLEND_HARDLIGHT,BLEND_DODGE,BLEND_HALF};
    for(m=0;m<6;m++) build_table(tbl[m],bxof[m]);

    printf("== verify_bench (A9) -- kernel correctness + regression ==\n\n");
    printf("[VERIFY 1] LUT blend == arithmetic (exhaustive per-channel + fuzz)\n");
    for(m=0;m<6;m++) total += verify_blend_mode(names[m],modes[m],bxof[m],tbl[m]);

    /* VERIFY 2: NEON copy == scalar copy */
    printf("\n[VERIFY 2] #1 NEON 8x copy == scalar copy\n");
    int N=2003, i; uint8_t*idx=(uint8_t*)malloc(N); uint16_t pal[256], *a=(uint16_t*)malloc(N*2), *b=(uint16_t*)malloc(N*2);
    for(i=0;i<256;i++) pal[i]=(uint16_t)(i*0x0123+7);
    for(i=0;i<N;i++) idx[i]=(uint8_t)((i*131+i/7)&0xFF);   /* exercises all 256 indices + 8x/remainder */
    copy_scalar(idx,N,pal,a); copy_neon(idx,N,pal,b);
    int c2mism = memcmp(a,b,N*2)?1:0;
    printf("  copy      %s  (n=%d, golden=%08x)\n", c2mism?"FAIL":"PASS", N, fnv1a(a,N*2)); total+=c2mism;

    /* VERIFY 3: inlined-LUT blit == fp-dispatch blit, per mode */
    printf("\n[VERIFY 3] #2 inlined-LUT blit == fp-dispatch blit\n");
    uint16_t*da=(uint16_t*)malloc(N*2), *db=(uint16_t*)malloc(N*2);
    for(m=0;m<6;m++){
        if(m==BLEND_HALF){ printf("  %-10s n/a (half keeps arith path -- not LUT-dispatched)\n",names[m]); continue; }
        for(i=0;i<N;i++){ da[i]=db[i]=(uint16_t)(i*0x2531+3); }
        blendtables[bxof[m]]=tbl[m];
        blit_fp(idx,N,pal,da,modes[m]);          /* fp path (LUT active) */
        blit_inlined(idx,N,pal,db,tbl[m]);        /* inlined path */
        blendtables[bxof[m]]=NULL;
        int mm=memcmp(da,db,N*2)?1:0; total+=mm;
        printf("  %-10s %s  (golden=%08x)\n",names[m], mm?"FAIL":"PASS", fnv1a(da,N*2));
    }

    printf("\n================  %s  (%d mismatch%s)  ================\n",
        total?"VERIFY FAIL":"VERIFY PASS -- B3 + #1 + #2 are BYTE-EXACT", total, total==1?"":"es");
    free(idx);free(a);free(b);free(da);free(db);
    return total?1:0;
}
