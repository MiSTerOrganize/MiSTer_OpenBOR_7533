/*
 * gif_decode_bench.c -- pure-CPU throughput floor of OpenBOR's GIF LZW decoder
 * (engine/source/gamelib/loadimg.c::decodegifblock, v7533), so we can tell
 * whether the [LOAD] "decode" bucket is LZW-CPU-bound or pak-I/O-bound.
 *
 * WHY THIS MATTERS: in the engine, decodegifblock interleaves LZW decode with
 * readpackfile() I/O (it pulls the next compressed sub-block from the .pak on
 * the fly). So the [LOAD] "decode" bucket = LZW-CPU + pak/SD I/O, fused. This
 * bench runs the SAME decoder from an in-memory buffer (zero I/O) to isolate the
 * LZW-CPU floor. Compare:
 *    engine decode ns/px (from deployed [LOAD] breakdown / total px)
 *    vs this bench's LZW ns/px
 *  - engine >> bench  -> I/O-bound: win is reading the whole pak entry into RAM
 *                        first / bigger readpackfile buffers, NOT faster LZW.
 *  - engine ~= bench  -> CPU-bound: faster LZW (table layout, fewer stack walks)
 *                        is the lever.
 *
 * Faithful: the decoder below is a 1:1 port of decodegifblock's bit-unpacking,
 * code-stack reconstruction, and output write, with readbyte/readpackfile backed
 * by an in-memory cursor instead of a pak handle. A canonical GIF-LZW encoder
 * (Welch/Thomas, the 30-year-standard pair for this decoder) produces a real
 * compressed stream; a self-verify (decoded == original) guards correctness.
 *
 * Build (CI bench.yml): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9
 *   -mfpu=neon -mfloat-abi=hard tools/gif_decode_bench.c -o gif_decode_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

/* ============================ in-memory "pak" ============================= */
typedef struct { const unsigned char *d; size_t pos, len; } memfile;
static int m_readbyte(memfile *m){ return (m->pos < m->len) ? m->d[m->pos++] : -1; }
static int m_readpack(memfile *m, unsigned char *buf, int n){
    int avail = (int)(m->len - m->pos);
    if (n > avail) n = avail;
    if (n > 0) memcpy(buf, m->d + m->pos, (size_t)n);
    m->pos += (size_t)n;
    return n;
}

typedef struct { int left, top, width, height, flags; } gifblock;

/* ===== faithful port of decodegifblock (loadimg.c v7533), I/O from memfile ===== */
static int decodegifblock_mem(memfile *mf, unsigned char *buf, int width, int height,
                              unsigned char bits, gifblock *gb)
{
    short bits2, codesize, codesize2, nextcode, thiscode, oldtoken, currentcode, oldcode, bitsleft, blocksize;
    int line = 0, byte = gb->left, pass = 0;
    unsigned char *p, *u, *q, b[255], *linebuffer;
    static unsigned char firstcodestack[4096], lastcodestack[4096];
    static short codestack[4096];
    static short wordmasktable[] = {0x0000,0x0001,0x0003,0x0007,0x000f,0x001f,0x003f,0x007f,
                                    0x00ff,0x01ff,0x03ff,0x07ff,0x0fff,0x1fff,0x3fff,0x7fff};
    static short inctable[] = {8,8,4,2,0};
    static short startable[] = {0,4,2,1,0};

    p = q = b;
    bitsleft = 8;
    if (bits < 2 || bits > 8) return 0;
    bits2 = 1 << bits;
    nextcode = bits2 + 2;
    codesize2 = 1 << (codesize = bits + 1);
    oldcode = oldtoken = -1;
    linebuffer = buf + (gb->top * width);

    for (;;) {
        if (bitsleft == 8) {
            if (++p >= q && (((blocksize = (unsigned char)m_readbyte(mf)) < 1) ||
                             (q = (p = b) + m_readpack(mf, b, blocksize)) < (b + blocksize)))
                return 0;
            bitsleft = 0;
        }
        thiscode = *p;
        if ((currentcode = (codesize + bitsleft)) <= 8) {
            *p >>= codesize;
            bitsleft = currentcode;
        } else {
            if (++p >= q && (((blocksize = (unsigned char)m_readbyte(mf)) < 1) ||
                             (q = (p = b) + m_readpack(mf, b, blocksize)) < (b + blocksize)))
                return 0;
            thiscode |= *p << (8 - bitsleft);
            if (currentcode <= 16) {
                *p >>= (bitsleft = currentcode - 8);
            } else {
                if (++p >= q && (((blocksize = (unsigned char)m_readbyte(mf)) < 1) ||
                                 (q = (p = b) + m_readpack(mf, b, blocksize)) < (b + blocksize)))
                    return 0;
                thiscode |= *p << (16 - bitsleft);
                *p >>= (bitsleft = currentcode - 16);
            }
        }
        thiscode &= wordmasktable[codesize];
        currentcode = thiscode;

        if (thiscode == (bits2 + 1)) break;          /* EOI */
        if (thiscode > nextcode) return 0;           /* bad code */
        if (thiscode == bits2) {                     /* clear */
            nextcode = bits2 + 2;
            codesize2 = 1 << (codesize = (bits + 1));
            oldtoken = oldcode = -1;
            continue;
        }

        u = firstcodestack;
        if (thiscode == nextcode) {
            if (oldcode == -1) return 0;
            *u++ = (unsigned char)oldtoken;
            thiscode = oldcode;
        }
        while (thiscode >= bits2) {
            *u++ = lastcodestack[thiscode];
            thiscode = codestack[thiscode];
        }
        oldtoken = thiscode;
        do {
            if (byte < width && line < (height - gb->top)) linebuffer[byte] = (unsigned char)thiscode;
            byte++;
            if (byte >= gb->left + gb->width) {
                byte = gb->left;
                if (gb->flags & 0x40) {
                    line += inctable[pass];
                    if (line >= gb->height) line = startable[++pass];
                } else ++line;
                linebuffer = buf + (width * (gb->top + line));
            }
            if (u <= firstcodestack) break;
            thiscode = *--u;
        } while (1);

        if (nextcode < 4096 && oldcode != -1) {
            codestack[nextcode] = oldcode;
            lastcodestack[nextcode] = (unsigned char)oldtoken;
            if (++nextcode >= codesize2 && codesize < 12) codesize2 = 1 << ++codesize;
        }
        oldcode = currentcode;
    }
    return 1;
}

/* ============== canonical GIF-LZW encoder (Welch/Thomas), to memory ============== */
#define HSIZE 5003
#define GIF_MAXBITS 12
static long  htab[HSIZE];
static unsigned short codetab[HSIZE];
static int n_bits, maxcode, free_ent, clear_flg, g_init_bits, ClearCode, EOFCode;
static unsigned long cur_accum; static int cur_bits;
static unsigned char accbuf[256]; static int a_count;
static unsigned char *g_out; static size_t g_outn;
static unsigned long lzw_masks[] = {0x0000,0x0001,0x0003,0x0007,0x000f,0x001f,0x003f,0x007f,
                                    0x00ff,0x01ff,0x03ff,0x07ff,0x0fff,0x1fff,0x3fff,0x7fff,0xffff};
#define MAXCODE(n) ((1 << (n)) - 1)

static void flush_char(void){ if (a_count){ *g_out++ = (unsigned char)a_count; memcpy(g_out, accbuf, (size_t)a_count); g_out += a_count; g_outn += 1 + (size_t)a_count; a_count = 0; } }
static void char_out(int c){ accbuf[a_count++] = (unsigned char)c; if (a_count >= 254) flush_char(); }
static void output(int code){
    cur_accum &= lzw_masks[cur_bits];
    if (cur_bits > 0) cur_accum |= ((unsigned long)code << cur_bits);
    else cur_accum = (unsigned long)code;
    cur_bits += n_bits;
    while (cur_bits >= 8){ char_out((int)(cur_accum & 0xff)); cur_accum >>= 8; cur_bits -= 8; }
    if (free_ent > maxcode || clear_flg) {
        if (clear_flg) { maxcode = MAXCODE(n_bits = g_init_bits); clear_flg = 0; }
        else { ++n_bits; maxcode = (n_bits == GIF_MAXBITS) ? (1 << GIF_MAXBITS) : MAXCODE(n_bits); }
    }
    if (code == EOFCode) { while (cur_bits > 0){ char_out((int)(cur_accum & 0xff)); cur_accum >>= 8; cur_bits -= 8; } flush_char(); }
}
/* init_bits = mincodesize+1. Returns compressed sub-block stream length in g_outn. */
static size_t gif_lzw_encode(int init_bits, const unsigned char *data, long len, unsigned char *out)
{
    long fcode; int i, c, ent, disp, hshift; long pos;
    g_out = out; g_outn = 0; a_count = 0; cur_accum = 0; cur_bits = 0;
    g_init_bits = init_bits; n_bits = g_init_bits; maxcode = MAXCODE(n_bits); clear_flg = 0;
    ClearCode = 1 << (init_bits - 1); EOFCode = ClearCode + 1; free_ent = ClearCode + 2;
    for (hshift = 0, fcode = HSIZE; fcode < 65536L; fcode *= 2L) ++hshift; hshift = 8 - hshift;
    for (i = 0; i < HSIZE; i++) htab[i] = -1;
    output(ClearCode);
    ent = data[0];
    for (pos = 1; pos < len; pos++) {
        c = data[pos];
        fcode = ((long)c << GIF_MAXBITS) + ent;
        i = (c << hshift) ^ ent;
        if (htab[i] == fcode) { ent = codetab[i]; continue; }
        if (htab[i] >= 0) {
            disp = HSIZE - i; if (i == 0) disp = 1;
            for (;;) {
                if ((i -= disp) < 0) i += HSIZE;
                if (htab[i] == fcode) { ent = codetab[i]; break; }
                if (htab[i] < 0) goto nomatch;
            }
            continue;
        }
    nomatch:
        output(ent);
        ent = c;
        if (free_ent < (1 << GIF_MAXBITS)) { codetab[i] = (unsigned short)free_ent++; htab[i] = fcode; }
        else { for (i = 0; i < HSIZE; i++) htab[i] = -1; free_ent = ClearCode + 2; clear_flg = 1; output(ClearCode); }
    }
    output(ent);
    output(EOFCode);
    return g_outn;
}

/* realistic 8-bit sprite-art-ish image: solid color blocks + structured detail
 * (limited palette, run-friendly) -> moderate ~2-4x compression, not a trivial
 * gradient and not white noise. */
static void make_image(unsigned char *img, int w, int h)
{
    int x, y; unsigned s = 2463534242u;
    for (y = 0; y < h; y++) for (x = 0; x < w; x++) {
        int bx = x >> 4, by = y >> 4;            /* 16x16 color blocks */
        unsigned base = (unsigned)((bx*7 + by*13) & 0x3F);
        s ^= s << 13; s ^= s >> 17; s ^= s << 5; /* xorshift detail */
        unsigned d = (s & 7);                     /* small per-pixel variation */
        img[y*w + x] = (unsigned char)((base + d) & 0xFF);
    }
}

int main(int argc, char **argv)
{
    int W = (argc>1)?atoi(argv[1]):256;
    int H = (argc>2)?atoi(argv[2]):256;
    int REP = (argc>3)?atoi(argv[3]):2000;
    int bits = 8, r, f;
    long npx = (long)W*H;

    unsigned char *img = malloc((size_t)npx);
    unsigned char *comp = malloc((size_t)npx * 2 + 4096);   /* worst case > raw */
    unsigned char *dec  = malloc((size_t)npx);

    make_image(img, W, H);
    size_t clen = gif_lzw_encode(bits + 1, img, npx, comp);

    gifblock gb = { 0, 0, W, H, 0 };

    /* self-verify the encoder/decoder pair before trusting timings */
    memset(dec, 0xAB, (size_t)npx);
    memfile mf = { comp, 0, clen };
    int ok = decodegifblock_mem(&mf, dec, W, H, (unsigned char)bits, &gb);
    int match = ok && (memcmp(dec, img, (size_t)npx) == 0);

    printf("== gif_decode_bench (A9) image=%dx%d (%ld px, 8-bit) ==\n", W, H, npx);
    printf("compressed: %zu bytes  ratio %.2fx  self-verify: %s\n",
           clen, (double)npx/(double)clen, match ? "PASS" : "*** FAIL ***");
    if (!match) { printf("encoder/decoder mismatch -- timings suppressed.\n"); free(img); free(comp); free(dec); return 1; }

    double best = 1e30;
    for (r = 0; r < 3; r++) {
        double t0 = now_ns();
        for (f = 0; f < REP; f++) {
            mf.pos = 0; mf.len = clen;
            decodegifblock_mem(&mf, dec, W, H, (unsigned char)bits, &gb);
        }
        double dt = now_ns() - t0;
        if (dt < best) best = dt;
    }
    double ns_per_px = best / ((double)REP * npx);
    double out_mbs   = (double)REP * npx / (best / 1e9) / 1e6;          /* decoded bytes/s */
    double comp_mbs  = (double)REP * (double)clen / (best / 1e9) / 1e6; /* compressed bytes consumed/s */

    printf("LZW decode (CPU floor, zero I/O): %.3f ns/output-px\n", ns_per_px);
    printf("  output throughput   : %7.1f MB/s (decoded index bytes)\n", out_mbs);
    printf("  compressed throughput: %6.1f MB/s (bytes pulled from the stream)\n", comp_mbs);
    printf("\nRead: this is the LZW CPU floor with NO pak/SD I/O. Compare vs the deployed\n");
    printf("      [LOAD] 'decode' ns/px. engine>>floor => I/O-bound (decompress pak to RAM\n");
    printf("      / bigger read buffers); engine~=floor => LZW-CPU-bound (faster decode).\n");
    free(img); free(comp); free(dec);
    return 0;
}
