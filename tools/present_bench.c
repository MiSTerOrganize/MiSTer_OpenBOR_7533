/*
 * present_bench.c -- frame-present cost (WriteFrame's final stage), the one
 * per-frame stage never isolated. Squish native (960x480, He-Man) -> 320x224
 * with the BGR565->RGB565 channel swap, exactly as native_video_writer does.
 *
 * Mode 1 (default, safe any state): squish+swap to a cached dest. Measures the
 *   PRESENT COMPUTE + the source-read traffic (960x480x2=900KB spills L2 -> DDR3).
 * Mode 2 (ddr3 <hexaddr>): mmap /dev/mem and time the uncached write of one
 *   320x224 frame to <hexaddr>. *** MENU ONLY *** -- writing the live FPGA ring
 *   corrupts the display. Provide a scratch DDR3 phys address you trust.
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/present_bench.c -o present_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>

#define DW 320
#define DH 224

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

/* anisotropic NN squish + BGR565->RGB565 swap (matches native_video_writer 16bpp) */
static void present_squish(const uint16_t *src, int sw, int sh, uint16_t *dst){
    uint16_t xtab[DW]; int x,y;
    for(x=0;x<DW;x++){ int sx=(x*sw)/DW; if(sx>=sw)sx=sw-1; xtab[x]=(uint16_t)sx; }
    for(y=0;y<DH;y++){
        int sy=(y*sh)/DH; if(sy>=sh)sy=sh-1;
        const uint16_t *srow = src + (long)sy*sw; uint16_t *drow = dst + (long)y*DW;
        for(x=0;x<DW;x++){
            uint16_t c = srow[xtab[x]];
            /* BGR565 -> RGB565: swap 5-bit R/B, keep 6-bit G */
            drow[x] = (uint16_t)(((c & 0x1F) << 11) | (c & 0x07E0) | ((c >> 11) & 0x1F));
        }
    }
}

int main(int argc,char**argv){
    int SW=960, SH=480, NF=300, i,r;
    /* Mode 2: ddr3 <hexaddr> */
    if(argc>1 && strcmp(argv[1],"ddr3")==0){
        if(argc<3){ printf("usage: present_bench ddr3 <hexaddr>\n"); return 2; }
        unsigned long pa = strtoul(argv[2],0,16);
        size_t bytes = (size_t)DW*DH*2;
        int fd = open("/dev/mem", O_RDWR|O_SYNC);
        if(fd<0){ perror("open /dev/mem"); return 1; }
        size_t pg = sysconf(_SC_PAGESIZE), off = pa & (pg-1);
        void *m = mmap(0, bytes+off, PROT_READ|PROT_WRITE, MAP_SHARED, fd, pa-off);
        if(m==MAP_FAILED){ perror("mmap"); close(fd); return 1; }
        volatile uint16_t *ring = (uint16_t*)((char*)m+off);
        uint16_t *fr = malloc(bytes); for(i=0;i<DW*DH;i++) fr[i]=(uint16_t)(i*0x1234);
        printf("== present_bench DDR3-write (MENU ONLY) @0x%lx, %d frames of %dx%d ==\n", pa, NF, DW, DH);
        double best=1e30; for(r=0;r<3;r++){ double t0=now_ns(); int f; for(f=0;f<NF;f++) memcpy((void*)ring, fr, bytes); double dt=now_ns()-t0; if(dt<best)best=dt; }
        double perfr = best/NF;
        printf("uncached DDR3 write: %.3f ms/frame (%.2f GB/s)\n", perfr/1e6, (double)bytes/perfr);
        munmap(m,bytes+off); close(fd); free(fr); return 0;
    }
    /* Mode 1: cached squish+swap compute */
    uint16_t *src=malloc((long)SW*SH*2), *dst=malloc((long)DW*DH*2);
    for(i=0;i<SW*SH;i++) src[i]=(uint16_t)(i*0x1234+i/7);
    double opx=(double)DW*DH;
    printf("== present_bench (A9) squish %dx%d -> %dx%d + BGR565->RGB565 swap ==\n", SW,SH,DW,DH);
    present_squish(src,SW,SH,dst);
    double best=1e30; for(r=0;r<3;r++){ double t0=now_ns(); int f; for(f=0;f<NF;f++) present_squish(src,SW,SH,dst); double dt=now_ns()-t0; if(dt<best)best=dt; }
    double perfr=best/NF;
    printf("present compute: %.2f ns/out-px  =  %.3f ms/frame\n", perfr/opx, perfr/1e6);
    printf("(source read = %dx%dx2 = %ldKB/frame spills L2 -> DDR3; cf mem_bench DDR3 read.)\n", SW,SH,(long)SW*SH*2/1024);
    printf("(real present also writes %ldKB to the uncached DDR3 ring -- run 'present_bench ddr3 <addr>' from MENU.)\n",(long)DW*DH*2/1024);
    free(src); free(dst); return 0;
}
