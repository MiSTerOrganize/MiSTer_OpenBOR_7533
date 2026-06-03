//
//  Native Video DDR3 Writer — OpenBOR MiSTer
//
//  STEP 60 / Option Y (2026-06-01): variable-res frame writes.
//  ARM writes native source-res RGB565 frames to DDR3 + a DIM ctrl word.
//  FPGA reads dimensions from DIM and performs edge-aware downscale-to-
//  display in hardware (Step 60 RTL). Eliminates ARM-side wrapper squish.
//
//  DDR3 Memory Map (must match openbor_video_reader.sv):
//    0x3A000000 + 0x000     : CTRL  (frame_counter[31:2] | active_buf[1:0])
//    0x3A000000 + 0x004     : DIM   (height[31:16] | width[15:0])   <-- NEW
//    0x3A000000 + 0x008     : Joystick P1 (32 bits)
//    0x3A000000 + 0x010     : Cart control (file_size from FPGA)
//    0x3A000000 + 0x018     : Joystick P2 (32 bits)
//    0x3A000000 + 0x020     : Joystick P3 (32 bits)
//    0x3A000000 + 0x028     : Joystick P4 (32 bits)
//    0x3A000000 + 0x030     : Audio ring write pointer (ARM writes)
//    0x3A000000 + 0x038     : Audio ring read pointer  (FPGA writes)
//    0x3A000000 + 0x040     : Buffer 0  (up to 1920×1080×2 = 4,147,200 bytes)
//    0x3A000000 + 0x400040  : Buffer 1  (up to 1920×1080×2 bytes)
//    0x3A000000 + 0x800040  : Cart data (PAK file from OSD; 1MB region)
//    0x3A000000 + 0x900040  : Audio ring (64 KiB)
//
//  Buffer alignment: each buffer is 4MB-rounded (0x400000) for clean
//  qword addressing. Actual frame data may be smaller (e.g., 320×240
//  fills first 153,600 bytes of buf, rest untouched). FPGA reads only
//  width×height pixels per buf, driven by DIM ctrl word.
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
/* NEON intrinsics for 128-bit DDR3 stores in the 16bpp fast path.
 * Cortex-A9 + -mfpu=neon -mfloat-abi=hard guarantee NEON support. */
#include <arm_neon.h>

#define NV_DDR_PHYS_BASE     0x3A000000u
#define NV_DDR_REGION_SIZE   0x01000000u   /* 16MB: covers two 4MB buffers + cart + audio ring */
#define NV_CTRL_OFFSET       0x00000000u
#define NV_DIM_OFFSET        0x00000004u   /* NEW: per-frame width/height */
#define NV_JOY0_OFFSET       0x00000008u
#define NV_CART_CTRL_OFFSET  0x00000010u
#define NV_JOY1_OFFSET       0x00000018u
#define NV_JOY2_OFFSET       0x00000020u
#define NV_JOY3_OFFSET       0x00000028u
#define NV_AUDIO_WR_OFFSET   0x00000030u
#define NV_AUDIO_RD_OFFSET   0x00000038u
#define NV_BUF0_OFFSET       0x00000040u
#define NV_BUF1_OFFSET       0x00400040u   /* MOVED: was 0x40040, now 4MB-stride */
#define NV_CART_DATA_OFFSET  0x00800040u   /* MOVED: was 0x80000 */
#define NV_CART_MAX_SIZE     0x00100000u   /* 1MB max PAK via OSD */
#define NV_AUDIO_RING_OFFSET 0x00900040u   /* MOVED: was 0xD0000 */
#define NV_AUDIO_RING_SIZE   0x00010000u   /* 64 KiB, unchanged */

/* Per-buffer max byte size = 1920 × 1080 × 2 = 4,147,200. Rounded up to
 * 4MB (0x400000) per buffer for clean addressing. */
#define NV_BUF_STRIDE_BYTES  0x00400000u

static const uint32_t joy_offsets[4] = {
    NV_JOY0_OFFSET, NV_JOY1_OFFSET, NV_JOY2_OFFSET, NV_JOY3_OFFSET
};

static int mem_fd = -1;
static volatile uint8_t* ddr_base = NULL;
static uint32_t frame_counter = 0;
static int active_buf = 0;
static uint16_t last_width  = NV_TARGET_WIDTH;   /* tracks most recent frame dims for DIM word */
static uint16_t last_height = NV_TARGET_HEIGHT;

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

    /* Clear control words + per-player joystick offsets. Per the universal
     * hybrid-core rule: cart's frame-0 reads stale DDR3 from previous core
     * if Init doesn't zero everything the engine polls. Buffer regions are
     * zeroed only for the first NV_TARGET_WIDTH × NV_TARGET_HEIGHT × 2
     * bytes (first frame's worth) — too expensive to zero 4MB each. */
    volatile uint32_t* ctrl     = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
    volatile uint32_t* dim      = (volatile uint32_t*)(ddr_base + NV_DIM_OFFSET);
    volatile uint32_t* cart_ctrl = (volatile uint32_t*)(ddr_base + NV_CART_CTRL_OFFSET);
    *ctrl = 0;
    *dim = ((uint32_t)NV_TARGET_HEIGHT << 16) | (uint32_t)NV_TARGET_WIDTH;
    *cart_ctrl = 0;
    for (int i = 0; i < 4; i++) {
        *(volatile uint32_t*)(ddr_base + joy_offsets[i]) = 0;
    }
    /* Zero just the first display-target frame area in each buffer. Avoids
     * 4MB×2 = 8MB zeroing cost while still presenting clean black to FPGA
     * if it reads before first WriteFrame. */
    size_t init_clear = (size_t)NV_TARGET_WIDTH * (size_t)NV_TARGET_HEIGHT * 2u;
    memset((void*)(ddr_base + NV_BUF0_OFFSET), 0, init_clear);
    memset((void*)(ddr_base + NV_BUF1_OFFSET), 0, init_clear);

    frame_counter = 0;
    active_buf = 0;
    last_width  = NV_TARGET_WIDTH;
    last_height = NV_TARGET_HEIGHT;

    fprintf(stderr, "NativeVideoWriter: mapped 0x%08X region=%uMB, max %dx%d/frame (Option Y)\n",
            NV_DDR_PHYS_BASE, NV_DDR_REGION_SIZE >> 20, NV_MAX_WIDTH, NV_MAX_HEIGHT);
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

    /* Step 60: clip to engine max instead of squish. Native dimensions
     * carried in DIM ctrl word; FPGA downscales to display. */
    if (width  > NV_MAX_WIDTH)  width  = NV_MAX_WIDTH;
    if (height > NV_MAX_HEIGHT) height = NV_MAX_HEIGHT;

    uint32_t buf_offset = (active_buf == 0) ? NV_BUF0_OFFSET : NV_BUF1_OFFSET;
    volatile uint16_t* dst = (volatile uint16_t*)(ddr_base + buf_offset);
    /* Destination stride in 16-bit pixels = source width. Each row in
     * DDR3 is laid out tightly at the engine's native res; FPGA reader
     * uses width from DIM ctrl word to compute per-line address. */
    const int dst_stride = width;

    if (bpp == 16) {
        /* OpenBOR's 16bpp surfaces are BGR565 (B in high bits). The FPGA
         * decoder expects RGB565. Swap R and B 5-bit fields per pixel.
         * Native-res direct copy — no squish loop. NEON 8-pixel vectorized
         * path when row pointer is 16-byte aligned. */
        const uint8_t* src = (const uint8_t*)pixels;
        const uint16x8_t mask_r = vdupq_n_u16(0x001F);
        const uint16x8_t mask_g = vdupq_n_u16(0x07E0);
        const uint16x8_t mask_b = vdupq_n_u16(0xF800);
        for (int y = 0; y < height; y++) {
            const uint16_t* src_row = (const uint16_t*)(src + (size_t)y * pitch);
            volatile uint16_t* dst_row = dst + (size_t)y * dst_stride;
            int x = 0;
            /* NEON fast path when source is 16-byte aligned */
            if (((uintptr_t)src_row & 15) == 0) {
                int neon_end = width & ~7;  /* round down to multiple of 8 */
                for (; x < neon_end; x += 8) {
                    uint16x8_t px = vld1q_u16(src_row + x);
                    uint16x8_t r = vandq_u16(px, mask_r);
                    uint16x8_t g = vandq_u16(px, mask_g);
                    uint16x8_t b = vandq_u16(px, mask_b);
                    uint16x8_t r_shifted = vshlq_n_u16(r, 11);
                    uint16x8_t b_shifted = vshrq_n_u16(b, 11);
                    uint16x8_t out = vorrq_u16(vorrq_u16(r_shifted, g), b_shifted);
                    vst1q_u16((uint16_t*)(dst_row + x), out);
                }
            }
            /* Scalar tail for unaligned rows OR width not multiple of 8 */
            for (; x < width; x++) {
                uint16_t p = src_row[x];
                uint16_t r = (p & 0x001F) << 11;
                uint16_t g = (p & 0x07E0);
                uint16_t b = (p & 0xF800) >> 11;
                dst_row[x] = r | g | b;
            }
        }
    }
    else if (bpp == 8 && palette) {
        /* 8bpp paletted — convert through palette to RGB565.
         * OpenBOR s_screen palette: 3 bytes per entry (R, G, B), 256 entries. */
        const uint8_t* src = (const uint8_t*)pixels;
        const uint8_t* pal = (const uint8_t*)palette;
        for (int y = 0; y < height; y++) {
            const uint8_t* row = src + (size_t)y * pitch;
            volatile uint16_t* dst_row = dst + (size_t)y * dst_stride;
            int x = 0;
            /* uint64_t-packed writes (4 px per store) when width is
             * multiple of 4. ~1.5-2× DDR3 write-side speedup vs scalar. */
            int packed_end = width & ~3;
            for (; x < packed_end; x += 4) {
                uint16_t out[4];
                for (int k = 0; k < 4; k++) {
                    uint8_t idx = row[x + k];
                    uint8_t r = pal[idx * 3 + 0];
                    uint8_t g = pal[idx * 3 + 1];
                    uint8_t b = pal[idx * 3 + 2];
                    out[k] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
                }
                uint64_t packed = ((uint64_t)out[0]) | ((uint64_t)out[1] << 16)
                                | ((uint64_t)out[2] << 32) | ((uint64_t)out[3] << 48);
                *(volatile uint64_t*)(dst_row + x) = packed;
            }
            /* Scalar tail */
            for (; x < width; x++) {
                uint8_t idx = row[x];
                uint8_t r = pal[idx * 3 + 0];
                uint8_t g = pal[idx * 3 + 1];
                uint8_t b = pal[idx * 3 + 2];
                dst_row[x] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
            }
        }
    }
    else if (bpp == 32) {
        /* 32bpp RGBA — byte-0=R, byte-1=G, byte-2=B, byte-3=A. */
        const uint8_t* src = (const uint8_t*)pixels;
        for (int y = 0; y < height; y++) {
            const uint8_t* row = src + (size_t)y * pitch;
            volatile uint16_t* dst_row = dst + (size_t)y * dst_stride;
            int x = 0;
            int packed_end = width & ~3;
            for (; x < packed_end; x += 4) {
                uint16_t out[4];
                for (int k = 0; k < 4; k++) {
                    int i = (x + k) * 4;
                    uint8_t r = row[i + 0];
                    uint8_t g = row[i + 1];
                    uint8_t b = row[i + 2];
                    out[k] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
                }
                uint64_t packed = ((uint64_t)out[0]) | ((uint64_t)out[1] << 16)
                                | ((uint64_t)out[2] << 32) | ((uint64_t)out[3] << 48);
                *(volatile uint64_t*)(dst_row + x) = packed;
            }
            /* Scalar tail */
            for (; x < width; x++) {
                int i = x * 4;
                uint8_t r = row[i + 0];
                uint8_t g = row[i + 1];
                uint8_t b = row[i + 2];
                dst_row[x] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
            }
        }
    }
    else {
        return;  /* unsupported format, skip frame */
    }

    /* Defensive barrier: ensure ALL pixel writes drain to DDR3 BEFORE the
     * FPGA sees the new ctrl word and starts reading the buffer we just
     * finished writing. The double-buffer flip protects against most
     * tearing (FPGA reads OPPOSITE buffer from the one we write), but
     * NEON/uint64_t stores can drain through the write-combine buffer at
     * a different rate than scalar stores. If ctrl is updated before the
     * buffer fully drains AND the FPGA pipeline races ahead, the very
     * first lines of the new frame could read partially-written pixels.
     * __sync_synchronize() generates ARMv7 DMB SY (~2 cycle cost). */
    __sync_synchronize();

    /* Write DIM word (width/height) BEFORE flipping CTRL so the FPGA sees
     * dimensions consistent with the new active buffer. */
    last_width  = (uint16_t)width;
    last_height = (uint16_t)height;
    volatile uint32_t* dim = (volatile uint32_t*)(ddr_base + NV_DIM_OFFSET);
    *dim = ((uint32_t)last_height << 16) | (uint32_t)last_width;

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
    /* Re-emit current DIM (no change — FPGA needs consistent dimensions). */
    volatile uint32_t* dim = (volatile uint32_t*)(ddr_base + NV_DIM_OFFSET);
    *dim = ((uint32_t)last_height << 16) | (uint32_t)last_width;
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
