#!/usr/bin/env python3
"""
patch_sdl_dummy.py -- inject DDR3-write code into SDL 2.0.8's dummy
video driver framebuffer hook so OpenBOR's full SDL render pipeline
lands its final composited frames directly in the FPGA's video ring
buffer.

SDL2's dummy driver routes window surface updates through:
  SDL_DUMMY_CreateWindowFramebuffer  -- allocates the pixel buffer
  SDL_DUMMY_UpdateWindowFramebuffer  -- called on SDL_UpdateWindowSurface
  SDL_DUMMY_DestroyWindowFramebuffer -- frees the pixel buffer

We hook UpdateWindowFramebuffer to read the surface attached to the
window and write its pixels (after fixed-point fit-to-screen scaling)
into the FPGA's DDR3 ring buffer at 0x3A000000.

Patches src/video/dummy/SDL_nullframebuffer.c.
"""

import sys

INJECT_INCLUDES = """
/* MiSTer DDR3 native-video bridge -- see patch_sdl_dummy.py */
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <stdint.h>
#include <pthread.h>

#define MISTER_DDR_PHYS_BASE   0x3A000000u
#define MISTER_DDR_REGION_SIZE 0x00100000u
#define MISTER_CTRL_OFFSET     0x00000000u
#define MISTER_BUF0_OFFSET     0x00000040u
#define MISTER_BUF1_OFFSET     0x00040040u
#define MISTER_FRAME_W         320
#define MISTER_FRAME_H         224  /* Sega CD V28 NTSC */
#define MISTER_FRAME_BYTES     (MISTER_FRAME_W * MISTER_FRAME_H * 2)

static int                 mister_fd        = -1;
static volatile uint8_t   *mister_ddr       = NULL;
static volatile uint32_t  *mister_ctrl      = NULL;
static uint32_t            mister_frame_cnt = 0;
static int                 mister_active_buf = 0;
static int                 mister_logged    = 0;
static pthread_t           mister_keepalive_tid;
static volatile int        mister_keepalive_run = 0;

/* Keepalive thread — pings the FPGA frame counter every ~150ms even
 * when ARM isn't producing frames. The FPGA video reader has a
 * staleness timeout: if frame_cnt doesn't change for ~30 vblanks
 * (~500ms) it sets frame_ready_reg=0 and BLANKS the screen. During
 * heavy model loading on big PAKs (He-Man, Avengers, late-build
 * sets) individual model parses take >500ms while the engine
 * throttles update_loading calls — so the FPGA blanks then unblanks,
 * producing the visible black/content flicker on the loading screen.
 *
 * Bumping the counter without rewriting the buffer keeps the same
 * image on screen (FPGA re-reads same active_buffer offset) but
 * keeps frame_ready_reg latched true. Same image, no flicker.
 *
 * IMPORTANT: mister_present writes buf X then TOGGLES mister_active_buf
 * to !X. So after a present, the LAST WRITTEN buffer is (!mister_active_buf).
 * Use that for the keepalive ctrl word — otherwise the FPGA flips to
 * the OTHER buffer (which holds the previous frame) and the loading
 * bar jitters between two positions. */
static void *mister_keepalive_fn(void *arg) {
    (void)arg;
    while (mister_keepalive_run) {
        usleep(150000); /* 150ms */
        if (mister_ctrl) {
            int last_written = !mister_active_buf & 1;
            mister_frame_cnt++;
            *mister_ctrl = (mister_frame_cnt << 2) | last_written;
        }
    }
    return NULL;
}

static void mister_ddr_init(void) {
    if (mister_ddr) return;
    mister_fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (mister_fd < 0) {
        fprintf(stderr, "MiSTer SDL2: open /dev/mem failed\\n");
        return;
    }
    mister_ddr = (volatile uint8_t *)mmap(NULL, MISTER_DDR_REGION_SIZE,
        PROT_READ | PROT_WRITE, MAP_SHARED, mister_fd, MISTER_DDR_PHYS_BASE);
    if (mister_ddr == MAP_FAILED) {
        fprintf(stderr, "MiSTer SDL2: mmap DDR3 failed\\n");
        mister_ddr = NULL;
        close(mister_fd);
        mister_fd = -1;
        return;
    }
    mister_ctrl = (volatile uint32_t *)(mister_ddr + MISTER_CTRL_OFFSET);
    *mister_ctrl = 0;
    fprintf(stderr, "MiSTer SDL2: DDR3 mapped @ 0x%08X (driver=dummy_native)\\n",
            MISTER_DDR_PHYS_BASE);
    mister_keepalive_run = 1;
    pthread_create(&mister_keepalive_tid, NULL, mister_keepalive_fn, NULL);
}

/* C90-compliant: all decls at function top, all loop indices declared
 * up front. SDL 2.0.8 builds with -Werror=declaration-after-statement
 * so we cannot mix decls with statements anywhere in this file. */
static void mister_present(SDL_Surface *screen) {
    int w, h, bpp, pitch;
    int Rshift, Gshift, Bshift, Rloss, Gloss, Bloss;
    SDL_Palette *pal;
    int sx256, sy256;
    int out_w, out_h, dst_y0;
    uint32_t buf_off;
    volatile uint16_t *dst;
    const uint8_t *rows;
    static int cleared = 0;
    int x, y, src_x, src_y;

    if (!mister_ddr || !screen || !screen->pixels) return;

    w      = screen->w;
    h      = screen->h;
    bpp    = screen->format->BitsPerPixel;
    pitch  = screen->pitch;
    Rshift = screen->format->Rshift;
    Gshift = screen->format->Gshift;
    Bshift = screen->format->Bshift;
    Rloss  = screen->format->Rloss;
    Gloss  = screen->format->Gloss;
    Bloss  = screen->format->Bloss;
    pal    = screen->format->palette;

    /* Anisotropic squish: fill entire 320x224 dest, X and Y scaled
     * independently. PAK content authored at non-224 native heights
     * (320x240 ~7% Y compress, 480x272 X+Y compress, 960x480 huge
     * downscale) maps to fill the Sega CD V28 NTSC active area
     * exactly. Aspect distortion is intentional — matches Sega CD
     * displayed area edge-to-edge, no letterbox. */
    sx256 = (w * 256) / MISTER_FRAME_W;
    sy256 = (h * 256) / MISTER_FRAME_H;
    out_w = MISTER_FRAME_W;
    out_h = MISTER_FRAME_H;
    dst_y0 = 0;

    if (!mister_logged) {
        fprintf(stderr, "MiSTer SDL2: first present %dx%d bpp=%d pitch=%d "
                "sx256=%d sy256=%d -> %dx%d Rmask=0x%08X Gmask=0x%08X Bmask=0x%08X "
                "Rshift=%d Gshift=%d Bshift=%d palette=%p\\n",
                w, h, bpp, pitch, sx256, sy256, out_w, out_h,
                (unsigned)screen->format->Rmask, (unsigned)screen->format->Gmask,
                (unsigned)screen->format->Bmask,
                Rshift, Gshift, Bshift, pal);
        mister_logged = 1;
    }

    buf_off = mister_active_buf ? MISTER_BUF1_OFFSET : MISTER_BUF0_OFFSET;
    dst  = (volatile uint16_t *)(mister_ddr + buf_off);
    rows = (const uint8_t *)screen->pixels;

    /* Clear BOTH buffers once on first frame for letterboxing. */
    if (!cleared) {
        volatile uint16_t *buf0 = (volatile uint16_t *)(mister_ddr + MISTER_BUF0_OFFSET);
        volatile uint16_t *buf1 = (volatile uint16_t *)(mister_ddr + MISTER_BUF1_OFFSET);
        memset((void*)buf0, 0, MISTER_FRAME_W * MISTER_FRAME_H * 2);
        memset((void*)buf1, 0, MISTER_FRAME_W * MISTER_FRAME_H * 2);
        cleared = 1;
    }

    if (bpp == 32) {
        /* Anisotropic bilinear: X and Y scaled independently via sx256
         * and sy256. Bilinear unconditionally — for the rare case where
         * source already matches 320x224 exactly (sx256==sy256==256),
         * fractional weights are zero so it degenerates to nearest. */
        uint32_t Rmask = screen->format->Rmask;
        uint32_t Gmask = screen->format->Gmask;
        uint32_t Bmask = screen->format->Bmask;
        {
            for (y = 0; y < out_h; y++) {
                int sy_fp, sy_int, sy_frac, sy_next;
                int sy_w0, sy_w1;
                const uint32_t *row0;
                const uint32_t *row1;
                volatile uint16_t *out_row;
                sy_fp   = y * sy256;
                sy_int  = sy_fp >> 8;
                sy_frac = sy_fp & 0xFF;
                sy_next = sy_int + 1;
                if (sy_next >= h) sy_next = h - 1;
                sy_w0 = 256 - sy_frac;
                sy_w1 = sy_frac;
                row0 = (const uint32_t *)(rows + sy_int  * pitch);
                row1 = (const uint32_t *)(rows + sy_next * pitch);
                out_row = dst + (dst_y0 + y) * MISTER_FRAME_W;
                for (x = 0; x < out_w; x++) {
                    int sx_fp, sx_int, sx_frac, sx_next;
                    int sx_w0, sx_w1;
                    uint32_t p00, p01, p10, p11;
                    int r00, r01, r10, r11, g00, g01, g10, g11, b00, b01, b10, b11;
                    int top_r, top_g, top_b, bot_r, bot_g, bot_b;
                    int out_r, out_g, out_b;
                    sx_fp   = x * sx256;
                    sx_int  = sx_fp >> 8;
                    sx_frac = sx_fp & 0xFF;
                    sx_next = sx_int + 1;
                    if (sx_next >= w) sx_next = w - 1;
                    sx_w0 = 256 - sx_frac;
                    sx_w1 = sx_frac;
                    p00 = row0[sx_int];
                    p01 = row0[sx_next];
                    p10 = row1[sx_int];
                    p11 = row1[sx_next];
                    r00 = (int)(((p00 & Rmask) >> Rshift) << Rloss);
                    g00 = (int)(((p00 & Gmask) >> Gshift) << Gloss);
                    b00 = (int)(((p00 & Bmask) >> Bshift) << Bloss);
                    r01 = (int)(((p01 & Rmask) >> Rshift) << Rloss);
                    g01 = (int)(((p01 & Gmask) >> Gshift) << Gloss);
                    b01 = (int)(((p01 & Bmask) >> Bshift) << Bloss);
                    r10 = (int)(((p10 & Rmask) >> Rshift) << Rloss);
                    g10 = (int)(((p10 & Gmask) >> Gshift) << Gloss);
                    b10 = (int)(((p10 & Bmask) >> Bshift) << Bloss);
                    r11 = (int)(((p11 & Rmask) >> Rshift) << Rloss);
                    g11 = (int)(((p11 & Gmask) >> Gshift) << Gloss);
                    b11 = (int)(((p11 & Bmask) >> Bshift) << Bloss);
                    top_r = r00 * sx_w0 + r01 * sx_w1;
                    top_g = g00 * sx_w0 + g01 * sx_w1;
                    top_b = b00 * sx_w0 + b01 * sx_w1;
                    bot_r = r10 * sx_w0 + r11 * sx_w1;
                    bot_g = g10 * sx_w0 + g11 * sx_w1;
                    bot_b = b10 * sx_w0 + b11 * sx_w1;
                    /* top/bot are in [0, 255*256] = [0, 65280]. After
                     * vertical blend and >>16 we land back in [0, 255]. */
                    out_r = (top_r * sy_w0 + bot_r * sy_w1) >> 16;
                    out_g = (top_g * sy_w0 + bot_g * sy_w1) >> 16;
                    out_b = (top_b * sy_w0 + bot_b * sy_w1) >> 16;
                    /* R/B swap fix: engine writes ABGR8888 (B at bit 16) but
                     * SDL2 dummy surface is ARGB8888 (R at bit 16), so the
                     * mask-extracted "r" actually holds engine's B. Swap on
                     * output. Wii build sets -DREVERSE_COLOR which packs
                     * 0xRRGGBB, matching SDL2; our Linux/SDL2 build does not. */
                    out_row[x] = ((out_b >> 3) << 11) | ((out_g >> 2) << 5) | (out_r >> 3);
                }
            }
        }
    }
    else if (bpp == 16) {
        /* Nearest-neighbor anisotropic — sx256/sy256 independently.
         * Same R/B swap fix as 32bpp: engine writes BGR565 (B at bit 11)
         * but SDL2 surface mask is standard RGB565 (R at bit 11) — swap
         * on output pack. See 32bpp comment above. */
        for (y = 0; y < out_h; y++) {
            const uint16_t *row;
            volatile uint16_t *out_row;
            src_y = (y * sy256) / 256;
            if (src_y >= h) src_y = h - 1;
            row = (const uint16_t *)(rows + src_y * pitch);
            out_row = dst + (dst_y0 + y) * MISTER_FRAME_W;
            for (x = 0; x < out_w; x++) {
                uint16_t px;
                uint8_t r, g, b;
                src_x = (x * sx256) / 256;
                if (src_x >= w) src_x = w - 1;
                px = row[src_x];
                r = ((px & screen->format->Rmask) >> Rshift) << Rloss;
                g = ((px & screen->format->Gmask) >> Gshift) << Gloss;
                b = ((px & screen->format->Bmask) >> Bshift) << Bloss;
                out_row[x] = ((b >> 3) << 11) | ((g >> 2) << 5) | (r >> 3);
            }
        }
    }
    else if (bpp == 8 && pal) {
        /* 8bpp palette path — nearest-neighbor anisotropic. Bilinear
         * in palette space would mix adjacent palette indices that map
         * to wildly different RGBs; not worth the artifacts for a small
         * (320x240 -> 320x224, ~7%) Y scrunch on the most common PAK
         * native dimensions. */
        for (y = 0; y < out_h; y++) {
            const uint8_t *row;
            volatile uint16_t *out_row;
            src_y = (y * sy256) / 256;
            if (src_y >= h) src_y = h - 1;
            row = rows + src_y * pitch;
            out_row = dst + (dst_y0 + y) * MISTER_FRAME_W;
            for (x = 0; x < out_w; x++) {
                SDL_Color c;
                src_x = (x * sx256) / 256;
                if (src_x >= w) src_x = w - 1;
                c = pal->colors[row[src_x]];
                out_row[x] = ((c.r >> 3) << 11) | ((c.g >> 2) << 5) | (c.b >> 3);
            }
        }
    }
    else {
        return;
    }

    mister_frame_cnt++;
    *mister_ctrl = (mister_frame_cnt << 2) | (mister_active_buf & 1);
    mister_active_buf ^= 1;
}
/* end MiSTer DDR3 bridge */
"""

# In SDL2's dummy framebuffer driver, the window surface is owned by
# SDL itself (SDL_GetWindowSurface returns it). The driver's
# UpdateWindowFramebuffer hook is called after the user calls
# SDL_UpdateWindowSurface — that's our cue to read the surface and
# write to DDR3.
UPDATE_NEW_BODY = (
    "int SDL_DUMMY_UpdateWindowFramebuffer(_THIS, SDL_Window * window, const SDL_Rect * rects, int numrects)\n"
    "{\n"
    "    /* C90 strict: all decls first, then statements. mister_ddr_init\n"
    "     * is idempotent (returns immediately if already mapped) so it's\n"
    "     * safe to call lazily on every frame instead of CreateFramebuffer. */\n"
    "    SDL_Surface *surface;\n"
    "    mister_ddr_init();\n"
    "    surface = SDL_GetWindowSurface(window);\n"
    "    if (surface) mister_present(surface);\n"
    "    return 0;\n"
    "}"
)

def main():
    if len(sys.argv) != 2:
        print("usage: patch_sdl_dummy.py <SDL_nullframebuffer.c>", file=sys.stderr)
        sys.exit(1)
    path = sys.argv[1]
    with open(path) as f:
        src = f.read()

    # 1) Inject our helper code right after the existing #include block.
    inject_anchor = '#include "SDL_nullframebuffer_c.h"\n'
    if inject_anchor not in src:
        # Fallback: any local include in the dummy driver
        for cand in ['#include "../SDL_sysvideo.h"\n', '#include "SDL_video.h"\n']:
            if cand in src:
                inject_anchor = cand
                break
    if inject_anchor not in src:
        print("ERROR: couldn't find an include anchor to inject helpers", file=sys.stderr)
        sys.exit(2)
    src = src.replace(inject_anchor, inject_anchor + INJECT_INCLUDES, 1)

    # 2) DDR3 init now happens lazily in UpdateWindowFramebuffer body
    #    (see UPDATE_NEW_BODY). Don't touch CreateWindowFramebuffer —
    #    SDL 2.0.8 strict C90 mode rejects mid-function decl injection.

    # 3) Replace UpdateWindowFramebuffer body to push the surface to DDR3.
    #    SDL2's stock implementation is a no-op (returns 0).
    update_sigs = [
        "int SDL_DUMMY_UpdateWindowFramebuffer(_THIS, SDL_Window * window, const SDL_Rect * rects, int numrects)\n{",
        "int SDL_DUMMY_UpdateWindowFramebuffer(_THIS, SDL_Window * window,\n                                      const SDL_Rect * rects, int numrects)\n{",
    ]
    sig_found = None
    for sig in update_sigs:
        if sig in src:
            sig_found = sig
            break
    if not sig_found:
        print("ERROR: couldn't locate SDL_DUMMY_UpdateWindowFramebuffer in source", file=sys.stderr)
        sys.exit(3)

    # Find the function and replace its full body.
    start = src.find(sig_found)
    brace = 0
    found_open = False
    end = start
    for i in range(start, len(src)):
        if src[i] == '{':
            brace += 1
            found_open = True
        elif src[i] == '}':
            brace -= 1
        if found_open and brace == 0:
            end = i + 1
            break
    src = src[:start] + UPDATE_NEW_BODY + src[end:]

    with open(path, 'w') as f:
        f.write(src)
    print(f"Patched {path}: DDR3 bridge installed in dummy framebuffer driver.")

if __name__ == '__main__':
    main()
