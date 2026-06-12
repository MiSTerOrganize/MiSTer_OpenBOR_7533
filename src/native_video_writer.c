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
#include <time.h>   /* TEMPORARY DIAG (REVERT AFTER MEASURED): [VCP] vcopy profile */
/* Step 20 (2026-05-27): NEON intrinsics for 128-bit DDR3 stores in the
 * no-squish fast path of WriteFrame. Cortex-A9 + -mfpu=neon -mfloat-abi=hard
 * build flags (see CLAUDE.md OpenBOR build config) guarantee NEON support. */
#include <arm_neon.h>

/* TEMPORARY DIAG (REVERT AFTER MEASURED): monotonic-ns clock for the [VCP]
 * vcopy-internal timing split (deinterleave vs accumulate vs divide). */
static inline uint64_t nv_now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    /* uint64_t: on ARMv7 `unsigned long` is 32-bit -> tv_sec*1e9 overflows and
     * a 5e9-ns (5s) gate becomes provably-false, so GCC DCE'd the whole log. */
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

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

    /* TEMP DIAG: unconditional one-time marker at WriteFrame top level. If
     * WFMARKER survives but [VCP] doesn't, the bpp==32 branch (or the deep
     * conditional) is being eliminated; prints actual bpp/width at runtime. */
    { static int _wf_m = 0; if (!_wf_m) { _wf_m = 1;
        fprintf(stderr, "WFMARKER_TOPLEVEL bpp=%d w=%d h=%d\n", bpp, width, height); } }

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
                /* MiSTer Path B Build 2: AREA-AVERAGE (box) 16-bit downscale,
                 * replacing nearest-neighbor for the squish case (He-Man
                 * 960x480 -> 320x224, 3x X). Source is BGR565; unpack to 8-bit
                 * R/G/B, average the output pixel's source-block footprint,
                 * pack to RGB565 (same output layout as the 32bpp box + the
                 * FPGA decoder). recip[] avoids a per-pixel divide (A9 has no
                 * HW integer divide). Scalar: the 16-bit vscreen already halved
                 * the bandwidth; a NEON box-16 is a later optimization. Self-
                 * degenerates to a copy on any axis not downscaled. */
                int yy0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
                int yy1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
                if (yy1 <= yy0) yy1 = yy0 + 1;
                if (yy1 > height) yy1 = height;
                if (yy0 >= height) yy0 = height - 1;
                int vcnt = yy1 - yy0;
                const uint8_t* sbase = src + (size_t)yy0 * pitch;
                uint32_t recip16[8];
                {
                    int h;
                    for (h = 1; h < 8; h++) recip16[h] = (uint32_t)((1u << 20) / ((uint32_t)h * (uint32_t)vcnt));
                }
                for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                    int x0 = (int)(((long)x * width) / NV_FRAME_WIDTH);
                    int x1 = (int)(((long)(x + 1) * width) / NV_FRAME_WIDTH);
                    if (x1 <= x0) x1 = x0 + 1;
                    if (x1 > width) x1 = width;
                    if (x0 >= width) x0 = width - 1;
                    int hcnt = x1 - x0;
                    if (hcnt > 7) hcnt = 7;
                    uint32_t rs = 0, gs = 0, bs = 0;
                    const uint8_t* rowp = sbase;
                    for (int syy = 0; syy < vcnt; syy++) {
                        const uint16_t* sp = (const uint16_t*)(rowp) + x0;
                        for (int k = 0; k < hcnt; k++) {
                            uint16_t pix = sp[k];
                            uint32_t b5 = (pix >> 11) & 0x1F;
                            uint32_t g6 = (pix >> 5) & 0x3F;
                            uint32_t r5 = pix & 0x1F;
                            rs += (r5 << 3) | (r5 >> 2);
                            gs += (g6 << 2) | (g6 >> 4);
                            bs += (b5 << 3) | (b5 >> 2);
                        }
                        rowp += pitch;
                    }
                    uint32_t rc = recip16[hcnt];
                    uint32_t r8 = (rs * rc + (1u << 19)) >> 20;
                    uint32_t g8 = (gs * rc + (1u << 19)) >> 20;
                    uint32_t b8 = (bs * rc + (1u << 19)) >> 20;
                    dst_row[x] = (uint16_t)(((r8 >> 3) << 11) | ((g8 >> 2) << 5) | (b8 >> 3));
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
         * stride-cap path packs to dst directly and never touches these.
         * 16-bit (vs the prior uint32) halves the NEON vertical-accumulate:
         * 8 lanes/store instead of 4, AND drops the vmovl_u16 widening, AND
         * halves accumulator memory traffic -- the dominant vcopy cost.
         * (The prior "~7650 at 1080p" note was wrong: 1080p uses the
         * stride-cap path, not these accumulators.) */
        uint16_t vr[NV_FRAME_WIDTH], vg[NV_FRAME_WIDTH], vb[NV_FRAME_WIDTH];

        /* Single fused pass: box area-average each output row and pack it
         * straight to RGB565 in DDR3 (dst). The prior 2-pass s_avg 8-bit
         * intermediate existed only to feed the unsharp's 3x3 reads in pass 2;
         * the unsharp was removed 2026-06-08, so the staging buffer + the
         * second pass are gone -- saves a 215 KB write + 215 KB read + a full
         * output-size pass per frame. */

        /* TEMPORARY DIAG (REVERT AFTER MEASURED): vcopy-internal timing split,
         * He-Man (NEON 3x) path only. Decides whether opt C (kill the plane
         * round-trip) is worth it: if deint dominates, yes. */
        static uint64_t s_vcp_deint_ns = 0, s_vcp_accum_ns = 0, s_vcp_div_ns = 0;
        static uint64_t s_vcp_last_ns  = 0;
        static int s_vcp_frames = 0;
        int vcp_active = (width == NV_FRAME_WIDTH * 3);
        /* TEMP DIAG [DCV]: verify the NEON divide+pack == scalar, first 2 frames. */
        static int s_dcv_frame = 0;
        static long s_dcv_mismatch = 0;
        int dcv_active = (width == NV_FRAME_WIDTH * 3 && s_dcv_frame < 2);
        if (dcv_active) s_dcv_mismatch = 0;

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
                volatile uint16_t* dst_row = dst + (size_t)y * NV_FRAME_WIDTH;
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
                    dst_row[x] = (uint16_t)(((rs >> 3) << 11) | ((gs >> 2) << 5) | (bs >> 3));
                }
                continue;   /* stride-cap packed straight to DDR3; skip the box path */
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
                    uint64_t _ta = nv_now_ns();   /* TEMP DIAG */
                    for (int sx = 0; sx < NV_FRAME_WIDTH * 3; sx += 16) {
                        /* PLD: prefetch source ~256 B (4 iters) ahead -- hides
                         * read latency on the streaming vld4q. Zero-risk hint:
                         * byte-identical output, cannot crash. */
                        __builtin_prefetch(row + (size_t)sx * 4 + 256, 0, 0);
                        uint8x16x4_t px = vld4q_u8(row + (size_t)sx * 4);
                        vst1q_u8(planeR + sx, px.val[0]);
                        vst1q_u8(planeG + sx, px.val[1]);
                        vst1q_u8(planeB + sx, px.val[2]);
                    }
                    uint64_t _tb = nv_now_ns();   /* TEMP DIAG */
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
                    s_vcp_deint_ns += _tb - _ta;               /* TEMP DIAG */
                    s_vcp_accum_ns += nv_now_ns() - _tb;       /* TEMP DIAG */
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

            /* One rounded divide per output pixel -> pack straight to RGB565. */
            uint64_t _td = nv_now_ns();   /* TEMP DIAG */
            volatile uint16_t* dst_row = dst + (size_t)y * NV_FRAME_WIDTH;
#ifdef __ARM_NEON
            if (width == NV_FRAME_WIDTH * 3) {
                /* Exact-3x: every s_hcnt[x]==3 so the reciprocal is constant
                 * across the row -> NEON the divide + RGB565 pack, 8 px/iter
                 * with ONE vst1q_u16 to DDR3 instead of 320 per-pixel volatile
                 * stores (the dominant vcopy cost per the [VCP] profile).
                 * Byte-identical to the scalar path below (verified via [DCV]). */
                const uint32_t   rc     = recip[3];
                const uint32x4_t rc_v   = vdupq_n_u32(rc);
                const uint32x4_t halfv  = vdupq_n_u32(1u << 19);
                const uint16x8_t max255 = vdupq_n_u16(255);
                for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                    uint16x8_t vr8 = vld1q_u16(&vr[x]);
                    uint16x8_t vg8 = vld1q_u16(&vg[x]);
                    uint16x8_t vb8 = vld1q_u16(&vb[x]);
                    uint32x4_t rl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(vr8)),  rc_v), halfv), 20);
                    uint32x4_t rh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(vr8)), rc_v), halfv), 20);
                    uint32x4_t gl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(vg8)),  rc_v), halfv), 20);
                    uint32x4_t gh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(vg8)), rc_v), halfv), 20);
                    uint32x4_t bl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(vb8)),  rc_v), halfv), 20);
                    uint32x4_t bh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(vb8)), rc_v), halfv), 20);
                    uint16x8_t r16 = vminq_u16(vcombine_u16(vmovn_u32(rl), vmovn_u32(rh)), max255);
                    uint16x8_t g16 = vminq_u16(vcombine_u16(vmovn_u32(gl), vmovn_u32(gh)), max255);
                    uint16x8_t b16 = vminq_u16(vcombine_u16(vmovn_u32(bl), vmovn_u32(bh)), max255);
                    uint16x8_t out = vorrq_u16(vorrq_u16(
                                         vshlq_n_u16(vshrq_n_u16(r16, 3), 11),
                                         vshlq_n_u16(vshrq_n_u16(g16, 2), 5)),
                                         vshrq_n_u16(b16, 3));
                    vst1q_u16((uint16_t*)(dst_row + x), out);
                }
                if (dcv_active) {   /* TEMP DIAG: compare NEON output vs scalar */
                    for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                        uint32_t r = (vr[x] * rc + (1u << 19)) >> 20;
                        uint32_t g = (vg[x] * rc + (1u << 19)) >> 20;
                        uint32_t b = (vb[x] * rc + (1u << 19)) >> 20;
                        if (r > 255) r = 255; if (g > 255) g = 255; if (b > 255) b = 255;
                        uint16_t want = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
                        if (dst_row[x] != want) s_dcv_mismatch++;
                    }
                }
            } else
#endif
            {
                for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                    uint32_t rc = recip[s_hcnt[x]];
                    uint32_t r = (vr[x] * rc + (1u << 19)) >> 20;
                    uint32_t g = (vg[x] * rc + (1u << 19)) >> 20;
                    uint32_t b = (vb[x] * rc + (1u << 19)) >> 20;
                    if (r > 255) r = 255;
                    if (g > 255) g = 255;
                    if (b > 255) b = 255;
                    dst_row[x] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
                }
            }
            s_vcp_div_ns += nv_now_ns() - _td;   /* TEMP DIAG */
        }

        /* TEMP DIAG: report NEON-divide byte-identity vs scalar (first 2 frames). */
        if (dcv_active) {
            fprintf(stderr, "[DCV] frame=%d NEON-div vs scalar: %s (mismatch=%ld)\n",
                    s_dcv_frame, s_dcv_mismatch == 0 ? "MATCH" : "MISMATCH", s_dcv_mismatch);
            s_dcv_frame++;
        }

        /* TEMPORARY DIAG (REVERT AFTER MEASURED): log [VCP] every ~5s. */
        if (vcp_active) {
            s_vcp_frames++;
            uint64_t _n = nv_now_ns();
            if (s_vcp_last_ns == 0) s_vcp_last_ns = _n;
            if (_n - s_vcp_last_ns >= 5000000000ULL) {
                fprintf(stderr, "[VCP] frames=%d deint=%llums accum=%llums div=%llums\n",
                        s_vcp_frames, (unsigned long long)(s_vcp_deint_ns / 1000000ULL),
                        (unsigned long long)(s_vcp_accum_ns / 1000000ULL),
                        (unsigned long long)(s_vcp_div_ns / 1000000ULL));
                s_vcp_deint_ns = s_vcp_accum_ns = s_vcp_div_ns = 0;
                s_vcp_frames = 0;
                s_vcp_last_ns = _n;
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
