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
#include <sys/stat.h>
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
/* Bug B fix 2026-06-03: WriteFrame thread and Keepalive thread both
 * read/write these. volatile prevents the compiler from caching them
 * across function boundaries when threads alias the storage. */
static volatile uint32_t frame_counter = 0;
static volatile int      active_buf    = 0;
static volatile uint16_t last_width    = NV_TARGET_WIDTH;
static volatile uint16_t last_height   = NV_TARGET_HEIGHT;
/* Bug B v2 fix 2026-06-03: set true on first WriteFrame; checked by
 * mister_present() to stop writing DDR3 once gameplay starts. */
static volatile int      has_rendered  = 0;
/* TEMPORARY DIAG 2026-06-03: test-pattern mode for downscale debugging.
 * Activated by touching /tmp/openbor_test_pattern before binary start.
 * When active, WriteFrame replaces engine pixels with a diagnostic
 * gradient: R-ramp top-to-bottom, G-ramp left-to-right. Lets us tell
 * by visual inspection whether reader+downscale are reading source
 * lines in correct order (smooth gradient) vs broken (uniform color,
 * random flicker, garbled). REVERT AFTER MEASURED. */
static volatile int      test_pattern_mode = 0;

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

    /* TEMPORARY DIAG: enable test-pattern mode if flag file present. */
    {
        struct stat st;
        if (stat("/tmp/openbor_test_pattern", &st) == 0) {
            test_pattern_mode = 1;
            fprintf(stderr, "NativeVideoWriter: TEST PATTERN MODE ENABLED (diagnostic) — "
                    "engine pixels suppressed, writing gradient instead\n");
        }
    }
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

    /* TEMPORARY DIAG: test-pattern mode. When enabled by /tmp/openbor_test_pattern
     * flag file at Init time, replace ALL engine pixels with a diagnostic
     * gradient. R-ramp top-to-bottom (0..31 across `height` source rows),
     * G-ramp left-to-right (0..63 across `width` source cols), B=0.
     *
     * Visual interpretation of FPGA output (after downscale to 320x224):
     *   - Smooth red gradient top->bottom + green gradient left->right ->
     *     reader+downscale work correctly, source-line ordering is right
     *   - All-one-color uniform output ->
     *     V-pass reads same source line for all dest lines (slot
     *     mapping broken)
     *   - Banded/striped output ->
     *     H-pass or V-pass positioning broken
     *   - Random per-frame variation ->
     *     ring buffer race or timing issue
     *   - Garbled/noise ->
     *     reader DDR3 address calculation broken
     * REVERT AFTER MEASURED. */
    if (test_pattern_mode) {
        /* TEMPORARY DIAG v2 2026-06-03: 6 distinct color BANDS per Y region
         * + horizontal position markers. Replaces the subtle gradient
         * (R-ramp 0..31 + G-ramp 0..63) which made V-pass collapse hard
         * to see. With distinct bands, V-pass behavior is unambiguous:
         *
         *   - V-pass works correctly: 6 horizontal color bands top-to-bottom
         *     (BLACK, RED, GREEN, BLUE, YELLOW, WHITE)
         *   - V-pass collapsed (reads same src line for all dest lines):
         *     screen shows ONE solid color (whichever band V-pass landed on)
         *   - V-pass partially works: bands visible but with wrong sizes
         *     or wrong colors
         *
         * Plus VERTICAL POSITION MARKERS: white vertical line at x=W/4,
         * W/2, 3W/4. Helps verify H-pass positioning — markers should
         * appear at correct screen positions if H-pass works. */
        int y;
        int x;
        static int diag_logged = 0;
        if (!diag_logged) {
            fprintf(stderr, "NativeVideoWriter: test pattern v2 frame 0: %dx%d bpp=%d (6 bands)\n",
                    width, height, bpp);
            diag_logged = 1;
        }
        const uint16_t band_colors[6] = {
            0x0000,  /* 0: BLACK    */
            0xF800,  /* 1: RED      */
            0x07E0,  /* 2: GREEN    */
            0x001F,  /* 3: BLUE     */
            0xFFE0,  /* 4: YELLOW   */
            0xFFFF   /* 5: WHITE    */
        };
        /* Mark x positions for H-pass verification (1-pixel-wide vertical
         * markers in CONTRASTING color). Using x positions divisible by
         * 4 to align with qword boundaries in DDR3. */
        const int marker_x[3] = { 0, 0, 0 };  /* filled below */
        int marker_xs[3];
        marker_xs[0] = width / 4;
        marker_xs[1] = width / 2;
        marker_xs[2] = (width * 3) / 4;
        (void)marker_x;
        for (y = 0; y < height; y++) {
            int band = (y * 6) / height;
            if (band > 5) band = 5;
            uint16_t band_color = band_colors[band];
            /* Marker color contrasts the band: if band is dark (BLACK,
             * BLUE), use WHITE; otherwise use BLACK. */
            uint16_t marker_color = (band == 0 || band == 3) ? 0xFFFF : 0x0000;
            volatile uint16_t* dst_row = dst + (size_t)y * (size_t)width;
            for (x = 0; x < width; x++) {
                uint16_t pixel = band_color;
                if (x == marker_xs[0] || x == marker_xs[1] || x == marker_xs[2]) {
                    pixel = marker_color;
                }
                dst_row[x] = pixel;
            }
        }
        goto present_ctrl_dim;  /* skip the bpp dispatch */
    }

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

present_ctrl_dim:  /* TEMPORARY DIAG label for test-pattern early-jump */
    /* Bug B fix 2026-06-03: full-drain DSB SY before CTRL flip.
     *
     * The previous __sync_synchronize() compiles to DMB SY, which only
     * ORDERS subsequent memory accesses with respect to preceding ones.
     * It does NOT block until preceding writes complete. NEON store
     * sequences (pixel writes) flow through the write-combine buffer
     * and the L2 cache write-allocate path; DMB allows them to STILL
     * BE IN-FLIGHT while subsequent CTRL stores complete first to DDR3.
     * Result: FPGA sees the new CTRL flip and starts reading the
     * "just-written" buffer while the last lines of that buffer are
     * still draining → racing partial-frame visible as flicker.
     *
     * He-Man (960x480 = 921KB/frame) had a larger drain window than
     * ATOV (320x240 = 153KB/frame), causing visibly worse flicker.
     *
     * DSB SY blocks until ALL preceding memory operations are observable
     * to all observers including the HPS-FPGA bridge / DDR3 controller
     * queue. Cost: ~10-20 cycles on Cortex-A9 (vs DMB's ~2-3) — paid
     * once per WriteFrame at ~60 Hz = negligible. */
    __asm__ volatile("dsb sy" ::: "memory");

    /* Bug B fix 2026-06-03: atomic 64-bit CTRL+DIM write.
     *
     * The FPGA reads CTRL and DIM as ONE atomic 64-bit qword from the
     * same DDR3 word (ddr_dout[31:0]=CTRL, ddr_dout[63:32]=DIM). The
     * previous code wrote them as TWO SEPARATE 32-bit stores. The
     * FPGA could land its qword read BETWEEN the two stores, seeing
     * NEW-DIM + OLD-CTRL (or vice versa) — visually a 1-frame flicker
     * if DIM had changed, or a 1-frame stale-buffer flicker on every
     * frame regardless.
     *
     * Combined 64-bit store compiles to a single STRD on Cortex-A9
     * when the address is 8-byte aligned. NV_CTRL_OFFSET=0x00 is
     * page-aligned (ddr_base = mmap'd page), so alignment is
     * guaranteed.
     *
     * ALSO: pre-flip active_buf BEFORE the CTRL write. Otherwise the
     * keepalive thread (running every 150ms on a separate thread) can
     * read active_buf in the OLD state between our CTRL flip and our
     * own ^=1 below, computing last_written = !active_buf with the
     * WRONG buffer index → emits a CTRL flip to the STALE buffer for
     * one keepalive tick (~16ms visible flicker).
     *
     * Order:
     *   1. Read active_buf (= buffer we just wrote)
     *   2. Flip active_buf BEFORE CTRL write
     *   3. Atomic 64-bit CTRL+DIM write
     * Keepalive sees post-flip active_buf consistently. */
    last_width  = (uint16_t)width;
    last_height = (uint16_t)height;
    frame_counter++;
    uint32_t buf_just_written = (uint32_t)active_buf & 1u;
    active_buf = (int)(buf_just_written ^ 1u);
    uint64_t ctrl32 = ((uint64_t)frame_counter << 2) | (uint64_t)buf_just_written;
    uint64_t dim32  = ((uint64_t)(uint16_t)last_height << 16)
                    | (uint64_t)(uint16_t)last_width;
    *(volatile uint64_t*)(ddr_base + NV_CTRL_OFFSET) =
        (dim32 << 32) | ctrl32;

    /* Bug B v2 fix 2026-06-03: signal that gameplay is rendering, so
     * mister_present() (SDL dummy driver path) stops writing DDR3 and
     * stops mutating its independent (mister_active_buf, mister_frame_cnt)
     * state. Eliminates the dual-CTRL-writer race that caused severe
     * gameplay flicker on ATOV + He-Man. Sticky flag — never cleared
     * (boot-screen rendering only happens before first frame). */
    has_rendered = 1;
}

bool NativeVideoWriter_IsActive(void) {
    return ddr_base != NULL;
}

int NativeVideoWriter_HasRendered(void) {
    return has_rendered;
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
     * Bug B fix 2026-06-03: atomic 64-bit CTRL+DIM write — same pattern
     * as WriteFrame. Keepalive runs every 150ms on a separate thread;
     * if it interleaved with WriteFrame's two separate 32-bit stores,
     * FPGA could read a mid-update qword. Single 64-bit store eliminates
     * that race window.
     *
     * No DSB SY needed here — keepalive doesn't write pixel data, only
     * refreshes the CTRL+DIM qword. Buffer contents are stable from the
     * last WriteFrame, which already DSB-drained. */
    if (!ddr_base) return;
    frame_counter++;
    uint32_t last_written = (uint32_t)((!active_buf) & 1);
    uint64_t ctrl32 = ((uint64_t)frame_counter << 2) | (uint64_t)last_written;
    uint64_t dim32  = ((uint64_t)(uint16_t)last_height << 16)
                    | (uint64_t)(uint16_t)last_width;
    *(volatile uint64_t*)(ddr_base + NV_CTRL_OFFSET) =
        (dim32 << 32) | ctrl32;
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
