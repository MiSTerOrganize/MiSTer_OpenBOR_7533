/* vscreen_align_probe.c -- settles Tier-B review finding D2.
 *
 * native_video_writer.c selects downscale variant 1 (the NN row pick, no box)
 * with:   width == 320 && ((uintptr_t)src_row & 15) == 0
 * where src_row = surface->data + src_y * pitch, and surface->data is
 * vscreen->data (videocommon.c passes src->data straight through).
 *
 * This probe reproduces s_screen's layout and allocscreen()'s malloc verbatim
 * from pristine v7533 (source/gamelib/types.h:97-108, screen.c:20-51) and
 * reports whether (data & 15) can EVER be 0 on the real device allocator.
 *
 * DEBUG/VERIFY harness only -- ships nothing.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <stddef.h>

#define ANYNUMBER 2                 /* types.h:16 */

typedef struct {                    /* types.h:97-108, PSP block not compiled */
    int             magic;
    int             width;
    int             height;
    int             pixelformat;
    unsigned char  *palette;
    unsigned char   data[ANYNUMBER];
} s_screen;

/* screen.c:20-51, non-PIXEL_x8 branch (the 16-bit ship build) */
static s_screen *allocscreen16(int width, int height)
{
    int psize;
    width &= (0xFFFFFFFF - 3);
    psize = width * height * 2;     /* pixelbytes[PIXEL_16] == 2 */
    return (s_screen *)malloc(sizeof(s_screen) + psize + ANYNUMBER);
}

int main(void)
{
    int trial, hit0 = 0, n = 0, rowhit0 = 0, rows = 0;
    printf("== s_screen layout (arm32) ==\n");
    printf("  offsetof(s_screen, data) = %u   (D2 claims 20)\n",
           (unsigned)offsetof(s_screen, data));
    printf("  sizeof(s_screen)         = %u\n", (unsigned)sizeof(s_screen));
    printf("  sizeof(void*)            = %u\n", (unsigned)sizeof(void *));
    printf("\n== allocscreen(320,240,PIXEL_16): 153,600 px bytes (mmap path) ==\n");

    for (trial = 0; trial < 128; trial++) {
        /* perturb heap state so we sample both chunk parities */
        void *noise = malloc(1 + (trial * 37) % 8191);
        s_screen *s = allocscreen16(320, 240);
        uintptr_t base, d;
        int pitch = 320 * 2, y;
        if (!s) { printf("  malloc failed\n"); free(noise); continue; }
        base = (uintptr_t)s;
        d    = (uintptr_t)s->data;
        if (trial < 6)
            printf("  trial %2d: base%%16=%2u  data%%16=%2u  pitch=%d (pitch%%16=%d)\n",
                   trial, (unsigned)(base & 15), (unsigned)(d & 15), pitch, pitch & 15);
        if ((d & 15) == 0) hit0++;
        n++;
        /* variant 1 tests EVERY row: src_row = data + src_y*pitch */
        for (y = 0; y < 224; y++) {
            if (((d + (uintptr_t)y * pitch) & 15) == 0) rowhit0++;
            rows++;
        }
        free(s);
        free(noise);
    }
    printf("  -> %d/%d screens with (data & 15)==0\n", hit0, n);
    printf("  -> %d/%d individual rows with (src_row & 15)==0\n", rowhit0, rows);

    printf("\n== same, small allocation (brk path): 320x64 ==\n");
    hit0 = n = 0;
    for (trial = 0; trial < 128; trial++) {
        void *noise = malloc(1 + (trial * 53) % 511);
        s_screen *s = allocscreen16(320, 64);      /* 40,960 B, under mmap threshold */
        if (s) {
            if ((((uintptr_t)s->data) & 15) == 0) hit0++;
            n++;
            free(s);
        }
        free(noise);
    }
    printf("  -> %d/%d screens with (data & 15)==0\n", hit0, n);

    printf("\nVERDICT: downscale variant 1 is %s on this allocator.\n",
           (hit0 || rowhit0) ? "REACHABLE" : "UNREACHABLE");
    return 0;
}
