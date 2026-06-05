//
//  Native Video DDR3 Writer — OpenBOR MiSTer (Option Y — Phase 2)
//
//  Writes source-NATIVE-resolution RGB565 frames to DDR3 at 0x3A000000.
//  FPGA-side reader + edge-aware downscale module handles the W×H → 320×224
//  squish per docs/dev/option_y_phase1_architecture.md.
//
//  Source dimensions up to 1920×1080×16bpp (4MB per buffer). Per-frame
//  CTRL+DIM 64-bit atomic write carries width+height in the DIM word so the
//  FPGA reader knows how many pixels per row to read.
//
//  DDR3 Memory Map (must match openbor_video_reader.sv):
//    0x3A000000 + 0x000      : CTRL  (32-bit, [0:1]=active_buf, [2:31]=frame_counter)
//    0x3A000000 + 0x004      : DIM   (32-bit, [10:0]=width, [21:11]=height)
//    0x3A000000 + 0x008      : Joystick P1 (32 bits)
//    0x3A000000 + 0x010      : Cart control (file_size from FPGA)
//    0x3A000000 + 0x018      : Joystick P2 (32 bits)
//    0x3A000000 + 0x020      : Joystick P3 (32 bits)
//    0x3A000000 + 0x028      : Joystick P4 (32 bits)
//    0x3A000000 + 0x030      : Audio ring write pointer
//    0x3A000000 + 0x038      : Audio ring read pointer
//    0x3A000000 + 0x040      : Buffer 0 (up to 1920×1080×2 = ~4 MB)
//    0x3A000000 + 0x400000   : Buffer 1 (4MB aligned)
//    0x3A000000 + 0x800000   : Cart data (PAK file from OSD)
//    0x3A000000 + 0x880000   : Audio ring buffer (64 KiB)
//
//  Total mmap region: 16 MB (8MB buffers + 512KB cart + 64KB audio + headroom).
//
//  Source row stride in DDR3 = width × 2 bytes (RGB565 packed, no padding).
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

#define NV_DDR_PHYS_BASE     0x3A000000u
#define NV_DDR_REGION_SIZE   0x01000000u   /* 16 MB covers 8MB buffers + cart + audio + headroom */
#define NV_CTRL_OFFSET       0x00000000u
#define NV_DIM_OFFSET        0x00000004u   /* Option Y: per-frame source dimensions */
#define NV_JOY0_OFFSET       0x00000008u
#define NV_CART_CTRL_OFFSET  0x00000010u
#define NV_JOY1_OFFSET       0x00000018u
#define NV_JOY2_OFFSET       0x00000020u
#define NV_JOY3_OFFSET       0x00000028u
#define NV_BUF0_OFFSET       0x00000040u   /* row-major source pixels at native W×H */
#define NV_BUF1_OFFSET       0x00400000u   /* 4MB-aligned per Option Y design */
#define NV_CART_DATA_OFFSET  0x00800000u
#define NV_CART_MAX_SIZE     0x00040000u   /* 256KB max PAK size via OSD */

/* Option Y: max source dimensions FPGA reader/downscale supports.
 * 1920×1080 future-proofs against any HD-authored PAK (Lust Rush et al). */
#define NV_MAX_SRC_WIDTH     1920
#define NV_MAX_SRC_HEIGHT    1080
#define NV_MAX_BUF_BYTES     (NV_MAX_SRC_WIDTH * NV_MAX_SRC_HEIGHT * 2)  /* 4,147,200 */

/* Legacy DEST dims preserved for any code path that needs them. The FPGA
 * downscale module handles WxH → 320×224 internally; these are NOT used
 * by WriteFrame anymore (source written at NATIVE resolution). */
#define NV_FRAME_WIDTH       320
#define NV_FRAME_HEIGHT      224   /* Sega CD V28 NTSC */

static const uint32_t joy_offsets[4] = {
    NV_JOY0_OFFSET, NV_JOY1_OFFSET, NV_JOY2_OFFSET, NV_JOY3_OFFSET
};

static int mem_fd = -1;
static volatile uint8_t* ddr_base = NULL;
static uint32_t frame_counter = 0;
static int active_buf = 0;
/* Option Y: last source dimensions written. KeepaliveTick re-emits these
 * along with the bumped frame counter so the FPGA reader's latched DIM
 * stays valid during idle (e.g., wait-for-PAK window). */
static uint32_t cur_src_width  = 320;
static uint32_t cur_src_height = 224;

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

    /* Clear both buffers (full max-sized regions so any leftover bytes
     * from a previous core's PAK swap can't render as garbage during the
     * first frame). Cart's frame-0 reads stale DDR3 from previous core if
     * Init doesn't zero everything the engine polls. Per the universal
     * hybrid-core rule. */
    memset((void*)(ddr_base + NV_BUF0_OFFSET), 0, NV_MAX_BUF_BYTES);
    memset((void*)(ddr_base + NV_BUF1_OFFSET), 0, NV_MAX_BUF_BYTES);
    /* Atomic CTRL+DIM init: CTRL=0 (frame_counter=0, active_buf=0);
     * DIM=default 320x224 so the FPGA reader has SOMETHING valid until
     * the first WriteFrame supplies real dimensions. */
    volatile uint64_t* ctrl_dim = (volatile uint64_t*)(ddr_base + NV_CTRL_OFFSET);
    uint32_t dim_init = ((uint32_t)NV_FRAME_HEIGHT << 11) | (uint32_t)NV_FRAME_WIDTH;
    *ctrl_dim = ((uint64_t)dim_init << 32) | 0u;
    volatile uint32_t* cart_ctrl = (volatile uint32_t*)(ddr_base + NV_CART_CTRL_OFFSET);
    *cart_ctrl = 0;
    for (int i = 0; i < 4; i++) {
        *(volatile uint32_t*)(ddr_base + joy_offsets[i]) = 0;
    }
    frame_counter  = 0;
    active_buf     = 0;
    cur_src_width  = NV_FRAME_WIDTH;
    cur_src_height = NV_FRAME_HEIGHT;

    fprintf(stderr, "NativeVideoWriter: mapped 0x%08X region=%uMB, max %dx%d/frame (Option Y)\n",
            NV_DDR_PHYS_BASE, NV_DDR_REGION_SIZE >> 20,
            NV_MAX_SRC_WIDTH, NV_MAX_SRC_HEIGHT);
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

    /* Option Y (Phase 2 2026-06-05): write source pixels at NATIVE resolution
     * to DDR3. NO software squish — FPGA edge-aware downscale module handles
     * W×H → 320×224. Saves ~3ms ARM time per frame on ATOV (320×240 → 320×224
     * squish removed); future-proofs for HD-authored PAKs (Lust Rush 1920×1080).
     *
     * Source row stride in DDR3 = width × 2 bytes (RGB565 tightly packed).
     * The reader reads `width` pixels per row, advancing `width × 2` bytes per
     * row. DIM word (offset 0x04) tells reader the W×H to expect. */
    if (width  > NV_MAX_SRC_WIDTH)  width  = NV_MAX_SRC_WIDTH;
    if (height > NV_MAX_SRC_HEIGHT) height = NV_MAX_SRC_HEIGHT;

    uint32_t buf_offset = (active_buf == 0) ? NV_BUF0_OFFSET : NV_BUF1_OFFSET;
    volatile uint16_t* dst = (volatile uint16_t*)(ddr_base + buf_offset);
    int dst_stride_px = width;   /* uint16_t elements per row (1:1, no padding) */

    if (bpp == 16) {
        /* OpenBOR's 16bpp surfaces are BGR565 (B in high bits). The FPGA
         * decoder expects RGB565. Swap R and B 5-bit fields per pixel. */
        const uint8_t* src = (const uint8_t*)pixels;
        for (int y = 0; y < height; y++) {
            const uint16_t* src_row = (const uint16_t*)(src + y * pitch);
            volatile uint16_t* dst_row = dst + y * dst_stride_px;

            /* NEON fast path — 16-byte aligned + width multiple of 8. Most
             * engine surfaces hit this path (320, 480, 640, 960, 1920 all
             * %8==0; OpenBOR uses 16-byte aligned malloc). 8 pixels per
             * iteration with BGR565→RGB565 swap. */
            if (((uintptr_t)src_row & 15) == 0 && (width & 7) == 0) {
                const uint16x8_t mask_r = vdupq_n_u16(0x001F);
                const uint16x8_t mask_g = vdupq_n_u16(0x07E0);
                const uint16x8_t mask_b = vdupq_n_u16(0xF800);
                int x;
                for (x = 0; x < width; x += 8) {
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
                /* Scalar fallback for unaligned or non-multiple-of-8 widths
                 * (rare — most engine surfaces are aligned). Same BGR565 →
                 * RGB565 swap, one pixel at a time. */
                for (int x = 0; x < width; x++) {
                    uint16_t p = src_row[x];
                    uint16_t r = (p & 0x001F) << 11;
                    uint16_t g = (p & 0x07E0);
                    uint16_t b = (p & 0xF800) >> 11;
                    dst_row[x] = r | g | b;
                }
            }
        }
    }
    else if (bpp == 8 && palette) {
        /* 8bpp paletted — convert through palette to RGB565.
         * OpenBOR s_screen palette: 3 bytes per entry (R, G, B), 256 entries. */
        const uint8_t* src = (const uint8_t*)pixels;
        const uint8_t* pal = (const uint8_t*)palette;
        for (int y = 0; y < height; y++) {
            const uint8_t* row = src + y * pitch;
            volatile uint16_t* dst_row = dst + y * dst_stride_px;
            /* uint64_t-packed writes (4 px per store) when width is multiple
             * of 4. Palette gather defeats NEON; uint64_t packing alone gives
             * ~1.5-2x DDR3 write-side speedup vs scalar 16-bit stores. */
            int x = 0;
            int wm4 = width & ~3;
            for (; x < wm4; x += 4) {
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
            /* Scalar tail for width not multiple of 4. */
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
            const uint8_t* row = src + y * pitch;
            volatile uint16_t* dst_row = dst + y * dst_stride_px;
            /* uint64_t-packed writes (4 px per store) when width is multiple
             * of 4. Per-pixel scalar conversion; the win is DDR3 write width. */
            int x = 0;
            int wm4 = width & ~3;
            for (; x < wm4; x += 4) {
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
            /* Scalar tail for width not multiple of 4. */
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

    /* Memory barrier: ensure ALL pixel writes (scalar, uint64_t-packed, OR
     * NEON 128-bit) drain to DDR3 BEFORE the FPGA sees the new ctrl+dim
     * word and starts reading the buffer we just finished writing. Without
     * this, the write-combine buffer can drain pixel data AFTER the ctrl
     * update propagates, causing the FPGA reader to fetch partially-written
     * rows on the first lines of the new frame. __sync_synchronize() =
     * ARMv7 DMB SY (full memory barrier); ~2 cycles, negligible. */
    __sync_synchronize();

    /* Option Y (Phase 2): atomic 64-bit CTRL+DIM write per
     * docs/dev/option_y_phase1_architecture.md §5-6. CTRL changes
     * frame_counter (triggers FPGA reader to start new frame); DIM word
     * carries source W×H so the FPGA reader knows row stride + pixels-per-row.
     * Single 64-bit store guarantees both halves arrive together at the
     * reader's CDC — no possibility of seeing new frame_counter with stale
     * DIM (which was Step 60 Phase 5 Bug B class). */
    frame_counter++;
    cur_src_width  = (uint32_t)width;
    cur_src_height = (uint32_t)height;
    uint32_t ctrl_val = (frame_counter << 2) | (active_buf & 1);
    uint32_t dim_val  = (cur_src_height << 11) | cur_src_width;
    *(volatile uint64_t*)(ddr_base + NV_CTRL_OFFSET) =
        ((uint64_t)dim_val << 32) | (uint64_t)ctrl_val;
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
     * active_buf state, racing with WriteFrame's state).
     *
     * Option Y (Phase 2 2026-06-05): atomic 64-bit CTRL+DIM write —
     * preserves the last DIM (cur_src_width/height) so the FPGA reader's
     * latched dimensions stay valid during idle ticks. */
    if (!ddr_base) return;
    frame_counter++;
    int last_written = (!active_buf) & 1;
    uint32_t ctrl_val = (frame_counter << 2) | last_written;
    uint32_t dim_val  = (cur_src_height << 11) | cur_src_width;
    *(volatile uint64_t*)(ddr_base + NV_CTRL_OFFSET) =
        ((uint64_t)dim_val << 32) | (uint64_t)ctrl_val;
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
