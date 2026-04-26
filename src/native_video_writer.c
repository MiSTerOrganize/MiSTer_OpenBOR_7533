//
//  Native Video DDR3 Writer — OpenBOR MiSTer
//
//  Writes 320x240 RGB565 frames to DDR3 at 0x3A000000 for FPGA native
//  video output. Double-buffered with control word handshake.
//
//  DDR3 Memory Map (must match openbor_video_reader.sv):
//    0x3A000000 + 0x000     : Control word (frame_counter[31:2] | active_buf[1:0])
//    0x3A000000 + 0x008     : Joystick P1 (32 bits)
//    0x3A000000 + 0x010     : Cart control (file_size from FPGA)
//    0x3A000000 + 0x018     : Joystick P2 (32 bits)
//    0x3A000000 + 0x020     : Joystick P3 (32 bits)
//    0x3A000000 + 0x028     : Joystick P4 (32 bits)
//    0x3A000000 + 0x040     : Buffer 0 (320*240*2 = 153,600 bytes)
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
#define NV_FRAME_HEIGHT     240
#define NV_FRAME_BYTES      (NV_FRAME_WIDTH * NV_FRAME_HEIGHT * 2)  /* 153,600 */

static const uint32_t joy_offsets[4] = {
    NV_JOY0_OFFSET, NV_JOY1_OFFSET, NV_JOY2_OFFSET, NV_JOY3_OFFSET
};

static int mem_fd = -1;
static volatile uint8_t* ddr_base = NULL;
static uint32_t frame_counter = 0;
static int active_buf = 0;
static int first_frame = 1;
static volatile int debug_dump_request = 0;

void NativeVideoWriter_RequestDebugDump(void) {
    debug_dump_request = 1;
}

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

    /* Clear both buffers and control words */
    memset((void*)(ddr_base + NV_BUF0_OFFSET), 0, NV_FRAME_BYTES);
    memset((void*)(ddr_base + NV_BUF1_OFFSET), 0, NV_FRAME_BYTES);
    volatile uint32_t* ctrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
    *ctrl = 0;
    volatile uint32_t* cart_ctrl = (volatile uint32_t*)(ddr_base + NV_CART_CTRL_OFFSET);
    *cart_ctrl = 0;
    frame_counter = 0;
    active_buf = 0;
    first_frame = 3;   /* sample the first three frames */

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

    /* Debug: dump format + raw byte samples from a handful of pixels for
     * the first 3 frames. Gives us enough signal to tell whether colors
     * look wrong because of byte-order (we extract wrong channels), a
     * palette issue (8bpp path with bad palette), or because OpenBOR
     * already produced wrong colors before reaching us. */
    int do_dump = (first_frame > 0) || debug_dump_request;
    if (do_dump) {
        const char *why = debug_dump_request ? "SELECT" : "BOOT";
        int frame_no = debug_dump_request ? 99 : (4 - first_frame);
        const uint8_t *src = (const uint8_t *)pixels;
        fprintf(stderr,
            "NativeVideoWriter[%s F%d]: %dx%d pitch=%d bpp=%d palette=%p\n",
            why, frame_no, width, height, pitch, bpp, palette);

        /* SELECT dumps a 4x4 grid across the frame so enemy pixels
         * (which rarely line up with the three fixed BOOT samples)
         * have a chance to show up. BOOT dumps stay terse. */
        int grid = debug_dump_request ? 4 : 3;
        int samples[16][2];
        int nsamples = 0;
        if (debug_dump_request) {
            for (int gy = 0; gy < 4; gy++)
                for (int gx = 0; gx < 4; gx++) {
                    samples[nsamples][0] = (width  * (gx * 2 + 1)) / 8;
                    samples[nsamples][1] = (height * (gy * 2 + 1)) / 8;
                    nsamples++;
                }
        } else {
            samples[0][0]=0;          samples[0][1]=0;
            samples[1][0]=width/2;    samples[1][1]=height/2;
            samples[2][0]=width-1;    samples[2][1]=height-1;
            nsamples = 3;
        }
        (void)grid;
        int bypp = bpp / 8; if (bypp < 1) bypp = 1;
        for (int i = 0; i < nsamples; i++) {
            int sx = samples[i][0], sy = samples[i][1];
            const uint8_t *p = src + sy * pitch + sx * bypp;
            if (bpp == 32) {
                fprintf(stderr,
                    "  px(%3d,%3d) raw=%02X %02X %02X %02X "
                    "-> assumed RGBA r=%02X g=%02X b=%02X\n",
                    sx, sy, p[0], p[1], p[2], p[3], p[0], p[1], p[2]);
            } else if (bpp == 16) {
                uint16_t px = ((const uint16_t *)p)[0];
                fprintf(stderr,
                    "  px(%3d,%3d) raw=%04X (LE bytes %02X %02X)\n",
                    sx, sy, px, p[0], p[1]);
            } else if (bpp == 8) {
                uint8_t idx = p[0];
                if (palette) {
                    const uint8_t *pal = (const uint8_t *)palette;
                    fprintf(stderr,
                        "  px(%3d,%3d) idx=%02X -> pal r=%02X g=%02X b=%02X\n",
                        sx, sy, idx, pal[idx*3+0], pal[idx*3+1], pal[idx*3+2]);
                } else {
                    fprintf(stderr,
                        "  px(%3d,%3d) idx=%02X (no palette!)\n",
                        sx, sy, idx);
                }
            } else {
                fprintf(stderr, "  px(%3d,%3d) bpp=%d unhandled\n", sx, sy, bpp);
            }
        }
        fflush(stderr);
        if (debug_dump_request) debug_dump_request = 0;
        else if (first_frame > 0)  first_frame--;
    }

    /* Clamp to frame dimensions */
    if (width > NV_FRAME_WIDTH) width = NV_FRAME_WIDTH;
    if (height > NV_FRAME_HEIGHT) height = NV_FRAME_HEIGHT;
    if (width <= 0 || height <= 0) return;

    uint32_t buf_offset = (active_buf == 0) ? NV_BUF0_OFFSET : NV_BUF1_OFFSET;
    volatile uint16_t* dst = (volatile uint16_t*)(ddr_base + buf_offset);
    int src_bpp_bytes = bpp / 8;

    if (bpp == 16) {
        /* OpenBOR's 16bpp surfaces are BGR565 (B in high bits). The FPGA
         * decoder expects RGB565 (R in high bits). Swap the R and B
         * 5-bit fields per pixel while preserving the 6-bit G channel. */
        const uint8_t* src = (const uint8_t*)pixels;
        for (int y = 0; y < height; y++) {
            const uint16_t* src_row = (const uint16_t*)(src + y * pitch);
            volatile uint16_t* dst_row = dst + y * NV_FRAME_WIDTH;
            for (int x = 0; x < width; x++) {
                uint16_t px = src_row[x];
                uint16_t r5 = px & 0x001F;
                uint16_t g6 = px & 0x07E0;
                uint16_t b5 = (px & 0xF800) >> 11;
                dst_row[x] = (r5 << 11) | g6 | b5;
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
            for (int x = 0; x < width; x++) {
                uint8_t idx = row[x];
                uint8_t r = pal[idx * 3 + 0];
                uint8_t g = pal[idx * 3 + 1];
                uint8_t b = pal[idx * 3 + 2];
                dst[y * NV_FRAME_WIDTH + x] =
                    (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
            }
        }
    }
    else if (bpp == 32) {
        /* 32bpp -- OpenBOR's SDL 1.2 surface is laid out byte-0=R,
         * byte-1=G, byte-2=B, byte-3=A (RGBA). Extracting r from byte 0
         * and b from byte 2 matches what the FPGA decoder expects in
         * RGB565 (r in high bits). The older BGRA assumption produced
         * a uniform blue tint in gameplay (first reported 2026-04-15). */
        const uint8_t* src = (const uint8_t*)pixels;
        for (int y = 0; y < height; y++) {
            const uint8_t* row = src + y * pitch;
            for (int x = 0; x < width; x++) {
                int i = x * 4;
                uint8_t r = row[i + 0];
                uint8_t g = row[i + 1];
                uint8_t b = row[i + 2];
                dst[y * NV_FRAME_WIDTH + x] =
                    (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
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
