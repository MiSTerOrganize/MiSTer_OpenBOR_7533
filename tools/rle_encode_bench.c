/*
 * rle_encode_bench.c -- how much of OpenBOR PAK load time is the encodesprite
 * RLE pass, and would a single-pass encode (vs the current size-then-fill DOUBLE
 * scan) help?
 *
 * Faithful port of engine/source/gamelib/sprite.c::encodesprite (v7533): a
 * per-row run-length scan emitting [clearcount][viscount][pixels...] with a
 * 0xFF end-of-line marker, exactly as upstream (the inner loop, EOL, clearcount,
 * viscount, memcpy are all mirrored 1:1).
 *
 * THE KEY FACT this bench exists to quantify: OpenBOR calls encodesprite TWICE
 * per sprite in loadsprite -- once with dest=NULL to SIZE the buffer (this scan
 * lands in the [LOAD] "other" bucket, NOT "encode"), then once with dest!=NULL
 * to FILL it (the [LOAD] "encode" bucket). So the TRUE cost of the RLE stage is
 * size-pass + fill-pass, split across two [LOAD] buckets. This bench measures:
 *   - SIZE pass  (dest=NULL : scan only, count bytes)         -> "other" bucket
 *   - FILL pass  (dest!=NULL: scan + write linetab + memcpy)  -> "encode" bucket
 *   - MERGED     (one fill pass into a max-sized buffer)       -> ceiling if the
 *                                                                 size pass were
 *                                                                 eliminated
 * The SIZE-pass ns/px is the headroom a single-pass refactor could reclaim.
 *
 * Per-px numbers x (sum of clipped w*h over all sprites in a PAK) estimates the
 * RLE contribution to that PAK's load time. Compare vs the [LOAD] decode/encode/
 * other split from the deployed binary to decide if RLE is worth attacking.
 *
 * Build (CI bench.yml): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9
 *   -mfpu=neon -mfloat-abi=hard tools/rle_encode_bench.c -o rle_encode_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <math.h>

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

/* Faithful encodesprite inner scan. dest==NULL => size-only pass (the engine's
 * fakey_encodesprite sizing call). dest!=NULL => fill pass. Returns bytes used
 * by the pixel-run section (linetab + run data), matching the engine's data
 * advance. We model a full-frame sprite (clip offsets 0, clipped == full). */
static size_t encode(const unsigned char *src, int w, int h, unsigned char *destbuf)
{
    int x, x0, y;
    int *linetab = NULL;
    unsigned char *data;
    const unsigned char *s = src;

    if (destbuf) {
        linetab = (int *)destbuf;
        data = (unsigned char *)(linetab + h);
    } else {
        /* size pass: track only the advancing data pointer relative to a base */
        linetab = NULL;
        data = (unsigned char *)0; /* used purely as an offset counter */
    }

    for (y = 0; y < h; y++, s += w) {
        if (destbuf) linetab[y] = (int)(((size_t)data) - ((size_t)(linetab + y)));
        x = 0;
        for (;;) {
            /* first visible pixel (clearcount run, capped 0xFE) */
            x0 = x;
            for (; (x < w) && ((x - x0) < 0xFE); x++) if (s[x]) break;
            if (x >= w) {                       /* EOL */
                if (destbuf) *data = 0xFF;
                data++;
                break;
            }
            if (destbuf) *data = (unsigned char)(x - x0); /* clearcount */
            data++;
            if (!s[x]) {                        /* still transparent: null viscount */
                if (destbuf) *data = 0;
                data++;
                continue;
            }
            /* first invisible pixel (viscount run, capped 0xFF) */
            x0 = x;
            for (; (x < w) && ((x - x0) < 0xFF); x++) if (!s[x]) break;
            if (destbuf) {
                *data++ = (unsigned char)(x - x0);
                memcpy(data, s + x0, (size_t)(x - x0));
                data += x - x0;
            } else {
                data += 1 + (x - x0);
            }
        }
    }
    return (size_t)data; /* size pass: bytes; fill pass: end offset (incl linetab base 0) */
}

/* Build a realistic character-sprite alpha pattern: opaque ellipse(s) on a
 * transparent field. Real sprites have contiguous transparent borders + opaque
 * blobs (NOT random per-pixel), which is what drives encodesprite's run lengths.
 * coverage ~= fraction opaque. */
static void make_sprite(unsigned char *buf, int w, int h, double coverage, unsigned seed)
{
    int i, x, y;
    for (i = 0; i < w*h; i++) buf[i] = 0;
    /* target opaque pixels */
    long want = (long)(coverage * w * h);
    long have = 0;
    unsigned s = seed ? seed : 1;
    /* scatter a few filled ellipses until coverage met (blobby, run-friendly) */
    while (have < want) {
        s = s*1103515245u + 12345u;
        int cx = (int)((s>>9) % w);
        s = s*1103515245u + 12345u;
        int cy = (int)((s>>9) % h);
        s = s*1103515245u + 12345u;
        int rx = 6 + (int)((s>>9) % (w/4 + 1));
        s = s*1103515245u + 12345u;
        int ry = 6 + (int)((s>>9) % (h/4 + 1));
        for (y = cy-ry; y <= cy+ry; y++) {
            if (y < 0 || y >= h) continue;
            for (x = cx-rx; x <= cx+rx; x++) {
                if (x < 0 || x >= w) continue;
                double dx = (double)(x-cx)/rx, dy = (double)(y-cy)/ry;
                if (dx*dx + dy*dy <= 1.0) {
                    if (!buf[y*w+x]) { buf[y*w+x] = (unsigned char)(1 + ((x+y) & 0xFE)); have++; }
                }
            }
        }
    }
}

static long real_opaque(const unsigned char*b,int n){ long c=0,i; for(i=0;i<n;i++) if(b[i]) c++; return c; }

int main(int argc, char **argv)
{
    int W = (argc>1)?atoi(argv[1]):64;     /* typical character-frame cell */
    int H = (argc>2)?atoi(argv[2]):96;
    int N = (argc>3)?atoi(argv[3]):4000;   /* sprites per measure (PAK-ish volume) */
    int F = 3, r, f, k;
    size_t px = (size_t)W*H;

    unsigned char *spr = malloc(px);
    unsigned char *dst = malloc(px*2 + (size_t)H*8 + 64); /* generous worst-case */

    double cov[3] = { 0.25, 0.50, 0.75 };
    const char *covn[3] = { "25% opaque (sparse)", "50% opaque (typical)", "75% opaque (dense)" };

    printf("== rle_encode_bench (A9) sprite=%dx%d, %d sprites/measure ==\n", W, H, N);
    printf("encodesprite faithful port. size-pass -> [LOAD] 'other'; fill-pass -> [LOAD] 'encode'.\n");
    printf("%-22s %8s %8s %8s %8s\n", "alpha pattern", "size", "fill", "size+fill", "ns/src-px");
    printf("%-22s %8s %8s %8s %8s\n", "", "ns/px", "ns/px", "ns/px", "(merged)");

    for (k = 0; k < 3; k++) {
        make_sprite(spr, W, H, cov[k], 12345u + 7919u*k);
        long op = real_opaque(spr, (int)px);

        double bs=1e30, bf=1e30, bm=1e30;
        for (r = 0; r < F; r++) {
            double t0 = now_ns();
            for (f = 0; f < N; f++) encode(spr, W, H, NULL);        /* size pass */
            double dt = now_ns()-t0; if (dt<bs) bs=dt;

            t0 = now_ns();
            for (f = 0; f < N; f++) encode(spr, W, H, dst);         /* fill pass */
            dt = now_ns()-t0; if (dt<bf) bf=dt;

            /* merged = one fill pass only (size pass eliminated). same call, but
             * counted once to show the ceiling of a single-pass refactor. */
            t0 = now_ns();
            for (f = 0; f < N; f++) encode(spr, W, H, dst);
            dt = now_ns()-t0; if (dt<bm) bm=dt;
        }
        double per = (double)N*px;
        printf("%-22s %8.3f %8.3f %8.3f %8.3f   (%.0f%% real opaque)\n",
               covn[k], bs/per, bf/per, (bs+bf)/per, bm/per,
               100.0*(double)op/(double)px);
    }

    printf("\nRead: size+fill = current engine cost (split across 'other'+'encode').\n");
    printf("      merged = single-pass ceiling. headroom from dropping the size scan\n");
    printf("      ~= the 'size ns/px' column. Multiply size+fill by (sum of clipped\n");
    printf("      w*h over all PAK sprites) to estimate RLE's share of [LOAD].\n");
    free(spr); free(dst);
    return 0;
}
