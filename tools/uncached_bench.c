/*
 * uncached_bench.c -- Tier-B C2 go/no-go: how much slower is reading sprite RLE
 * out of an UNCACHED DDR3 arena than out of cached (malloc'd) memory?
 *
 * WHY THIS DECIDES THE ARCHITECTURE
 * Tier-B proposes relocating every sprite's RLE into a reserved DDR3 region so
 * the FPGA can fetch it. The ARM reaches that region the same way
 * native_video_writer.c reaches the framebuffer: open("/dev/mem", O_RDWR|O_SYNC)
 * + mmap(MAP_SHARED) == strongly-ordered, UNCACHED. Today that is fine because
 * the traffic is write-only streaming. After Tier-B the CPU must still READ that
 * same memory for the ~29% of blits that stay on the CPU (gfx_draw_scale and
 * friends). The f2h SDRAM ports are not coherent with A9 L1/L2 and ARMv7
 * userspace cannot issue cache maintenance, so "just map it cached" silently
 * feeds the FPGA stale bytes. If uncached reads are as slow as feared, the
 * fallback gets slower by more than the offload saves and the arena idea dies.
 *
 * WHAT IT MEASURES
 * The access pattern that matters is the RLE walk: a byte-at-a-time sequential
 * scan with a per-row seek (sprite.c encodesprite emits linetab[h] then rows of
 * clearcount/viscount/pixels). Three patterns are timed against both mappings:
 *   seq   -- pure sequential byte walk        (best case for a prefetcher)
 *   rle   -- realistic RLE decode walk        (the one that decides C2)
 *   rows  -- linetab-style random row seeks   (worst case)
 *
 * SAFETY -- WHERE THE UNCACHED BUFFER LIVES
 * It maps the core's OWN cart-staging region at 0x3A080000, which
 * openbor_video_reader.sv uses only while an ioctl cart download is in flight.
 * The audio ring starts at 0x3A0D0000, so 256 KB from 0x3A080000 stays clear of
 * it and of all three framebuffers (0x3A000040 / 0x3A028040 / 0x3A050040). REFUSES TO RUN if an
 * OpenBOR binary is live, so nothing can be using it. It does NOT touch
 * 0x30000000 -- that region's extent is still an unverified Tier-B open item.
 *
 * BUILD  (bench.yml globs tools/*_bench.c)
 * RUN    ./uncached_bench          -- must be root for /dev/mem
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <malloc.h>
#include <string.h>
#include <stdint.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/mman.h>
#include <time.h>
#include <signal.h>
#include <setjmp.h>

#define ARENA_PHYS   0x3A080000UL   /* cart staging -- idle when no cart loads  */
#define ARENA_BYTES  (256u * 1024u) /* clear of the audio ring at 0x3A0D0000    */
#define ROW_W        320            /* representative sprite row width          */
#define REPS         24

static double now_ms(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1000.0 + t.tv_nsec / 1e6;
}

/* Build a plausible RLE stream: linetab of int32 self-relative offsets, then
 * rows of [clearcount][viscount][pixels...] terminated by 0xFF -- the layout
 * sprite.c::encodesprite actually emits. */
static unsigned build_rle(volatile unsigned char *buf, unsigned bytes, unsigned rows)
{
    /* EVERYTHING stays volatile. Casting volatile away here is what made v3
     * still SIGBUS: with -O2 -mfpu=neon GCC vectorises a 16-byte store loop
     * into a wide UNALIGNED store, which faults on strongly-ordered memory
     * (proved by probe_alignment). volatile forces byte-at-a-time stores. */
    volatile unsigned char *b = buf;
    volatile int32_t *linetab = (volatile int32_t *)b;   /* base is page-aligned */
    volatile unsigned char *d = b + rows * sizeof(int32_t);
    unsigned r, x;

    for (r = 0; r < rows; r++) {
        {   /* aligned u32 store is legal; keep it explicit */
            int32_t off = (int32_t)((size_t)d - (size_t)&linetab[r]);
            linetab[r] = off;
        }
        for (x = 0; x + 24 < ROW_W;) {
            if ((size_t)(d - b) + 32 >= bytes) break;
            unsigned k;
            *d++ = 8;                      /* clearcount: 8 transparent   */
            *d++ = 16;                     /* viscount:   16 opaque       */
            /* byte-wise: memset is ILLEGAL on strongly-ordered memory when
             * unaligned (see probe_alignment) */
            for (k = 0; k < 16; k++) *d++ = (unsigned char)((r ^ x ^ k) & 0xFE);
            x += 24;
        }
        if ((size_t)(d - b) + 2 >= bytes) { rows = r + 1; break; }
        *d++ = 0xFF;                       /* end of row                  */
    }
    return rows;
}

/* --- alignment probe -------------------------------------------------------
 * The first run of this bench died with SIGBUS while BUILDING the stream in the
 * uncached mapping. On ARM, /dev/mem + O_SYNC gives strongly-ordered (device)
 * memory, and unaligned multi-byte access to device memory FAULTS -- so libc
 * memset/memcpy, which happily use unaligned word/NEON stores, are illegal
 * there. That is a Tier-B finding in its own right: `encodesprite` builds a
 * sprite with `memcpy(data, src + x0, x - x0)` at arbitrary byte offsets, so it
 * cannot write into an uncached arena as-is.
 * This probe reports exactly which access classes survive. */
static volatile sig_atomic_t g_sigbus;
static sigjmp_buf g_jb;
static void on_bus(int sig) { (void)sig; g_sigbus = 1; siglongjmp(g_jb, 1); }

#define TRY(label, stmt)                                                      \
    do {                                                                      \
        g_sigbus = 0;                                                         \
        if (sigsetjmp(g_jb, 1) == 0) { stmt; printf("  %-34s OK\n", label); }  \
        else printf("  %-34s SIGBUS\n", label);                               \
    } while (0)

static void probe_alignment(volatile unsigned char *u)
{
    struct sigaction sa, old;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_bus;
    sigaction(SIGBUS, &sa, &old);

    printf("uncached access probe (/dev/mem O_SYNC = strongly-ordered):\n");
    TRY("byte store, any offset",        *(volatile unsigned char *)(u + 3) = 0xA5);
    TRY("u32 store, 4-byte aligned",     *(volatile uint32_t *)(u + 8) = 0x12345678u);
    TRY("u32 store, UNALIGNED (+1)",     *(volatile uint32_t *)(u + 9) = 0x12345678u);
    TRY("memset 16 B, aligned",          memset((void *)(u + 64), 0x5A, 16));
    TRY("memset 16 B, UNALIGNED (+1)",   memset((void *)(u + 65), 0x5A, 16));
    TRY("memcpy 16 B, UNALIGNED (+1)",   { unsigned char t[16]; memcpy((void *)(u + 97), t, 16); });
    TRY("byte loop 16 B, unaligned",     { int i; for (i = 0; i < 16; i++) u[129 + i] = (unsigned char)i; });
    printf("\n");

    sigaction(SIGBUS, &old, NULL);
}

/* Walk it the way the decoder does. volatile so nothing is optimised away. */
static uint32_t walk_rle(volatile unsigned char *buf, unsigned rows)
{
    volatile int32_t *linetab = (volatile int32_t *)buf;
    uint32_t sum = 0;
    unsigned r;

    for (r = 0; r < rows; r++) {
        volatile unsigned char *p =
            (volatile unsigned char *)((size_t)&linetab[r] + (size_t)linetab[r]);
        for (;;) {
            unsigned char cc = *p++;
            if (cc == 0xFF) break;
            unsigned char vc = *p++;
            while (vc--) sum += *p++;
        }
    }
    return sum;
}

static uint32_t walk_seq(volatile unsigned char *buf, unsigned bytes)
{
    uint32_t sum = 0;
    unsigned i;
    for (i = 0; i < bytes; i++) sum += buf[i];
    return sum;
}

static uint32_t walk_rows(volatile unsigned char *buf, unsigned rows)
{
    volatile int32_t *linetab = (volatile int32_t *)buf;
    uint32_t sum = 0;
    unsigned k, r;
    /* stride by a large odd step so consecutive seeks land far apart */
    for (k = 0, r = 0; k < rows * 4; k++) {
        r = (r + 37) % rows;
        volatile unsigned char *p =
            (volatile unsigned char *)((size_t)&linetab[r] + (size_t)linetab[r]);
        sum += p[0] + p[1] + p[2] + p[3];
    }
    return sum;
}

static double best_of(double (*fn)(volatile unsigned char *, unsigned),
                      volatile unsigned char *buf, unsigned arg)
{
    double best = 1e30;
    int i;
    for (i = 0; i < REPS; i++) {
        double t = fn(buf, arg);
        if (t < best) best = t;
    }
    return best;
}

static volatile uint32_t g_sink;
static unsigned g_rows;
static unsigned g_bytes;

static double t_seq(volatile unsigned char *b, unsigned n)
{ double t0 = now_ms(); g_sink = walk_seq(b, n); return now_ms() - t0; }
static double t_rle(volatile unsigned char *b, unsigned n)
{ double t0 = now_ms(); g_sink = walk_rle(b, n); return now_ms() - t0; }
static double t_rows(volatile unsigned char *b, unsigned n)
{ double t0 = now_ms(); g_sink = walk_rows(b, n); return now_ms() - t0; }

int main(void)
{
    /* Line-buffer immediately: the first two runs died with SIGBUS and their
     * block-buffered stdout was LOST, so nothing showed where they died. */
    setvbuf(stdout, NULL, _IOLBF, 0);
    setvbuf(stderr, NULL, _IOLBF, 0);

    /* Refuse to run while a hybrid binary could be using the region. */
    if (system("pidof OpenBOR_7533 >/dev/null 2>&1 || pidof OpenBOR_4086 >/dev/null 2>&1") == 0) {
        fprintf(stderr, "REFUSING: an OpenBOR binary is running; it may own 0x%lX.\n"
                        "Switch the MiSTer to the MENU core and re-run.\n", ARENA_PHYS);
        return 2;
    }

    fprintf(stderr, "[bc] start\n");
    g_bytes = ARENA_BYTES;
    g_rows  = g_bytes / (ROW_W + 64);
    if (g_rows < 16) g_rows = 16;

    /* --- cached reference: ordinary malloc --- */
    fprintf(stderr, "[bc] alloc cached\n");
    unsigned char *cached = memalign(64, g_bytes);
    if (!cached) { perror("memalign"); return 1; }
    memset(cached, 0, g_bytes);
    fprintf(stderr, "[bc] build cached\n");
    unsigned rows_c = build_rle(cached, g_bytes, g_rows);

    /* --- uncached: /dev/mem, exactly how native_video_writer.c maps DDR3 --- */
    fprintf(stderr, "[bc] open /dev/mem\n");
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) { perror("open /dev/mem (run as root)"); return 1; }
    volatile unsigned char *unc = mmap(NULL, g_bytes, PROT_READ | PROT_WRITE,
                                       MAP_SHARED, fd, ARENA_PHYS);
    if (unc == MAP_FAILED) { perror("mmap"); close(fd); return 1; }

    /* MUST run before anything writes the mapping: the first version of this
     * bench died with SIGBUS inside build_rle. */
    fprintf(stderr, "[bc] mmap ok, probing\n");
    probe_alignment(unc);

    fprintf(stderr, "[bc] build uncached\n");
    unsigned rows_u = build_rle(unc, g_bytes, g_rows);

    printf("Tier-B C2 -- uncached DDR3 arena vs cached malloc\n");
    printf("buffer %u KB, %u rows, best of %d\n", g_bytes / 1024, rows_c, REPS);
    printf("uncached mapping: /dev/mem O_SYNC MAP_SHARED @ 0x%lX\n\n", ARENA_PHYS);
    printf("%-26s %10s %10s %9s\n", "pattern", "cached ms", "uncach ms", "slowdown");
    printf("%s\n", "---------------------------------------------------------------");

    double c, u;
    c = best_of(t_seq,  cached, g_bytes); u = best_of(t_seq,  unc, g_bytes);
    printf("%-26s %10.3f %10.3f %8.1fx\n", "sequential byte scan", c, u, u / c);
    c = best_of(t_rle,  cached, rows_c);  u = best_of(t_rle,  unc, rows_u);
    printf("%-26s %10.3f %10.3f %8.1fx  <== decides C2\n", "RLE decode walk", c, u, u / c);
    c = best_of(t_rows, cached, rows_c);  u = best_of(t_rows, unc, rows_u);
    printf("%-26s %10.3f %10.3f %8.1fx\n", "linetab row seeks", c, u, u / c);

    printf("\nInterpretation: the RLE row is what gfx_draw_scale does for the ~29%% of\n");
    printf("blits that stay on the CPU. A slowdown near 1x means the arena is safe;\n");
    printf("10x+ means the fallback loses more than the offload gains and C2 is a NO-GO.\n");

    munmap((void *)unc, g_bytes);
    close(fd);
    free(cached);
    (void)g_sink;
    return 0;
}
