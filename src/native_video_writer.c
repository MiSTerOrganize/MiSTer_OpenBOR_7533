//
//  Native Video DDR3 Writer — OpenBOR MiSTer
//
//  Writes 320x224 RGB565 frames to DDR3 at 0x3A000000 for FPGA native
//  video output. Double-buffered with control word handshake.
//
//  DDR3 Memory Map (must match openbor_video_reader.sv):
//    0x3A000000 + 0x000     : Control word (frame_counter[31:2] | active_buf[1:0])
//    0x3A000000 + 0x008     : Joystick P1 (32 bits)
//    0x3A000000 + 0x010     : Cart control (file_size from FPGA)
//    0x3A000000 + 0x018     : Joystick P2 (32 bits)
//    0x3A000000 + 0x020     : Joystick P3 (32 bits)
//    0x3A000000 + 0x028     : Joystick P4 (32 bits)
//    0x3A000000 + 0x040     : Buffer 0 (320*224*2 = 143,360 bytes)
//    0x3A000000 + 0x40040   : Buffer 1 (153,600 bytes)
//    0x3A000000 + 0x80000   : Cart data (PAK file from OSD)
//
//  Copyright (C) 2026 MiSTer Organize — GPL-3.0
//

#ifndef _GNU_SOURCE
#define _GNU_SOURCE   /* 2026-06-07: sched_setaffinity / cpu_set_t for render-thread core pin */
#endif
#include "native_video_writer.h"

#include <fcntl.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stdint.h>
/* Step 20 (2026-05-27): NEON intrinsics for 128-bit DDR3 stores in the
 * no-squish fast path of WriteFrame. Cortex-A9 + -mfpu=neon -mfloat-abi=hard
 * build flags (see CLAUDE.md OpenBOR build config) guarantee NEON support. */
#include <arm_neon.h>

#define NV_DDR_PHYS_BASE    0x3A000000u
#define NV_DDR_REGION_SIZE  0x00100000u   /* 1MB covers buffers + control + cart data */
#define NV_CTRL_OFFSET      0x00000000u
#define NV_JOY0_OFFSET      0x00000008u
#define NV_CART_CTRL_OFFSET  0x00000010u
#define NV_JOY1_OFFSET      0x00000018u
#define NV_JOY2_OFFSET      0x00000020u
#define NV_JOY3_OFFSET      0x00000028u
#define NV_BUF0_OFFSET      0x00000040u
#define NV_BUF1_OFFSET      0x00040040u
#define NV_CART_DATA_OFFSET  0x00080000u
#define NV_CART_MAX_SIZE     0x00040000u  /* 256KB max PAK size via OSD */
#define NV_FRAME_WIDTH      320
#define NV_FRAME_HEIGHT     224   /* Sega CD V28 NTSC */
#define NV_FRAME_BYTES      (NV_FRAME_WIDTH * NV_FRAME_HEIGHT * 2)  /* 143,360 */

static const uint32_t joy_offsets[4] = {
    NV_JOY0_OFFSET, NV_JOY1_OFFSET, NV_JOY2_OFFSET, NV_JOY3_OFFSET
};

static int mem_fd = -1;
static volatile uint8_t* ddr_base = NULL;
static uint32_t frame_counter = 0;
static int active_buf = 0;

bool NativeVideoWriter_Init(void) {
    /* 2026-06-07 affinity fix: pin this (engine/render/main) thread to core 1.
     * The handler now launches with taskset 0x03 (both cores); previously 0x02
     * (core 1 only) silently EINVAL'd the audio thread's core-0 pin. Pinning
     * render to core 1 keeps it on its cache-warm core while audio moves to
     * core 0 (sblaster_patch.c), so audio stops contending with the render loop.
     * Init runs once at startup on the main thread, so this pins the render thread. */
    {
        cpu_set_t _cs;
        CPU_ZERO(&_cs);
        CPU_SET(1, &_cs);
        if (sched_setaffinity(0, sizeof(_cs), &_cs) != 0) {
            perror("NativeVideoWriter: sched_setaffinity core 1");
        } else {
            fprintf(stderr, "NativeVideoWriter: render thread pinned to core 1\n");
        }
    }

    mem_fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (mem_fd < 0) {
        perror("NativeVideoWriter: open /dev/mem");
        return false;
    }

    ddr_base = (volatile uint8_t*)mmap(NULL, NV_DDR_REGION_SIZE,
        PROT_READ | PROT_WRITE, MAP_SHARED, mem_fd, NV_DDR_PHYS_BASE);
    if (ddr_base == MAP_FAILED) {
        perror("NativeVideoWriter: mmap");
        ddr_base = NULL;
        close(mem_fd);
        mem_fd = -1;
        return false;
    }

    /* Clear both buffers, control words, AND all per-player joystick
     * offsets. Cart's frame-0 reads stale DDR3 from previous core if
     * Init doesn't zero everything the engine polls. OpenBOR currently
     * uses btn() (held state) more than btn_pressed() so the symptom
     * doesn't surface, but matches the universal hybrid-core rule. */
    memset((void*)(ddr_base + NV_BUF0_OFFSET), 0, NV_FRAME_BYTES);
    memset((void*)(ddr_base + NV_BUF1_OFFSET), 0, NV_FRAME_BYTES);
    volatile uint32_t* ctrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
    *ctrl = 0;
    volatile uint32_t* cart_ctrl = (volatile uint32_t*)(ddr_base + NV_CART_CTRL_OFFSET);
    *cart_ctrl = 0;
    for (int i = 0; i < 4; i++) {
        *(volatile uint32_t*)(ddr_base + joy_offsets[i]) = 0;
    }
    frame_counter = 0;
    active_buf = 0;

    fprintf(stderr, "NativeVideoWriter: mapped 0x%08X, %dx%d @ %d bytes/frame\n",
            NV_DDR_PHYS_BASE, NV_FRAME_WIDTH, NV_FRAME_HEIGHT, NV_FRAME_BYTES);
    return true;
}

void NativeVideoWriter_Shutdown(void) {
    if (ddr_base) {
        volatile uint32_t* ctrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
        *ctrl = 0;
        munmap((void*)ddr_base, NV_DDR_REGION_SIZE);
        ddr_base = NULL;
    }
    if (mem_fd >= 0) {
        close(mem_fd);
        mem_fd = -1;
    }
}

/* (2026-06-08) Pass-2 cross-Laplacian unsharp REMOVED. It made 7533 read
 * jaggier than 4086 (which has no sharpen) on ATOV's 1:1-X sprites/text. The
 * 32bpp downscale now ships the box area-average (PASS 1) only, packed straight
 * to RGB565 with no edge enhancement. */

void NativeVideoWriter_WriteFrame(const void* pixels, int width, int height,
                                  int pitch, int bpp, const void* palette) {
    if (!ddr_base || !pixels) return;
    if (width <= 0 || height <= 0) return;

    /* Anisotropic nearest-neighbor squish: source W×H → NV_FRAME_WIDTH×HEIGHT.
     * Sega CD V28 NTSC active area = 320×224. 320×240 PAKs (ATOV, etc.)
     * get ~7% Y compress; sub-native PAKs (480×272, 960×480) get larger
     * downscale. Aspect distortion intentional — matches Sega CD displayed
     * area edge-to-edge per NTSC region match rule. NN avoids the per-pixel
     * cost of bilinear (~4× faster on Cortex-A9 — 2026-05-22 measurement). */
    int sx256 = (width * 256) / NV_FRAME_WIDTH;
    int sy256 = (height * 256) / NV_FRAME_HEIGHT;
    if (sx256 == 0) sx256 = 1;
    if (sy256 == 0) sy256 = 1;

    /* MiSTer 2026-05-27 Step 18: precompute src_x lookup table once per
     * frame. Hoists (x * sx256) / 256 + clamp out of the inner pixel loop.
     * Saves 1 mul + 1 div + 1 compare per dest pixel (71680 px/frame on
     * 320x224). Same arithmetic, byte-identical output, ~20-30% lift on
     * the WriteFrame inner loop. Identifies vcopy as JL Legacy's dominant
     * cost (53% of per-frame budget; SUB-PROFILE v9 measurement 2026-05-27). */
    uint16_t src_x_table[NV_FRAME_WIDTH];
    {
        int wm1 = width - 1;
        for (int x = 0; x < NV_FRAME_WIDTH; x++) {
            int sx = (x * sx256) / 256;
            src_x_table[x] = (uint16_t)((sx >= width) ? wm1 : sx);
        }
    }

    uint32_t buf_offset = (active_buf == 0) ? NV_BUF0_OFFSET : NV_BUF1_OFFSET;
    volatile uint16_t* dst = (volatile uint16_t*)(ddr_base + buf_offset);

    if (bpp == 16) {
        /* OpenBOR's 16bpp surfaces are BGR565 (B in high bits). The FPGA
         * decoder expects RGB565. Swap R and B 5-bit fields per pixel. */
        const uint8_t* src = (const uint8_t*)pixels;
        for (int y = 0; y < NV_FRAME_HEIGHT; y++) {
            int src_y = (y * sy256) / 256;
            if (src_y >= height) src_y = height - 1;
            const uint16_t* src_row = (const uint16_t*)(src + src_y * pitch);
            volatile uint16_t* dst_row = dst + y * NV_FRAME_WIDTH;

            /* Step 20 (2026-05-27): wider DDR3 stores. JL Legacy vcopy
             * measured 53% of per-frame budget on heavy combat scenes
             * (SUB-PROFILE v9, 2026-05-27). Per-pixel scalar 16-bit stores
             * to DDR3 are bus-inefficient; widening to uint64_t (4 px) and
             * NEON 128-bit (8 px) lets the write-combine buffer issue
             * fuller DDR3 bursts. NV_FRAME_WIDTH=320 is divisible by 8 so
             * neither path needs a scalar tail. */
            if (width == NV_FRAME_WIDTH && ((uintptr_t)src_row & 15) == 0) {
                /* NEON fast path — no squish + 16-byte-aligned source.
                 * Process 8 pixels per iteration. BGR565 -> RGB565: swap
                 * the low-5 (R) and high-5 (B) fields per pixel. Green (mid
                 * 6 bits) stays in place. */
                const uint16x8_t mask_r = vdupq_n_u16(0x001F);
                const uint16x8_t mask_g = vdupq_n_u16(0x07E0);
                const uint16x8_t mask_b = vdupq_n_u16(0xF800);
                for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                    uint16x8_t px = vld1q_u16(src_row + x);
                    uint16x8_t r = vandq_u16(px, mask_r);
                    uint16x8_t g = vandq_u16(px, mask_g);
                    uint16x8_t b = vandq_u16(px, mask_b);
                    uint16x8_t r_shifted = vshlq_n_u16(r, 11);
                    uint16x8_t b_shifted = vshrq_n_u16(b, 11);
                    uint16x8_t out = vorrq_u16(vorrq_u16(r_shifted, g), b_shifted);
                    vst1q_u16((uint16_t*)(dst_row + x), out);
                }
            } else {
                /* Step K v2 (v3.1 perf, 2026-05-28): NEON wide-source squish.
                 *
                 * v1 used gather[8] stack array + vld1q_u16(gather) which
                 * incurred 8 STRH + 1 VLD1 = 9 wasteful memory ops just to
                 * get values into a NEON register. Avengers v3.1 measurement
                 * showed vcopy got SLOWER per-frame (7.21 vs 4.45 ms in v12)
                 * partly because of this round-trip pattern.
                 *
                 * v2 uses vsetq_lane_u16 to pack values directly into NEON
                 * register lanes — no memory traffic, just 8 LDRH + 8 lane-
                 * insert micro-ops. Result: NEON convert + store still wins
                 * over scalar uint64_t-packed for the wide-source squish.
                 *
                 * Expected: Avengers vcopy 7.21 -> ~3-4 ms/frame (+3-5 fps)
                 *           He-Man vcopy ~6.1 -> ~3-4 ms/frame */
                const uint16x8_t mask_r = vdupq_n_u16(0x001F);
                const uint16x8_t mask_g = vdupq_n_u16(0x07E0);
                const uint16x8_t mask_b = vdupq_n_u16(0xF800);
                for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                    /* Pack 8 indexed scalar loads directly into NEON register
                     * lanes — no stack round-trip. ARMv7 has no native gather
                     * instruction, but vsetq_lane is a register-to-register
                     * micro-op (after the LDRH brings the value into a GPR). */
                    uint16x8_t px = vdupq_n_u16(0);
                    px = vsetq_lane_u16(src_row[src_x_table[x + 0]], px, 0);
                    px = vsetq_lane_u16(src_row[src_x_table[x + 1]], px, 1);
                    px = vsetq_lane_u16(src_row[src_x_table[x + 2]], px, 2);
                    px = vsetq_lane_u16(src_row[src_x_table[x + 3]], px, 3);
                    px = vsetq_lane_u16(src_row[src_x_table[x + 4]], px, 4);
                    px = vsetq_lane_u16(src_row[src_x_table[x + 5]], px, 5);
                    px = vsetq_lane_u16(src_row[src_x_table[x + 6]], px, 6);
                    px = vsetq_lane_u16(src_row[src_x_table[x + 7]], px, 7);
                    /* NEON convert BGR565 -> RGB565 (same as Step 20 fast path). */
                    uint16x8_t r = vandq_u16(px, mask_r);
                    uint16x8_t g = vandq_u16(px, mask_g);
                    uint16x8_t b = vandq_u16(px, mask_b);
                    uint16x8_t r_shifted = vshlq_n_u16(r, 11);
                    uint16x8_t b_shifted = vshrq_n_u16(b, 11);
                    uint16x8_t out = vorrq_u16(vorrq_u16(r_shifted, g), b_shifted);
                    vst1q_u16((uint16_t*)(dst_row + x), out);
                }
            }
        }
    }
    else if (bpp == 8 && palette) {
        /* 8bpp paletted — convert through palette to RGB565.
         * OpenBOR s_screen palette: 3 bytes per entry (R, G, B), 256 entries. */
        const uint8_t* src = (const uint8_t*)pixels;
        const uint8_t* pal = (const uint8_t*)palette;
        for (int y = 0; y < NV_FRAME_HEIGHT; y++) {
            int src_y = (y * sy256) / 256;
            if (src_y >= height) src_y = height - 1;
            const uint8_t* row = src + src_y * pitch;
            volatile uint16_t* dst_row = dst + y * NV_FRAME_WIDTH;
            /* Step 20: uint64_t-packed writes (4 px per store). Palette
             * lookup gather makes NEON impractical; uint64_t packing alone
             * gives ~1.5-2x DDR3 write-side speedup. */
            for (int x = 0; x < NV_FRAME_WIDTH; x += 4) {
                uint16_t out[4];
                for (int k = 0; k < 4; k++) {
                    uint8_t idx = row[src_x_table[x + k]];
                    uint8_t r = pal[idx * 3 + 0];
                    uint8_t g = pal[idx * 3 + 1];
                    uint8_t b = pal[idx * 3 + 2];
                    out[k] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
                }
                uint64_t packed = ((uint64_t)out[0]) | ((uint64_t)out[1] << 16)
                                | ((uint64_t)out[2] << 32) | ((uint64_t)out[3] << 48);
                *(volatile uint64_t*)(dst_row + x) = packed;
            }
        }
    }
    else if (bpp == 32) {
        /* 32bpp RGBA -- byte-0=R, byte-1=G, byte-2=B, byte-3=A.
         *
         * MiSTer 2026-06-06 (v3.2 quality): AREA-AVERAGE (box filter) downscale,
         * replacing the nearest-neighbor src_x_table path for the 32bpp case.
         * The v3.9/v3.10 palette pipeline forces hi-res PAKs (He-Man 960x480,
         * Avengers/PDC2 480x272) through PIXEL_32, so this is the path where
         * downscale quality matters most on a CRT. NN kept only 1 of every 3
         * He-Man columns (aliased/janky text); box average folds every source
         * pixel in each output pixel's footprint into the result.
         *
         * Separable box, single divide: accumulate the raw 2D block sum, then
         * divide ONCE per output pixel via a reciprocal-multiply keyed on the
         * block area (h*vcnt). Fewer multiplies than per-row averaging and one
         * rounding step instead of two. The horizontal block table is cached
         * across frames (recomputed only on a resolution change). The filter
         * self-degenerates to a 1:1 copy on any axis that isn't downscaled
         * (ATOV is 320 wide -> hcnt==1), so near-native PAKs look identical.
         *
         * 8bpp/16bpp paths still use NN (src_x_table) -- extended in a later
         * step. See CLAUDE.md video-pipeline section (this reverses the prior
         * "NN not bilinear" squish decision for sub-native downscale). */
        const uint8_t* src = (const uint8_t*)pixels;

        /* Cached horizontal block table: source column span [hx0, hx0+hcnt)
         * per output column. Only recomputed when the PAK width changes, not
         * per frame. static is safe -- WriteFrame runs only on the render
         * thread; the keepalive thread uses KeepaliveTick and never enters. */
        static uint16_t s_hx0[NV_FRAME_WIDTH];
        static uint16_t s_hcnt[NV_FRAME_WIDTH];
        static int s_htab_width   = -1;
        static int s_htab_maxhcnt = 1;
        if (width != s_htab_width) {
            int maxh = 1;
            for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                int x0 = (int)(((long)x * width) / NV_FRAME_WIDTH);
                int x1 = (int)(((long)(x + 1) * width) / NV_FRAME_WIDTH);
                if (x1 <= x0) x1 = x0 + 1;
                if (x1 > width)  x1 = width;
                if (x0 >= width) x0 = width - 1;
                s_hx0[x]  = (uint16_t)x0;
                s_hcnt[x] = (uint16_t)(x1 - x0);
                if ((x1 - x0) > maxh) maxh = x1 - x0;
            }
            if (maxh > 63) maxh = 63;   /* recip[] table bound (defensive) */
            s_htab_width   = width;
            s_htab_maxhcnt = maxh;
        }

        /* Raw 2D block-sum accumulators; one divide per output pixel.
         * uint16 is ample for the box path: width<=1280 && height<=896 ->
         * block area <= 4x4=16 -> max sum 16*255=4080 << 65535. The >4x
         * stride-cap path writes s_avg directly and never touches these.
         * 16-bit (vs the prior uint32) halves the NEON vertical-accumulate:
         * 8 lanes/store instead of 4, AND drops the vmovl_u16 widening, AND
         * halves accumulator memory traffic -- the dominant vcopy cost.
         * (The prior "~7650 at 1080p" note was wrong: 1080p uses the
         * stride-cap path, not these accumulators.) */
        uint16_t vr[NV_FRAME_WIDTH], vg[NV_FRAME_WIDTH], vb[NV_FRAME_WIDTH];

        /* Averaged 8-bit RGB intermediate. Pass 1 fills it from the box
         * average; pass 2 reads 3x3 neighborhoods for the unsharp + packs to
         * DDR3. static (render-thread only). ~215 KB. */
        static uint8_t s_avg[NV_FRAME_HEIGHT * NV_FRAME_WIDTH * 3];

        /* TEMPORARY DIAG (REVERT AFTER MEASURED): verify the 16-bit NEON
         * accumulate is byte-identical to an independent scalar reference, for
         * the first 2 He-Man frames. Compares the raw block SUMS (vr/vg/vb),
         * not the divided output, so the reciprocal-multiply rounding can't
         * cause a false mismatch. Result logged as [VCV] after PASS 1. */
        static int  s_vcv_frame    = 0;
        static long s_vcv_mismatch = 0;
        int vcv_active = (s_vcv_frame < 2 && width == NV_FRAME_WIDTH * 3);
        if (vcv_active) s_vcv_mismatch = 0;

        /* PASS 1: box area-average -> 8-bit s_avg (no DDR3 write yet). */
        for (int y = 0; y < NV_FRAME_HEIGHT; y++) {
            int y0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
            int y1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
            if (y1 <= y0) y1 = y0 + 1;
            if (y1 > height)  y1 = height;
            if (y0 >= height) y0 = height - 1;
            int vcnt = y1 - y0;

            /* >4x downscale (e.g. Lust Rush 1920x1080): cap the box to a 4x4
             * evenly-spaced sample grid per output pixel so read cost stays
             * bounded regardless of ratio. Exact for blocks <=4x4; a clean
             * subsample above that. Only fires when source >1280 wide or >896
             * tall -- every PAK <=960x480 keeps the exact box/NEON path below. */
            if (width > NV_FRAME_WIDTH * 4 || height > NV_FRAME_HEIGHT * 4) {
                uint8_t* arow = s_avg + (size_t)y * NV_FRAME_WIDTH * 3;
                int bh = y1 - y0;
                for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                    int x0 = (int)(((long)x * width) / NV_FRAME_WIDTH);
                    int x1 = (int)(((long)(x + 1) * width) / NV_FRAME_WIDTH);
                    if (x1 <= x0) x1 = x0 + 1;
                    if (x1 > width)  x1 = width;
                    if (x0 >= width) x0 = width - 1;
                    int bw = x1 - x0;
                    uint32_t rs = 0, gs = 0, bs = 0;
                    for (int j = 0; j < 4; j++) {
                        int sy = y0 + (j * bh) / 4;
                        if (sy >= height) sy = height - 1;
                        const uint8_t* row = src + (size_t)sy * pitch;
                        for (int k = 0; k < 4; k++) {
                            int sx = x0 + (k * bw) / 4;
                            if (sx >= width) sx = width - 1;
                            const uint8_t* p = row + (size_t)sx * 4;
                            rs += p[0]; gs += p[1]; bs += p[2];
                        }
                    }
                    rs = (rs + 8) >> 4;   /* divide by 16 (4x4 samples), rounded */
                    gs = (gs + 8) >> 4;
                    bs = (bs + 8) >> 4;
                    arow[x * 3 + 0] = (uint8_t)rs;
                    arow[x * 3 + 1] = (uint8_t)gs;
                    arow[x * 3 + 2] = (uint8_t)bs;
                }
                continue;   /* stride-cap wrote s_avg directly; skip the box path */
            }

            /* Combined reciprocal per distinct horizontal block width:
             * recip[h] = (1<<20) / (h * vcnt). out = (block_sum*recip + half)
             * >> 20 = block_sum / (h*vcnt). Only a few distinct h values, so
             * this replaces a per-pixel integer divide. */
            uint32_t recip[64];
            for (int h = 1; h <= s_htab_maxhcnt; h++) {
                recip[h] = (uint32_t)((1u << 20) / ((uint32_t)h * (uint32_t)vcnt));
            }

            memset(vr, 0, sizeof(vr));
            memset(vg, 0, sizeof(vg));
            memset(vb, 0, sizeof(vb));

            /* Accumulate raw block sums across the source-row band (no divide). */
#ifdef __ARM_NEON
            if (width == NV_FRAME_WIDTH * 3) {
                /* MiSTer 2026-06-07: exact 3x horizontal box via NEON (He-Man
                 * 960->320). Deinterleave RGBA -> R/G/B planes (vld4q), then
                 * vld3 deinterleave-by-3 yields group-of-3 sums directly --
                 * byte-identical to the scalar box for hcnt==3 (buffer-verified).
                 * Only the exact 3x case qualifies (320*3==960, 48-aligned, no
                 * boundary spill); every other ratio uses the scalar path. */
                static uint8_t planeR[NV_FRAME_WIDTH * 3];
                static uint8_t planeG[NV_FRAME_WIDTH * 3];
                static uint8_t planeB[NV_FRAME_WIDTH * 3];
                for (int sy = y0; sy < y1; sy++) {
                    const uint8_t* row = src + (size_t)sy * pitch;
                    for (int sx = 0; sx < NV_FRAME_WIDTH * 3; sx += 16) {
                        uint8x16x4_t px = vld4q_u8(row + (size_t)sx * 4);
                        vst1q_u8(planeR + sx, px.val[0]);
                        vst1q_u8(planeG + sx, px.val[1]);
                        vst1q_u8(planeB + sx, px.val[2]);
                    }
                    for (int x = 0; x < NV_FRAME_WIDTH; x += 16) {
                        int sx = x * 3;
                        uint8x16x3_t gr = vld3q_u8(planeR + sx);
                        uint8x16x3_t gg = vld3q_u8(planeG + sx);
                        uint8x16x3_t gb = vld3q_u8(planeB + sx);
                        uint16x8_t rlo = vaddw_u8(vaddl_u8(vget_low_u8(gr.val[0]),  vget_low_u8(gr.val[1])),  vget_low_u8(gr.val[2]));
                        uint16x8_t rhi = vaddw_u8(vaddl_u8(vget_high_u8(gr.val[0]), vget_high_u8(gr.val[1])), vget_high_u8(gr.val[2]));
                        uint16x8_t glo = vaddw_u8(vaddl_u8(vget_low_u8(gg.val[0]),  vget_low_u8(gg.val[1])),  vget_low_u8(gg.val[2]));
                        uint16x8_t ghi = vaddw_u8(vaddl_u8(vget_high_u8(gg.val[0]), vget_high_u8(gg.val[1])), vget_high_u8(gg.val[2]));
                        uint16x8_t blo = vaddw_u8(vaddl_u8(vget_low_u8(gb.val[0]),  vget_low_u8(gb.val[1])),  vget_low_u8(gb.val[2]));
                        uint16x8_t bhi = vaddw_u8(vaddl_u8(vget_high_u8(gb.val[0]), vget_high_u8(gb.val[1])), vget_high_u8(gb.val[2]));
                        /* 16-bit accumulate: rlo->cols x..x+7, rhi->x+8..x+15
                         * (same lane mapping as the prior uint32 stores). 8
                         * lanes/store, no vmovl widening. */
                        vst1q_u16(&vr[x],     vaddq_u16(vld1q_u16(&vr[x]),     rlo));
                        vst1q_u16(&vr[x + 8], vaddq_u16(vld1q_u16(&vr[x + 8]), rhi));
                        vst1q_u16(&vg[x],     vaddq_u16(vld1q_u16(&vg[x]),     glo));
                        vst1q_u16(&vg[x + 8], vaddq_u16(vld1q_u16(&vg[x + 8]), ghi));
                        vst1q_u16(&vb[x],     vaddq_u16(vld1q_u16(&vb[x]),     blo));
                        vst1q_u16(&vb[x + 8], vaddq_u16(vld1q_u16(&vb[x + 8]), bhi));
                    }
                }
            } else
#endif
            {
                for (int sy = y0; sy < y1; sy++) {
                    const uint8_t* row = src + (size_t)sy * pitch;
                    for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                        const uint8_t* p = row + (size_t)s_hx0[x] * 4;
                        uint32_t rs = 0, gs = 0, bs = 0;
                        int n = s_hcnt[x];
                        for (int k = 0; k < n; k++) {
                            rs += p[0]; gs += p[1]; bs += p[2];
                            p += 4;
                        }
                        vr[x] += rs; vg[x] += gs; vb[x] += bs;
                    }
                }
            }

            /* TEMPORARY DIAG (REVERT AFTER MEASURED): cross-check vr/vg/vb
             * against an independent scalar sum over the SAME block bounds
             * (y0..y1, s_hx0/s_hcnt). Catches any NEON 16-bit-lane mistake. */
            if (vcv_active) {
                for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                    uint32_t rr = 0, rg = 0, rb = 0;
                    int n = s_hcnt[x];
                    for (int sy = y0; sy < y1; sy++) {
                        const uint8_t* p = src + (size_t)sy * pitch + (size_t)s_hx0[x] * 4;
                        for (int k = 0; k < n; k++) { rr += p[0]; rg += p[1]; rb += p[2]; p += 4; }
                    }
                    if ((uint32_t)vr[x] != rr || (uint32_t)vg[x] != rg || (uint32_t)vb[x] != rb)
                        s_vcv_mismatch++;
                }
            }

            /* One rounded divide per output pixel -> store 8-bit RGB. */
            uint8_t* arow = s_avg + (size_t)y * NV_FRAME_WIDTH * 3;
            for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                uint32_t rc = recip[s_hcnt[x]];
                uint32_t r = (vr[x] * rc + (1u << 19)) >> 20;
                uint32_t g = (vg[x] * rc + (1u << 19)) >> 20;
                uint32_t b = (vb[x] * rc + (1u << 19)) >> 20;
                if (r > 255) r = 255;
                if (g > 255) g = 255;
                if (b > 255) b = 255;
                arow[x * 3 + 0] = (uint8_t)r;
                arow[x * 3 + 1] = (uint8_t)g;
                arow[x * 3 + 2] = (uint8_t)b;
            }
        }

        /* TEMPORARY DIAG (REVERT AFTER MEASURED): vcopy verify result. */
        if (vcv_active) {
            fprintf(stderr, "[VCV] frame=%d 16bit-accum vs scalar-ref: %s (mismatch=%ld)\n",
                    s_vcv_frame, s_vcv_mismatch == 0 ? "MATCH" : "MISMATCH",
                    s_vcv_mismatch);
            s_vcv_frame++;
        }

        /* PASS 2: pack the box-averaged image straight to RGB565 — NO sharpen.
         * (2026-06-08: the cross-Laplacian unsharp was removed; it read jaggier
         * than 4086 on ATOV. The box area-average in PASS 1 is the only filter.) */
        for (int y = 0; y < NV_FRAME_HEIGHT; y++) {
            const uint8_t* arow = s_avg + (size_t)y * NV_FRAME_WIDTH * 3;
            volatile uint16_t* dst_row = dst + y * NV_FRAME_WIDTH;
            for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                uint8_t r = arow[x * 3 + 0];
                uint8_t g = arow[x * 3 + 1];
                uint8_t b = arow[x * 3 + 2];
                dst_row[x] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
            }
        }
    }
    else {
        return;  /* unsupported format, skip frame */
    }

    /* Step 20 (2026-05-27) defensive barrier: ensure ALL pixel writes
     * (scalar, uint64_t-packed, OR NEON 128-bit) drain to DDR3 BEFORE
     * the FPGA sees the new ctrl word and starts reading the buffer we
     * just finished writing. The double-buffer flip already protects
     * against most tearing (FPGA reads OPPOSITE buffer from the one we
     * write), but NEON stores can drain through the write-combine buffer
     * at a different rate than scalar stores -- if ctrl is updated before
     * the buffer fully drains AND the FPGA pipeline races ahead, the very
     * first lines of the new frame could read partially-written pixels.
     * __sync_synchronize() generates ARMv7 DMB SY (full memory barrier);
     * costs ~2 cycles, negligible. */
    __sync_synchronize();

    /* Flip control word */
    frame_counter++;
    volatile uint32_t* ctrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
    *ctrl = (frame_counter << 2) | (active_buf & 1);
    active_buf ^= 1;
}

bool NativeVideoWriter_IsActive(void) {
    return ddr_base != NULL;
}

void NativeVideoWriter_KeepaliveTick(void) {
    /* Tick frame_counter pointing at the LAST-WRITTEN buffer (not next-
     * to-write). After WriteFrame's active_buf toggle, the last-written
     * buffer is (!active_buf). Pointing the FPGA at next-to-write would
     * flip it to a stale/empty buffer, causing jitter between frames
     * (verified 2026-05-22 — loading bar jitter root cause was a
     * separate keepalive thread maintaining its own frame_counter +
     * active_buf state, racing with WriteFrame's state). */
    if (!ddr_base) return;
    frame_counter++;
    int last_written = (!active_buf) & 1;
    volatile uint32_t* ctrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
    *ctrl = (frame_counter << 2) | last_written;
}

uint32_t NativeVideoWriter_CheckCart(void) {
    if (!ddr_base) return 0;
    volatile uint32_t *ctrl = (volatile uint32_t *)(ddr_base + NV_CART_CTRL_OFFSET);
    uint32_t val = *ctrl;
    if (val > NV_CART_MAX_SIZE) return 0;
    return val;
}

uint32_t NativeVideoWriter_ReadCart(void* buf, uint32_t max_size) {
    if (!ddr_base || !buf) return 0;
    uint32_t file_size = NativeVideoWriter_CheckCart();
    if (file_size == 0) return 0;
    if (file_size > max_size) file_size = max_size;
    if (file_size > NV_CART_MAX_SIZE) file_size = NV_CART_MAX_SIZE;
    memcpy(buf, (const void *)(ddr_base + NV_CART_DATA_OFFSET), file_size);
    return file_size;
}

void NativeVideoWriter_AckCart(void) {
    if (!ddr_base) return;
    volatile uint32_t *ctrl = (volatile uint32_t *)(ddr_base + NV_CART_CTRL_OFFSET);
    *ctrl = 0;
}

uint32_t NativeVideoWriter_ReadJoystick(int player) {
    if (!ddr_base || player < 0 || player > 3) return 0;
    volatile uint32_t *joy = (volatile uint32_t *)(ddr_base + joy_offsets[player]);
    return *joy;
}
