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
#include <time.h>
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
    /* 2026-06-13 affinity INVERSION: pin this (engine/render/main) thread to core 0.
     * mem_bench shows core 0 has ~1.85x core 1's DDR3 read bandwidth; the sprite
     * blend/render pass is memory-bound, so render belongs on the bandwidth-fast
     * core 0 (validated +81% on He-Man). Audio moves to core 1 (sblaster_patch.c).
     * The handler launches with taskset 0x03 (both cores). Init runs once at startup
     * on the main thread, so this pins the render thread. */
    {
        cpu_set_t _cs;
        CPU_ZERO(&_cs);
        CPU_SET(0, &_cs);
        if (sched_setaffinity(0, sizeof(_cs), &_cs) != 0) {
            perror("NativeVideoWriter: sched_setaffinity core 0");
        } else {
            fprintf(stderr, "NativeVideoWriter: render thread pinned to core 0\n");
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

/* ==========================================================================
 * FPS OVERLAY  (pause menu -> Options -> "FPS Display")
 *
 * Drawn HERE, into the final 320x224 RGB565 buffer, and deliberately NOT on
 * the engine's vscreen. A PAK renders at its own native size -- He-Man is
 * 960x480 -- and WriteFrame squishes that to 320x224. An overlay drawn engine-
 * side would be downscaled with everything else and, at 3x, become unreadable.
 * Drawing post-downscale makes it pixel-crisp and identical on every PAK.
 *
 * `mister_fps_overlay` is a global defined in openbor.c (next to `mrec_mode`)
 * so the pause menu can toggle it in both the ship and headless builds without
 * a link dependency on this translation unit.
 *
 * NOTE for the harnesses: this writes into the framebuffer, so it changes
 * screenshots and any frame-hash comparison ([DCV16] byte-identity, golden
 * traces). It does NOT affect .inp recordings -- those are input streams, and
 * replay determinism rides on inputs + RNG seed + the interval lock, none of
 * which this touches. Recording or replaying with it on is safe and is its
 * intended debugging use: an fps read-out across a whole deterministic replay.
 * Default OFF, and it resets to OFF each launch, so it can never silently
 * contaminate a byte-identity run.
 * ========================================================================== */
/* Defined in openbor.c beside mrec_mode, in BOTH builds (see apply_patches.py),
 * so this resolves wherever native_video_writer.o is linked. */
extern int mister_fps_overlay;

/* 5x7 digits, low 5 bits per row, drawn at 2x -> 10x14 px per glyph. */
static const uint8_t nv_font5x7[10][7] = {
    {0x0E,0x11,0x11,0x11,0x11,0x11,0x0E}, /* 0 */
    {0x04,0x0C,0x04,0x04,0x04,0x04,0x0E}, /* 1 */
    {0x0E,0x11,0x01,0x02,0x04,0x08,0x1F}, /* 2 */
    {0x1F,0x02,0x04,0x02,0x01,0x11,0x0E}, /* 3 */
    {0x02,0x06,0x0A,0x12,0x1F,0x02,0x02}, /* 4 */
    {0x1F,0x10,0x1E,0x01,0x01,0x11,0x0E}, /* 5 */
    {0x06,0x08,0x10,0x1E,0x11,0x11,0x0E}, /* 6 */
    {0x1F,0x01,0x02,0x04,0x08,0x08,0x08}, /* 7 */
    {0x0E,0x11,0x11,0x0E,0x11,0x11,0x0E}, /* 8 */
    {0x0E,0x11,0x11,0x0F,0x01,0x02,0x0C}, /* 9 */
};

#define NV_GLYPH_W 10   /* 5 * 2 */
#define NV_GLYPH_H 14   /* 7 * 2 */
#define NV_GLYPH_GAP 2
#define NV_FPS_MARGIN 4

/* A-Z for the notice overlay. The fps read-out only ever needed digits, so
 * until now every message this recorder emitted went to OpenBorLog.txt -- which
 * nobody reads while sitting in front of a TV holding a controller. */
static const uint8_t nv_font_az[26][7] = {
    {0x0E,0x11,0x11,0x1F,0x11,0x11,0x11}, {0x1E,0x11,0x11,0x1E,0x11,0x11,0x1E},
    {0x0E,0x11,0x10,0x10,0x10,0x11,0x0E}, {0x1E,0x11,0x11,0x11,0x11,0x11,0x1E},
    {0x1F,0x10,0x10,0x1E,0x10,0x10,0x1F}, {0x1F,0x10,0x10,0x1E,0x10,0x10,0x10},
    {0x0E,0x11,0x10,0x17,0x11,0x11,0x0F}, {0x11,0x11,0x11,0x1F,0x11,0x11,0x11},
    {0x0E,0x04,0x04,0x04,0x04,0x04,0x0E}, {0x01,0x01,0x01,0x01,0x01,0x11,0x0E},
    {0x11,0x12,0x14,0x18,0x14,0x12,0x11}, {0x10,0x10,0x10,0x10,0x10,0x10,0x1F},
    {0x11,0x1B,0x15,0x15,0x11,0x11,0x11}, {0x11,0x19,0x15,0x13,0x11,0x11,0x11},
    {0x0E,0x11,0x11,0x11,0x11,0x11,0x0E}, {0x1E,0x11,0x11,0x1E,0x10,0x10,0x10},
    {0x0E,0x11,0x11,0x11,0x15,0x12,0x0D}, {0x1E,0x11,0x11,0x1E,0x14,0x12,0x11},
    {0x0F,0x10,0x10,0x0E,0x01,0x01,0x1E}, {0x1F,0x04,0x04,0x04,0x04,0x04,0x04},
    {0x11,0x11,0x11,0x11,0x11,0x11,0x0E}, {0x11,0x11,0x11,0x11,0x11,0x0A,0x04},
    {0x11,0x11,0x11,0x15,0x15,0x1B,0x11}, {0x11,0x11,0x0A,0x04,0x0A,0x11,0x11},
    {0x11,0x11,0x0A,0x04,0x04,0x04,0x04}, {0x1F,0x01,0x02,0x04,0x08,0x10,0x1F},
};

/* Only the punctuation the messages use. */
static const uint8_t nv_font_punct[7][7] = {
    {0x00,0x00,0x00,0x0E,0x00,0x00,0x00}, /* - */
    {0x00,0x00,0x00,0x00,0x00,0x0C,0x0C}, /* . */
    {0x00,0x0C,0x0C,0x00,0x0C,0x0C,0x00}, /* : */
    {0x0E,0x11,0x01,0x02,0x04,0x00,0x04}, /* ? */
    {0x04,0x04,0x04,0x04,0x04,0x00,0x04}, /* ! */
    {0x02,0x04,0x08,0x08,0x08,0x04,0x02}, /* ( */
    {0x08,0x04,0x02,0x02,0x02,0x04,0x08}, /* ) */
};

/* NULL for space and anything unmapped: an unknown character renders as a
 * blank cell rather than dropping out, so the text keeps its shape. */
static const uint8_t* nv_glyph_rows(char c) {
    if (c >= '0' && c <= '9') return nv_font5x7[c - '0'];
    if (c >= 'A' && c <= 'Z') return nv_font_az[c - 'A'];
    if (c >= 'a' && c <= 'z') return nv_font_az[c - 'a'];
    switch (c) {
        case '-': return nv_font_punct[0];
        case '.': return nv_font_punct[1];
        case ':': return nv_font_punct[2];
        case '?': return nv_font_punct[3];
        case '!': return nv_font_punct[4];
        case '(': return nv_font_punct[5];
        case ')': return nv_font_punct[6];
        default:  return NULL;
    }
}

/* Notices draw at 1x, not the fps read-out's 2x: 320/6 = 53 columns, which is
 * enough to name a PAK. At 2x it would be 26 and every useful message would
 * wrap to three lines. */
#define NV_COLS       52
#define NV_NOTICE_MAX 3

static int      nv_fps_value    = 0;
static uint32_t nv_fps_frames   = 0;
static uint64_t nv_fps_last_ns  = 0;

static uint64_t nv_now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* Recompute about twice a second: fast enough to feel live, slow enough that
 * the digits do not flicker between adjacent values while you read them. */
static void nv_fps_tick(void) {
    uint64_t now = nv_now_ns();
    nv_fps_frames++;
    if (nv_fps_last_ns == 0) { nv_fps_last_ns = now; nv_fps_frames = 0; return; }
    uint64_t dt = now - nv_fps_last_ns;
    if (dt >= 500000000ull) {
        nv_fps_value   = (int)((nv_fps_frames * 1000000000ull + dt / 2) / dt);
        if (nv_fps_value > 999) nv_fps_value = 999;
        nv_fps_frames  = 0;
        nv_fps_last_ns = now;
    }
}

/* 1x, for notice text. */
static void nv_blit_rows1x(volatile uint16_t* dst, int gx, int gy,
                           const uint8_t* rows, uint16_t colour) {
    if (!rows) return;
    for (int ry = 0; ry < 7; ry++) {
        uint8_t bits = rows[ry];
        int py = gy + ry;
        if (py < 0 || py >= NV_FRAME_HEIGHT) continue;
        volatile uint16_t* row = dst + (size_t)py * NV_FRAME_WIDTH;
        for (int rx = 0; rx < 5; rx++) {
            if (!(bits & (0x10 >> rx))) continue;
            int px = gx + rx;
            if (px < 0 || px >= NV_FRAME_WIDTH) continue;
            row[px] = colour;
        }
    }
}

/* ==========================================================================
 * NOTICE OVERLAY
 *
 * Called from the recorder in openbor.c. Every message it emits went through
 * printf, which the engine #defines to writeToLogFile() -> OpenBorLog.txt. So
 * "wrong PAK", "not a valid recording", "recorded on an older build", "you took
 * over" and "playback finished" all presented identically to the player: the
 * PAK reset and then behaved oddly.
 *
 * Held for a few seconds, top-left, so it never collides with the bottom-right
 * fps read-out. Drawn here in WriteFrame, i.e. AFTER the downscale, exactly
 * like the fps overlay -- an engine-space notice would be squished with the
 * game image on a PAK that renders at 960x480.
 *
 * Frame-counted rather than clock-based so it behaves identically under a
 * deterministic replay.
 * ========================================================================== */
static char nv_notice_text[NV_COLS * NV_NOTICE_MAX + 1];
static int  nv_notice_frames = 0;

void NativeVideoWriter_Notice(const char* msg, int seconds) {
    if (!msg) { nv_notice_frames = 0; return; }
    size_t i = 0;
    while (msg[i] && i < sizeof(nv_notice_text) - 1) {
        char c = msg[i];
        nv_notice_text[i] = (c >= 'a' && c <= 'z') ? (char)(c - 32) : c;
        i++;
    }
    nv_notice_text[i] = 0;
    if (seconds <= 0) seconds = 4;
    nv_notice_frames = seconds * 60;   /* ~59.92 Hz; exactness does not matter */
}

/* Word-wrapped, not cropped. A word longer than a line is hard-broken rather
 * than dropped, so a long PAK name still shows something useful. */
static void nv_draw_notice(volatile uint16_t* dst) {
    if (nv_notice_frames <= 0) return;
    nv_notice_frames--;

    const char* p = nv_notice_text;
    int line = 0;
    while (*p && line < NV_NOTICE_MAX) {
        while (*p == ' ') p++;
        if (!*p) break;

        int take = 0, brk = 0;
        while (p[take] && take < NV_COLS) {
            if (p[take] == ' ') brk = take;
            take++;
        }
        if (p[take] && brk > 0) take = brk;

        int y = NV_FPS_MARGIN + line * 9;
        for (int pass = 0; pass < 2; pass++) {
            uint16_t c   = (pass == 0) ? 0x0000 : 0xFFFF;
            int      off = (pass == 0) ? 1 : 0;
            for (int i = 0; i < take; i++)
                nv_blit_rows1x(dst, NV_FPS_MARGIN + i * 6 + off, y + off,
                               nv_glyph_rows(p[i]), c);
        }
        p += take;
        line++;
    }
}

static void nv_blit_glyph(volatile uint16_t* dst, int gx, int gy,
                          int digit, uint16_t colour) {
    const uint8_t* rows = nv_font5x7[digit];
    for (int ry = 0; ry < 7; ry++) {
        uint8_t bits = rows[ry];
        for (int rx = 0; rx < 5; rx++) {
            if (!(bits & (0x10 >> rx))) continue;
            /* 2x scale */
            for (int sy = 0; sy < 2; sy++) {
                int py = gy + ry * 2 + sy;
                if (py < 0 || py >= NV_FRAME_HEIGHT) continue;
                volatile uint16_t* row = dst + (size_t)py * NV_FRAME_WIDTH;
                for (int sx = 0; sx < 2; sx++) {
                    int px = gx + rx * 2 + sx;
                    if (px < 0 || px >= NV_FRAME_WIDTH) continue;
                    row[px] = colour;
                }
            }
        }
    }
}

/* Draw the current fps, bottom-right, colour-coded:
 *   red    0-29    yellow 30-59    green 60+
 * A black copy is laid down one pixel down-right first so the number stays
 * legible over bright or busy backgrounds. */
static void nv_draw_fps(volatile uint16_t* dst) {
    int v = nv_fps_value;
    int digits[3], nd = 0;
    if (v <= 0) { digits[nd++] = 0; }
    else { while (v > 0 && nd < 3) { digits[nd++] = v % 10; v /= 10; } }

    uint16_t colour = (nv_fps_value >= 60) ? 0x07E0        /* green  */
                    : (nv_fps_value >= 30) ? 0xFFE0        /* yellow */
                                           : 0xF800;       /* red    */

    int total_w = nd * NV_GLYPH_W + (nd - 1) * NV_GLYPH_GAP;
    int x0 = NV_FRAME_WIDTH  - NV_FPS_MARGIN - total_w;
    int y0 = NV_FRAME_HEIGHT - NV_FPS_MARGIN - NV_GLYPH_H;

    for (int pass = 0; pass < 2; pass++) {
        uint16_t c  = (pass == 0) ? 0x0000 : colour;
        int      off = (pass == 0) ? 1 : 0;
        for (int i = 0; i < nd; i++) {
            /* digits[] is least-significant first */
            int gx = x0 + (nd - 1 - i) * (NV_GLYPH_W + NV_GLYPH_GAP);
            nv_blit_glyph(dst, gx + off, y0 + off, digits[i], c);
        }
    }
}

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
            } else if (width == NV_FRAME_WIDTH * 3) {
                /* MiSTer Tier-0 (2026-06-15): NEON 3x area-average box for the
                 * 16bpp BGR565 squish (He-Man 960x480 -> 320x224, 3x X). hcnt==3
                 * for every output column; vcnt varies per row. Byte-identical to
                 * the scalar box below: expand each 5/6-bit tap to 8-bit
                 * ((v<<3)|(v>>2) / (v<<2)|(v>>4)), sum, divide by 3*vcnt via the
                 * same recip, pack RGB565. vld3q_u16 deinterleaves the 3 taps. */
                int yy0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
                int yy1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
                if (yy1 <= yy0) yy1 = yy0 + 1;
                if (yy1 > height) yy1 = height;
                if (yy0 >= height) yy0 = height - 1;
                int vcnt = yy1 - yy0;
                const uint8_t* sbase = src + (size_t)yy0 * pitch;
                uint32_t rc = (uint32_t)((1u << 20) / ((uint32_t)3 * (uint32_t)vcnt));

                uint16_t acc_r[NV_FRAME_WIDTH], acc_g[NV_FRAME_WIDTH], acc_b[NV_FRAME_WIDTH];
                memset(acc_r, 0, sizeof(acc_r));
                memset(acc_g, 0, sizeof(acc_g));
                memset(acc_b, 0, sizeof(acc_b));

                const uint16x8_t m5 = vdupq_n_u16(0x001F);
                const uint16x8_t m6 = vdupq_n_u16(0x003F);
                const uint8_t* rowp = sbase;
                for (int syy = 0; syy < vcnt; syy++) {
                    const uint16_t* sp = (const uint16_t*)rowp;
                    for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                        uint16x8x3_t g3 = vld3q_u16(sp + (size_t)x * 3);
                        uint16x8_t hr = vdupq_n_u16(0), hg = vdupq_n_u16(0), hb = vdupq_n_u16(0);
                        for (int t = 0; t < 3; t++) {
                            uint16x8_t p  = g3.val[t];
                            uint16x8_t r5 = vandq_u16(p, m5);
                            uint16x8_t g6 = vandq_u16(vshrq_n_u16(p, 5), m6);
                            uint16x8_t b5 = vandq_u16(vshrq_n_u16(p, 11), m5);
                            hr = vaddq_u16(hr, vorrq_u16(vshlq_n_u16(r5, 3), vshrq_n_u16(r5, 2)));
                            hg = vaddq_u16(hg, vorrq_u16(vshlq_n_u16(g6, 2), vshrq_n_u16(g6, 4)));
                            hb = vaddq_u16(hb, vorrq_u16(vshlq_n_u16(b5, 3), vshrq_n_u16(b5, 2)));
                        }
                        vst1q_u16(&acc_r[x], vaddq_u16(vld1q_u16(&acc_r[x]), hr));
                        vst1q_u16(&acc_g[x], vaddq_u16(vld1q_u16(&acc_g[x]), hg));
                        vst1q_u16(&acc_b[x], vaddq_u16(vld1q_u16(&acc_b[x]), hb));
                    }
                    rowp += pitch;
                }

                const uint32x4_t rc_v  = vdupq_n_u32(rc);
                const uint32x4_t halfv = vdupq_n_u32(1u << 19);
                volatile uint16_t* drow = dst + (size_t)y * NV_FRAME_WIDTH;
                for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                    uint16x8_t ar = vld1q_u16(&acc_r[x]);
                    uint16x8_t ag = vld1q_u16(&acc_g[x]);
                    uint16x8_t ab = vld1q_u16(&acc_b[x]);
                    uint32x4_t rl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ar)),  rc_v), halfv), 20);
                    uint32x4_t rh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ar)), rc_v), halfv), 20);
                    uint32x4_t gl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ag)),  rc_v), halfv), 20);
                    uint32x4_t gh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ag)), rc_v), halfv), 20);
                    uint32x4_t bl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ab)),  rc_v), halfv), 20);
                    uint32x4_t bh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ab)), rc_v), halfv), 20);
                    uint16x8_t r8 = vcombine_u16(vmovn_u32(rl), vmovn_u32(rh));
                    uint16x8_t g8 = vcombine_u16(vmovn_u32(gl), vmovn_u32(gh));
                    uint16x8_t b8 = vcombine_u16(vmovn_u32(bl), vmovn_u32(bh));
                    uint16x8_t out = vorrq_u16(vorrq_u16(
                                        vshlq_n_u16(vshrq_n_u16(r8, 3), 11),
                                        vshlq_n_u16(vshrq_n_u16(g8, 2), 5)),
                                        vshrq_n_u16(b8, 3));
                    vst1q_u16((uint16_t*)(drow + x), out);
                }
            } else if (width == NV_FRAME_WIDTH * 2) {
                /* MiSTer #9 (2026-06-16): NEON 2x area-average box (16bpp BGR565,
                 * 640x.. -> 320x224, 2x X). hcnt==2 for every output column;
                 * byte-identical to the scalar box (same expand / recip / round).
                 * vld2q_u16 deinterleaves the 2 taps per dest pixel. */
                int yy0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
                int yy1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
                if (yy1 <= yy0) yy1 = yy0 + 1;
                if (yy1 > height) yy1 = height;
                if (yy0 >= height) yy0 = height - 1;
                int vcnt = yy1 - yy0;
                const uint8_t* sbase = src + (size_t)yy0 * pitch;
                uint32_t rc = (uint32_t)((1u << 20) / ((uint32_t)2 * (uint32_t)vcnt));

                uint16_t acc_r[NV_FRAME_WIDTH], acc_g[NV_FRAME_WIDTH], acc_b[NV_FRAME_WIDTH];
                memset(acc_r, 0, sizeof(acc_r));
                memset(acc_g, 0, sizeof(acc_g));
                memset(acc_b, 0, sizeof(acc_b));
                const uint16x8_t m5 = vdupq_n_u16(0x001F);
                const uint16x8_t m6 = vdupq_n_u16(0x003F);
                const uint8_t* rowp = sbase;
                for (int syy = 0; syy < vcnt; syy++) {
                    const uint16_t* sp = (const uint16_t*)rowp;
                    for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                        uint16x8x2_t g2 = vld2q_u16(sp + (size_t)x * 2);
                        uint16x8_t hr = vdupq_n_u16(0), hg = vdupq_n_u16(0), hb = vdupq_n_u16(0);
                        for (int t = 0; t < 2; t++) {
                            uint16x8_t p  = g2.val[t];
                            uint16x8_t r5 = vandq_u16(p, m5);
                            uint16x8_t g6 = vandq_u16(vshrq_n_u16(p, 5), m6);
                            uint16x8_t b5 = vandq_u16(vshrq_n_u16(p, 11), m5);
                            hr = vaddq_u16(hr, vorrq_u16(vshlq_n_u16(r5, 3), vshrq_n_u16(r5, 2)));
                            hg = vaddq_u16(hg, vorrq_u16(vshlq_n_u16(g6, 2), vshrq_n_u16(g6, 4)));
                            hb = vaddq_u16(hb, vorrq_u16(vshlq_n_u16(b5, 3), vshrq_n_u16(b5, 2)));
                        }
                        vst1q_u16(&acc_r[x], vaddq_u16(vld1q_u16(&acc_r[x]), hr));
                        vst1q_u16(&acc_g[x], vaddq_u16(vld1q_u16(&acc_g[x]), hg));
                        vst1q_u16(&acc_b[x], vaddq_u16(vld1q_u16(&acc_b[x]), hb));
                    }
                    rowp += pitch;
                }
                const uint32x4_t rc_v  = vdupq_n_u32(rc);
                const uint32x4_t halfv = vdupq_n_u32(1u << 19);
                volatile uint16_t* drow = dst + (size_t)y * NV_FRAME_WIDTH;
                for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                    uint16x8_t ar = vld1q_u16(&acc_r[x]);
                    uint16x8_t ag = vld1q_u16(&acc_g[x]);
                    uint16x8_t ab = vld1q_u16(&acc_b[x]);
                    uint32x4_t rl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ar)),  rc_v), halfv), 20);
                    uint32x4_t rh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ar)), rc_v), halfv), 20);
                    uint32x4_t gl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ag)),  rc_v), halfv), 20);
                    uint32x4_t gh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ag)), rc_v), halfv), 20);
                    uint32x4_t bl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ab)),  rc_v), halfv), 20);
                    uint32x4_t bh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ab)), rc_v), halfv), 20);
                    uint16x8_t r8 = vcombine_u16(vmovn_u32(rl), vmovn_u32(rh));
                    uint16x8_t g8 = vcombine_u16(vmovn_u32(gl), vmovn_u32(gh));
                    uint16x8_t b8 = vcombine_u16(vmovn_u32(bl), vmovn_u32(bh));
                    uint16x8_t out = vorrq_u16(vorrq_u16(
                                        vshlq_n_u16(vshrq_n_u16(r8, 3), 11),
                                        vshlq_n_u16(vshrq_n_u16(g8, 2), 5)),
                                        vshrq_n_u16(b8, 3));
                    vst1q_u16((uint16_t*)(drow + x), out);
                }
            } else if (width * 2 == NV_FRAME_WIDTH * 3) {
                /* MiSTer #9 (2026-06-16): NEON 3:2 area-average box (16bpp BGR565,
                 * 480x.. -> 320x224, 1.5x X). Each dest PAIR (2p,2p+1) spans 3 src
                 * px: dest 2p = src 3p (hcnt=1), dest 2p+1 = src 3p+1 + 3p+2
                 * (hcnt=2) -- exactly the scalar box's x0/x1 for width=480.
                 * vld3q_u16 gives the 3 taps; even/odd accumulate separately (they
                 * have different divisors), vst2q_u16 re-interleaves the pair.
                 * Byte-identical to the scalar box. Processes 16 dest (8 pairs)/iter. */
                int yy0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
                int yy1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
                if (yy1 <= yy0) yy1 = yy0 + 1;
                if (yy1 > height) yy1 = height;
                if (yy0 >= height) yy0 = height - 1;
                int vcnt = yy1 - yy0;
                const uint8_t* sbase = src + (size_t)yy0 * pitch;
                uint32_t rc_e = (uint32_t)((1u << 20) / ((uint32_t)1 * (uint32_t)vcnt)); /* hcnt=1 */
                uint32_t rc_o = (uint32_t)((1u << 20) / ((uint32_t)2 * (uint32_t)vcnt)); /* hcnt=2 */

                /* 160 pairs = NV_FRAME_WIDTH/2; even/odd dest accumulated apart. */
                uint16_t er[NV_FRAME_WIDTH/2], eg[NV_FRAME_WIDTH/2], eb[NV_FRAME_WIDTH/2];
                uint16_t orr[NV_FRAME_WIDTH/2], og[NV_FRAME_WIDTH/2], ob[NV_FRAME_WIDTH/2];
                memset(er,0,sizeof(er)); memset(eg,0,sizeof(eg)); memset(eb,0,sizeof(eb));
                memset(orr,0,sizeof(orr)); memset(og,0,sizeof(og)); memset(ob,0,sizeof(ob));
                const uint16x8_t m5 = vdupq_n_u16(0x001F);
                const uint16x8_t m6 = vdupq_n_u16(0x003F);
                const uint8_t* rowp = sbase;
                for (int syy = 0; syy < vcnt; syy++) {
                    const uint16_t* sp = (const uint16_t*)rowp;
                    for (int x = 0; x < NV_FRAME_WIDTH; x += 16) {
                        int p = x >> 1;                       /* pair base index */
                        uint16x8x3_t g3 = vld3q_u16(sp + (size_t)(x * 3) / 2);
                        /* even tap = val[0]; odd taps = val[1] + val[2] */
                        uint16x8_t e_r5 = vandq_u16(g3.val[0], m5);
                        uint16x8_t e_g6 = vandq_u16(vshrq_n_u16(g3.val[0], 5), m6);
                        uint16x8_t e_b5 = vandq_u16(vshrq_n_u16(g3.val[0], 11), m5);
                        uint16x8_t hr_e = vorrq_u16(vshlq_n_u16(e_r5,3), vshrq_n_u16(e_r5,2));
                        uint16x8_t hg_e = vorrq_u16(vshlq_n_u16(e_g6,2), vshrq_n_u16(e_g6,4));
                        uint16x8_t hb_e = vorrq_u16(vshlq_n_u16(e_b5,3), vshrq_n_u16(e_b5,2));
                        uint16x8_t hr_o = vdupq_n_u16(0), hg_o = vdupq_n_u16(0), hb_o = vdupq_n_u16(0);
                        for (int t = 1; t < 3; t++) {
                            uint16x8_t pp = g3.val[t];
                            uint16x8_t r5 = vandq_u16(pp, m5);
                            uint16x8_t g6 = vandq_u16(vshrq_n_u16(pp,5), m6);
                            uint16x8_t b5 = vandq_u16(vshrq_n_u16(pp,11), m5);
                            hr_o = vaddq_u16(hr_o, vorrq_u16(vshlq_n_u16(r5,3), vshrq_n_u16(r5,2)));
                            hg_o = vaddq_u16(hg_o, vorrq_u16(vshlq_n_u16(g6,2), vshrq_n_u16(g6,4)));
                            hb_o = vaddq_u16(hb_o, vorrq_u16(vshlq_n_u16(b5,3), vshrq_n_u16(b5,2)));
                        }
                        vst1q_u16(&er[p], vaddq_u16(vld1q_u16(&er[p]), hr_e));
                        vst1q_u16(&eg[p], vaddq_u16(vld1q_u16(&eg[p]), hg_e));
                        vst1q_u16(&eb[p], vaddq_u16(vld1q_u16(&eb[p]), hb_e));
                        vst1q_u16(&orr[p], vaddq_u16(vld1q_u16(&orr[p]), hr_o));
                        vst1q_u16(&og[p], vaddq_u16(vld1q_u16(&og[p]), hg_o));
                        vst1q_u16(&ob[p], vaddq_u16(vld1q_u16(&ob[p]), hb_o));
                    }
                    rowp += pitch;
                }
                const uint32x4_t halfv = vdupq_n_u32(1u << 19);
                const uint32x4_t rce = vdupq_n_u32(rc_e), rco = vdupq_n_u32(rc_o);
                volatile uint16_t* drow = dst + (size_t)y * NV_FRAME_WIDTH;
                for (int x = 0; x < NV_FRAME_WIDTH; x += 16) {
                    int p = x >> 1;
                    /* helper: divide one acc-vector by rc and pack the 5/6-bit field shift */
                    #define DIV8(acc, rcv) ({ \
                        uint16x8_t _a = vld1q_u16(acc); \
                        uint32x4_t _l = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(_a)),  rcv), halfv), 20); \
                        uint32x4_t _h = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(_a)), rcv), halfv), 20); \
                        vcombine_u16(vmovn_u32(_l), vmovn_u32(_h)); })
                    uint16x8_t er8 = DIV8(&er[p], rce), eg8 = DIV8(&eg[p], rce), eb8 = DIV8(&eb[p], rce);
                    uint16x8_t or8 = DIV8(&orr[p], rco), og8 = DIV8(&og[p], rco), ob8 = DIV8(&ob[p], rco);
                    #undef DIV8
                    uint16x8x2_t out;
                    out.val[0] = vorrq_u16(vorrq_u16(vshlq_n_u16(vshrq_n_u16(er8,3),11),
                                                     vshlq_n_u16(vshrq_n_u16(eg8,2),5)),
                                                     vshrq_n_u16(eb8,3));
                    out.val[1] = vorrq_u16(vorrq_u16(vshlq_n_u16(vshrq_n_u16(or8,3),11),
                                                     vshlq_n_u16(vshrq_n_u16(og8,2),5)),
                                                     vshrq_n_u16(ob8,3));
                    vst2q_u16((uint16_t*)(drow + x), out);
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

        /* Raw 2D block-sum accumulators; one divide per output pixel. uint32
         * is ample: block_sum <= block_area * 255 (~7650 at 1080p source). */
        uint32_t vr[NV_FRAME_WIDTH], vg[NV_FRAME_WIDTH], vb[NV_FRAME_WIDTH];

        /* Averaged 8-bit RGB intermediate. Pass 1 fills it from the box
         * average; pass 2 reads 3x3 neighborhoods for the unsharp + packs to
         * DDR3. static (render-thread only). ~215 KB. */
        static uint8_t s_avg[NV_FRAME_HEIGHT * NV_FRAME_WIDTH * 3];

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
                        vst1q_u32(&vr[x],      vaddq_u32(vld1q_u32(&vr[x]),      vmovl_u16(vget_low_u16(rlo))));
                        vst1q_u32(&vr[x + 4],  vaddq_u32(vld1q_u32(&vr[x + 4]),  vmovl_u16(vget_high_u16(rlo))));
                        vst1q_u32(&vr[x + 8],  vaddq_u32(vld1q_u32(&vr[x + 8]),  vmovl_u16(vget_low_u16(rhi))));
                        vst1q_u32(&vr[x + 12], vaddq_u32(vld1q_u32(&vr[x + 12]), vmovl_u16(vget_high_u16(rhi))));
                        vst1q_u32(&vg[x],      vaddq_u32(vld1q_u32(&vg[x]),      vmovl_u16(vget_low_u16(glo))));
                        vst1q_u32(&vg[x + 4],  vaddq_u32(vld1q_u32(&vg[x + 4]),  vmovl_u16(vget_high_u16(glo))));
                        vst1q_u32(&vg[x + 8],  vaddq_u32(vld1q_u32(&vg[x + 8]),  vmovl_u16(vget_low_u16(ghi))));
                        vst1q_u32(&vg[x + 12], vaddq_u32(vld1q_u32(&vg[x + 12]), vmovl_u16(vget_high_u16(ghi))));
                        vst1q_u32(&vb[x],      vaddq_u32(vld1q_u32(&vb[x]),      vmovl_u16(vget_low_u16(blo))));
                        vst1q_u32(&vb[x + 4],  vaddq_u32(vld1q_u32(&vb[x + 4]),  vmovl_u16(vget_high_u16(blo))));
                        vst1q_u32(&vb[x + 8],  vaddq_u32(vld1q_u32(&vb[x + 8]),  vmovl_u16(vget_low_u16(bhi))));
                        vst1q_u32(&vb[x + 12], vaddq_u32(vld1q_u32(&vb[x + 12]), vmovl_u16(vget_high_u16(bhi))));
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
    /* FPS overlay: after every pixel path, before the publish barrier, so it
     * lands in the frame the FPGA is about to scan out. */
    nv_fps_tick();
    if (mister_fps_overlay) nv_draw_fps(dst);
    /* After the fps read-out so a notice is never painted over by it. Ungated:
     * a notice only appears when there is something the player must know. */
    nv_draw_notice(dst);

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
