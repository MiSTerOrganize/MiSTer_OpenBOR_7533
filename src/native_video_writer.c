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

#include "native_video_writer.h"

#include <fcntl.h>
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
                /* Scalar fallback with uint64_t-packed writes (4 px per
                 * store). Handles squish via src_x_table; same bit-exact
                 * output as the original scalar loop. */
                for (int x = 0; x < NV_FRAME_WIDTH; x += 4) {
                    uint16_t p0 = src_row[src_x_table[x + 0]];
                    uint16_t p1 = src_row[src_x_table[x + 1]];
                    uint16_t p2 = src_row[src_x_table[x + 2]];
                    uint16_t p3 = src_row[src_x_table[x + 3]];
                    p0 = ((p0 & 0x001F) << 11) | (p0 & 0x07E0) | ((p0 & 0xF800) >> 11);
                    p1 = ((p1 & 0x001F) << 11) | (p1 & 0x07E0) | ((p1 & 0xF800) >> 11);
                    p2 = ((p2 & 0x001F) << 11) | (p2 & 0x07E0) | ((p2 & 0xF800) >> 11);
                    p3 = ((p3 & 0x001F) << 11) | (p3 & 0x07E0) | ((p3 & 0xF800) >> 11);
                    uint64_t packed = ((uint64_t)p0) | ((uint64_t)p1 << 16)
                                    | ((uint64_t)p2 << 32) | ((uint64_t)p3 << 48);
                    *(volatile uint64_t*)(dst_row + x) = packed;
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
        /* 32bpp RGBA — byte-0=R, byte-1=G, byte-2=B, byte-3=A. */
        const uint8_t* src = (const uint8_t*)pixels;
        for (int y = 0; y < NV_FRAME_HEIGHT; y++) {
            int src_y = (y * sy256) / 256;
            if (src_y >= height) src_y = height - 1;
            const uint8_t* row = src + src_y * pitch;
            volatile uint16_t* dst_row = dst + y * NV_FRAME_WIDTH;
            /* Step 20: uint64_t-packed writes (4 px per store). Per-pixel
             * RGBA-to-RGB565 conversion stays scalar; the win is in the
             * DDR3 write width. */
            for (int x = 0; x < NV_FRAME_WIDTH; x += 4) {
                uint16_t out[4];
                for (int k = 0; k < 4; k++) {
                    int i = src_x_table[x + k] * 4;
                    uint8_t r = row[i + 0];
                    uint8_t g = row[i + 1];
                    uint8_t b = row[i + 2];
                    out[k] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
                }
                uint64_t packed = ((uint64_t)out[0]) | ((uint64_t)out[1] << 16)
                                | ((uint64_t)out[2] << 32) | ((uint64_t)out[3] << 48);
                *(volatile uint64_t*)(dst_row + x) = packed;
            }
        }
    }
    else {
        return;  /* unsupported format, skip frame */
    }

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
