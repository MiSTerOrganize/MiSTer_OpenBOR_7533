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

#define MISTER_DDR_PHYS_BASE   0x3A000000u
#define MISTER_DDR_REGION_SIZE 0x00100000u
#define MISTER_CTRL_OFFSET     0x00000000u
#define MISTER_BUF0_OFFSET     0x00000040u
#define MISTER_BUF1_OFFSET     0x00040040u
#define MISTER_FRAME_W         320
#define MISTER_FRAME_H         240
#define MISTER_FRAME_BYTES     (MISTER_FRAME_W * MISTER_FRAME_H * 2)

static int                 mister_fd        = -1;
static volatile uint8_t   *mister_ddr       = NULL;
static volatile uint32_t  *mister_ctrl      = NULL;
static uint32_t            mister_frame_cnt = 0;
static int                 mister_active_buf = 0;
static int                 mister_logged    = 0;

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
}

static void mister_present(SDL_Surface *screen) {
    if (!mister_ddr || !screen || !screen->pixels) return;
    int w = screen->w, h = screen->h;
    int bpp = screen->format->BitsPerPixel;
    int pitch = screen->pitch;
    int Rshift = screen->format->Rshift;
    int Gshift = screen->format->Gshift;
    int Bshift = screen->format->Bshift;
    /* SDL2 SDL_PixelFormat has Rloss/Gloss/Bloss too. */
    int Rloss  = screen->format->Rloss;
    int Gloss  = screen->format->Gloss;
    int Bloss  = screen->format->Bloss;
    SDL_Palette *pal = screen->format->palette;

    /* Scale to fit entirely within 320x240, no cropping. */
    int scale256 = 256;
    if (w > MISTER_FRAME_W || h > MISTER_FRAME_H) {
        int sx256 = (w * 256 + MISTER_FRAME_W - 1) / MISTER_FRAME_W;
        int sy256 = (h * 256 + MISTER_FRAME_H - 1) / MISTER_FRAME_H;
        scale256 = sx256 > sy256 ? sx256 : sy256;
    }
    int out_w = (w * 256) / scale256;
    int out_h = (h * 256) / scale256;
    if (out_w > MISTER_FRAME_W) out_w = MISTER_FRAME_W;
    if (out_h > MISTER_FRAME_H) out_h = MISTER_FRAME_H;
    int dst_y0 = (MISTER_FRAME_H - out_h) / 2;

    if (!mister_logged) {
        fprintf(stderr, "MiSTer SDL2: first present %dx%d bpp=%d pitch=%d "
                "scale256=%d -> %dx%d dst_y0=%d palette=%p\\n",
                w, h, bpp, pitch, scale256, out_w, out_h, dst_y0, pal);
        mister_logged = 1;
    }

    uint32_t buf_off = mister_active_buf ? MISTER_BUF1_OFFSET : MISTER_BUF0_OFFSET;
    volatile uint16_t *dst = (volatile uint16_t *)(mister_ddr + buf_off);
    const uint8_t *rows = (const uint8_t *)screen->pixels;

    /* Clear BOTH buffers once on first frame for letterboxing. */
    {
        static int cleared = 0;
        if (!cleared) {
            volatile uint16_t *buf0 = (volatile uint16_t *)(mister_ddr + MISTER_BUF0_OFFSET);
            volatile uint16_t *buf1 = (volatile uint16_t *)(mister_ddr + MISTER_BUF1_OFFSET);
            memset((void*)buf0, 0, MISTER_FRAME_W * MISTER_FRAME_H * 2);
            memset((void*)buf1, 0, MISTER_FRAME_W * MISTER_FRAME_H * 2);
            cleared = 1;
        }
    }

    if (bpp == 32) {
        for (int y = 0; y < out_h; y++) {
            int src_y = (y * scale256) / 256;
            const uint32_t *row = (const uint32_t *)(rows + src_y * pitch);
            volatile uint16_t *out_row = dst + (dst_y0 + y) * MISTER_FRAME_W;
            for (int x = 0; x < out_w; x++) {
                int src_x = (x * scale256) / 256;
                uint32_t px = row[src_x];
                uint8_t r = ((px & screen->format->Rmask) >> Rshift) << Rloss;
                uint8_t g = ((px & screen->format->Gmask) >> Gshift) << Gloss;
                uint8_t b = ((px & screen->format->Bmask) >> Bshift) << Bloss;
                out_row[x] = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3);
            }
        }
    }
    else if (bpp == 16) {
        for (int y = 0; y < out_h; y++) {
            int src_y = (y * scale256) / 256;
            const uint16_t *row = (const uint16_t *)(rows + src_y * pitch);
            volatile uint16_t *out_row = dst + (dst_y0 + y) * MISTER_FRAME_W;
            for (int x = 0; x < out_w; x++) {
                int src_x = (x * scale256) / 256;
                uint16_t px = row[src_x];
                uint8_t r = ((px & screen->format->Rmask) >> Rshift) << Rloss;
                uint8_t g = ((px & screen->format->Gmask) >> Gshift) << Gloss;
                uint8_t b = ((px & screen->format->Bmask) >> Bshift) << Bloss;
                out_row[x] = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3);
            }
        }
    }
    else if (bpp == 8 && pal) {
        for (int y = 0; y < out_h; y++) {
            int src_y = (y * scale256) / 256;
            const uint8_t *row = rows + src_y * pitch;
            volatile uint16_t *out_row = dst + (dst_y0 + y) * MISTER_FRAME_W;
            for (int x = 0; x < out_w; x++) {
                int src_x = (x * scale256) / 256;
                SDL_Color c = pal->colors[row[src_x]];
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
    "    /* SDL_GetWindowSurface returns the cached framebuffer surface\n"
    "     * created by SDL_DUMMY_CreateWindowFramebuffer above; this is\n"
    "     * the same SDL_Surface OpenBOR drew into. */\n"
    "    SDL_Surface *surface = SDL_GetWindowSurface(window);\n"
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

    # 2) Initialize DDR3 mapping inside CreateWindowFramebuffer so it
    #    runs lazily on first frame allocation.
    create_anchor = "int SDL_DUMMY_CreateWindowFramebuffer(_THIS, SDL_Window * window, Uint32 * format, void ** pixels, int *pitch)\n{"
    if create_anchor in src:
        src = src.replace(
            create_anchor,
            create_anchor + "\n    mister_ddr_init();",
            1
        )
        print("  CreateWindowFramebuffer: mister_ddr_init() injected.")
    else:
        print("  WARN: CreateWindowFramebuffer signature not found; init may not happen.")

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
