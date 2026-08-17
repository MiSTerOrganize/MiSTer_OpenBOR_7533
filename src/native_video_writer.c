//
//  Native Video DDR3 Writer — OpenBOR MiSTer
//
//  Writes 320x224 RGB565 frames to DDR3 at 0x3A000000 for FPGA native
//  video output. Double-buffered with control word handshake.
//
//  DDR3 Memory Map (must match openbor_video_reader.sv):
//    0x3A000000 + 0x00000   : Control word (frame_counter[31:2] | active_buf[1:0])
//    0x3A000000 + 0x00008   : Joystick P1 (32 bits)
//    0x3A000000 + 0x00010   : Cart control (file_size from FPGA)
//    0x3A000000 + 0x00018   : Joystick P2 (32 bits)
//    0x3A000000 + 0x00020   : Joystick P3 (32 bits)
//    0x3A000000 + 0x00028   : Joystick P4 (32 bits)
//    0x3A000000 + 0x00040   : Buffer 0 (320*224*2 = 143,360 bytes)
//    0x3A000000 + 0x40040   : Buffer 1
//    0x3A000000 + 0x80000   : Cart data (PAK file from OSD)
//
//    TWO buffers, stride 0x40000. 🛑 These offsets are mirrored in
//    patch_sdl_dummy.py and in the RTL's BUF*_ADDR localparams -- change all
//    three together or the FPGA reads the wrong region and shows garbage.
//
//    A THIRD buffer was implemented and reverted on 2026-08-06. It was aimed at
//    the overlay flicker -- a core rendering faster than the panel scans (ATOV
//    ~166 fps, JL Legacy ~200, against 59.92 Hz) laps the reader and must write
//    into the buffer the FPGA has latched, so the FPGA can scan the window
//    between the game-pixel write and DrawOverlays and the overlay is simply
//    absent for that frame. It does not fix that: buf_base_addr is latched once
//    per vblank in the RTL and held for the whole scan, so N buffers are safe
//    only below (N-1) x 59.9 fps -- 119.8 at N=3, which BOTH of those PAKs
//    exceed. The flicker is fixed instead by making the notice STATIC (below):
//    written once per buffer and then never rewritten, so nothing can catch it
//    half-written at any frame rate or buffer count. See #TRIPLE_BUFFER_SCOPE.md.
//
//  Copyright (C) 2026 MiSTer Organize — GPL-3.0
//

#ifndef _GNU_SOURCE
#define _GNU_SOURCE   /* 2026-06-07: sched_setaffinity / cpu_set_t for render-thread core pin */
#endif
#include "native_video_writer.h"

#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
#include <stdint.h>
#include <time.h>
/* Step 20 (2026-05-27): NEON intrinsics for 128-bit DDR3 stores in the
 * no-squish fast path of WriteFrame. Cortex-A9 + -mfpu=neon -mfloat-abi=hard
 * build flags (see CLAUDE.md OpenBOR build config) guarantee NEON support. */
#include <arm_neon.h>

#define NV_DDR_PHYS_BASE    0x3A000000u
#define NV_DDR_REGION_SIZE  0x00100000u   /* 1MB covers buffers + control + cart data */
#define NV_CTRL_OFFSET      0x00000000u
/* OSD replay-slot transport. Both words ride in DEAD SPACE inside qwords the
 * FPGA already moves every frame, so neither direction costs a new DDR3
 * transaction:
 *   0x04 = spare upper half of the CONTROL qword (the reader takes only
 *          ddr_dout[31:0] of it) -- ARM -> FPGA, byte0=slot, byte1=seq.
 *   0x0C = spare upper half of the JOY0 qword (the FPGA wrote {32'd0, joy})
 *          -- FPGA -> ARM, byte0=cmd, byte1=slot, byte2=seq.
 * Slots are 0-BASED on the wire (0..7); the user-facing 1..8 conversion
 * happens at this boundary and nowhere else.
 *
 * 🛑 0x04 is deliberately NOT zeroed in Init, and that is not an oversight.
 * The FPGA is already running and polling before the ARM binary starts, so a
 * zeroing write would look to replay_slot_ui like a pause-menu change to
 * slot 1 and CLOBBER the OSD slot the user had persisted in the core's .cfg.
 *
 * 🛑 An earlier version of this note claimed "the RTL's own `armed` guard
 * covers the stale-DDR3 case instead." IT DID NOT. That guard released on the
 * first clock edge, roughly 1.7 million cycles before arm_seq ever arrives
 * from DDR3, so the stale word was adopted every time -- measured on hardware
 * at 0xAABBBFFA, which pushed "Slot 3" into the OSD on every core load. The
 * guard now waits for the reader's arm_valid strobe, which makes that first
 * DDR3 sample the BASELINE rather than a command. Not-zeroing is correct, but
 * it was never what made it safe; arm_valid is. See replay_slot_ui.sv.
 *
 * The sequence byte is likewise never seeded from a process-local counter --
 * PublishReplaySlot derives it from what is already on the wire, because 0x04
 * survives the _exit()/respawn that Record and Play both perform. */
#define NV_REPLAY_PUB_OFFSET 0x00000004u  /* ARM -> FPGA */
#define NV_JOY0_OFFSET      0x00000008u
#define NV_REPLAY_OFFSET    0x0000000Cu   /* FPGA -> ARM */
#define NV_CART_CTRL_OFFSET  0x00000010u
#define NV_JOY1_OFFSET      0x00000018u
#define NV_JOY2_OFFSET      0x00000020u
#define NV_JOY3_OFFSET      0x00000028u
#define NV_BUF0_OFFSET      0x00000040u
#define NV_BUF1_OFFSET      0x00040040u
#define NV_CART_DATA_OFFSET  0x00080000u
#define NV_CART_MAX_SIZE     0x00040000u  /* 256KB max PAK size via OSD */
#define NV_FRAME_WIDTH      320
#define NV_FRAME_HEIGHT     224   /* Sega CD V28 NTSC */
#define NV_FRAME_BYTES      (NV_FRAME_WIDTH * NV_FRAME_HEIGHT * 2)  /* 143,360 */

static const uint32_t joy_offsets[4] = {
    NV_JOY0_OFFSET, NV_JOY1_OFFSET, NV_JOY2_OFFSET, NV_JOY3_OFFSET
};

static int mem_fd = -1;
static volatile uint8_t* ddr_base = NULL;
static uint32_t frame_counter = 0;
static int active_buf = 0;

/* The buffer the keepalive thread may republish. Written by WriteFrame ONLY
 * once that buffer is completely drawn, so a keepalive tick can never point the
 * FPGA at a buffer WriteFrame is still filling.
 *
 * 🛑 Do NOT go back to deriving this in the keepalive as `(!active_buf)`. That
 * was the notice-flicker root cause (measured 2026-08-05): the keepalive runs on
 * its own pthread, so between its read of active_buf and its ctrl write,
 * WriteFrame could toggle active_buf -- and the keepalive then published the
 * buffer WriteFrame was ABOUT TO WRITE INTO. The FPGA showed an undrawn buffer:
 * no notice, and no publisher tag either, which is how it was identified.
 *
 * Sharing frame_counter/active_buf between the threads (the 2026-05-22
 * loading-bar-jitter fix) narrowed this race but did not remove it -- shared
 * state is not synchronised state. */
static volatile int nv_last_published = 0;

bool NativeVideoWriter_Init(void) {
    /* 2026-06-13 affinity INVERSION: pin this (engine/render/main) thread to core 0.
     * mem_bench shows core 0 has ~1.85x core 1's DDR3 read bandwidth; the sprite
     * blend/render pass is memory-bound, so render belongs on the bandwidth-fast
     * core 0 (validated +81% on He-Man). Audio moves to core 1 (sblaster_patch.c).
     * The handler launches with taskset 0x03 (both cores). Init runs once at startup
     * on the main thread, so this pins the render thread. */
    {
        cpu_set_t _cs;
        CPU_ZERO(&_cs);
        CPU_SET(0, &_cs);
        if (sched_setaffinity(0, sizeof(_cs), &_cs) != 0) {
            perror("NativeVideoWriter: sched_setaffinity core 0");
        } else {
            fprintf(stderr, "NativeVideoWriter: render thread pinned to core 0\n");
        }
    }

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

    /* Clear all three buffers, control words, AND all per-player joystick
     * offsets. Cart's frame-0 reads stale DDR3 from previous core if
     * Init doesn't zero everything the engine polls. OpenBOR currently
     * uses btn() (held state) more than btn_pressed() so the symptom
     * doesn't surface, but matches the universal hybrid-core rule. */
    memset((void*)(ddr_base + NV_BUF0_OFFSET), 0, NV_FRAME_BYTES);
    memset((void*)(ddr_base + NV_BUF1_OFFSET), 0, NV_FRAME_BYTES);
    volatile uint32_t* ctrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
    *ctrl = 0;
    volatile uint32_t* cart_ctrl = (volatile uint32_t*)(ddr_base + NV_CART_CTRL_OFFSET);
    *cart_ctrl = 0;
    for (int i = 0; i < 4; i++) {
        *(volatile uint32_t*)(ddr_base + joy_offsets[i]) = 0;
    }
    frame_counter = 0;
    active_buf = 0;
    nv_last_published = 0;

    /* Both buffers were just zeroed, so any notice band painted into them went
     * with it. Drop the message (a re-init is a fresh start) and record that
     * neither buffer holds a band any more -- otherwise the row skip would
     * protect a hole the game can never cover. NULL is the cancel path; the
     * state itself is declared further down. */
    NativeVideoWriter_Notice(NULL, 0);
    NativeVideoWriter_NoticeRepaint();

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

/* (2026-06-08) Pass-2 cross-Laplacian unsharp REMOVED. It made 7533 read
 * jaggier than 4086 (which has no sharpen) on ATOV's 1:1-X sprites/text. The
 * 32bpp downscale now ships the box area-average (PASS 1) only, packed straight
 * to RGB565 with no edge enhancement. */

/* ==========================================================================
 * FPS OVERLAY  (pause menu -> Options -> "FPS Display")
 *
 * Drawn HERE, into the final 320x224 RGB565 buffer, and deliberately NOT on
 * the engine's vscreen. A PAK renders at its own native size -- He-Man is
 * 960x480 -- and WriteFrame squishes that to 320x224. An overlay drawn engine-
 * side would be downscaled with everything else and, at 3x, become unreadable.
 * Drawing post-downscale makes it pixel-crisp and identical on every PAK.
 *
 * `mister_fps_overlay` is a global defined in openbor.c (next to `mrec_mode`)
 * so the pause menu can toggle it in both the ship and headless builds without
 * a link dependency on this translation unit.
 *
 * NOTE for the harnesses: this writes into the framebuffer, so it changes
 * screenshots and any frame-hash comparison ([DCV16] byte-identity, golden
 * traces). It does NOT affect .inp recordings -- those are input streams, and
 * replay determinism rides on inputs + RNG seed + the interval lock, none of
 * which this touches. Recording or replaying with it on is safe and is its
 * intended debugging use: an fps read-out across a whole deterministic replay.
 * Default OFF, and it resets to OFF each launch, so it can never silently
 * contaminate a byte-identity run.
 * ========================================================================== */
/* Defined in openbor.c beside mrec_mode, in BOTH builds (see apply_patches.py),
 * so this resolves wherever native_video_writer.o is linked. */
extern int mister_fps_overlay;

/* 5x7 digits, low 5 bits per row, drawn at 2x -> 10x14 px per glyph. */
static const uint8_t nv_font5x7[10][7] = {
    {0x0E,0x11,0x11,0x11,0x11,0x11,0x0E}, /* 0 */
    {0x04,0x0C,0x04,0x04,0x04,0x04,0x0E}, /* 1 */
    {0x0E,0x11,0x01,0x02,0x04,0x08,0x1F}, /* 2 */
    {0x1F,0x02,0x04,0x02,0x01,0x11,0x0E}, /* 3 */
    {0x02,0x06,0x0A,0x12,0x1F,0x02,0x02}, /* 4 */
    {0x1F,0x10,0x1E,0x01,0x01,0x11,0x0E}, /* 5 */
    {0x06,0x08,0x10,0x1E,0x11,0x11,0x0E}, /* 6 */
    {0x1F,0x01,0x02,0x04,0x08,0x08,0x08}, /* 7 */
    {0x0E,0x11,0x11,0x0E,0x11,0x11,0x0E}, /* 8 */
    {0x0E,0x11,0x11,0x0F,0x01,0x02,0x0C}, /* 9 */
};

#define NV_GLYPH_W 10   /* 5 * 2 */
#define NV_GLYPH_H 14   /* 7 * 2 */
#define NV_GLYPH_GAP 2
#define NV_FPS_MARGIN 4

/* A-Z for the notice overlay. The fps read-out only ever needed digits, so
 * until now every message this recorder emitted went to OpenBorLog.txt -- which
 * nobody reads while sitting in front of a TV holding a controller. */
static const uint8_t nv_font_az[26][7] = {
    {0x0E,0x11,0x11,0x1F,0x11,0x11,0x11}, {0x1E,0x11,0x11,0x1E,0x11,0x11,0x1E},
    {0x0E,0x11,0x10,0x10,0x10,0x11,0x0E}, {0x1E,0x11,0x11,0x11,0x11,0x11,0x1E},
    {0x1F,0x10,0x10,0x1E,0x10,0x10,0x1F}, {0x1F,0x10,0x10,0x1E,0x10,0x10,0x10},
    {0x0E,0x11,0x10,0x17,0x11,0x11,0x0F}, {0x11,0x11,0x11,0x1F,0x11,0x11,0x11},
    {0x0E,0x04,0x04,0x04,0x04,0x04,0x0E}, {0x01,0x01,0x01,0x01,0x01,0x11,0x0E},
    {0x11,0x12,0x14,0x18,0x14,0x12,0x11}, {0x10,0x10,0x10,0x10,0x10,0x10,0x1F},
    {0x11,0x1B,0x15,0x15,0x11,0x11,0x11}, {0x11,0x19,0x15,0x13,0x11,0x11,0x11},
    {0x0E,0x11,0x11,0x11,0x11,0x11,0x0E}, {0x1E,0x11,0x11,0x1E,0x10,0x10,0x10},
    {0x0E,0x11,0x11,0x11,0x15,0x12,0x0D}, {0x1E,0x11,0x11,0x1E,0x14,0x12,0x11},
    {0x0F,0x10,0x10,0x0E,0x01,0x01,0x1E}, {0x1F,0x04,0x04,0x04,0x04,0x04,0x04},
    {0x11,0x11,0x11,0x11,0x11,0x11,0x0E}, {0x11,0x11,0x11,0x11,0x11,0x0A,0x04},
    {0x11,0x11,0x11,0x15,0x15,0x1B,0x11}, {0x11,0x11,0x0A,0x04,0x0A,0x11,0x11},
    {0x11,0x11,0x0A,0x04,0x04,0x04,0x04}, {0x1F,0x01,0x02,0x04,0x08,0x10,0x1F},
};

/* Only the punctuation the messages use. */
static const uint8_t nv_font_punct[8][7] = {
    {0x00,0x00,0x00,0x0E,0x00,0x00,0x00}, /* - */
    {0x00,0x00,0x00,0x00,0x00,0x0C,0x0C}, /* . */
    {0x00,0x0C,0x0C,0x00,0x0C,0x0C,0x00}, /* : */
    {0x0E,0x11,0x01,0x02,0x04,0x00,0x04}, /* ? */
    {0x04,0x04,0x04,0x04,0x04,0x00,0x04}, /* ! */
    {0x02,0x04,0x08,0x08,0x08,0x04,0x02}, /* ( */
    {0x08,0x04,0x02,0x02,0x02,0x04,0x08}, /* ) */
    /* '%' added 2026-08-17 for the loading read-out. This font is OURS, so the
     * charset is whatever we give it -- but note the rule that a character with
     * no glyph renders as a BLANK here (not a box), which is how "this take's"
     * once shipped as "THIS TAKE S". Anything added must also be picked up by
     * notice_wrap_check.py, which derives the set from nv_glyph_rows() rather
     * than from a hardcoded list, so it tracks this automatically. */
    {0x19,0x1A,0x02,0x04,0x08,0x0B,0x13}, /* % */
};

/* NULL for space and anything unmapped: an unknown character renders as a
 * blank cell rather than dropping out, so the text keeps its shape. */
static const uint8_t* nv_glyph_rows(char c) {
    if (c >= '0' && c <= '9') return nv_font5x7[c - '0'];
    if (c >= 'A' && c <= 'Z') return nv_font_az[c - 'A'];
    if (c >= 'a' && c <= 'z') return nv_font_az[c - 'a'];
    switch (c) {
        case '-': return nv_font_punct[0];
        case '.': return nv_font_punct[1];
        case ':': return nv_font_punct[2];
        case '?': return nv_font_punct[3];
        case '!': return nv_font_punct[4];
        case '(': return nv_font_punct[5];
        case ')': return nv_font_punct[6];
        case '%': return nv_font_punct[7];
        default:  return NULL;
    }
}

/* Notices draw at 1x, not the fps read-out's 2x: 320/6 = 53 columns, which is
 * enough to name a PAK. At 2x it would be 26 and every useful message would
 * wrap to three lines. */
#define NV_COLS       52
#define NV_NOTICE_MAX 3

static int      nv_fps_value    = 0;
static uint32_t nv_fps_frames   = 0;
static uint64_t nv_fps_last_ns  = 0;

static uint64_t nv_now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

/* Recompute about twice a second: fast enough to feel live, slow enough that
 * the digits do not flicker between adjacent values while you read them. */
static void nv_fps_tick(void) {
    uint64_t now = nv_now_ns();
    nv_fps_frames++;
    if (nv_fps_last_ns == 0) { nv_fps_last_ns = now; nv_fps_frames = 0; return; }
    uint64_t dt = now - nv_fps_last_ns;
    if (dt >= 500000000ull) {
        nv_fps_value   = (int)((nv_fps_frames * 1000000000ull + dt / 2) / dt);
        if (nv_fps_value > 999) nv_fps_value = 999;
        nv_fps_frames  = 0;
        nv_fps_last_ns = now;
    }
}

/* 1x, for notice text. */
static void nv_blit_rows1x(volatile uint16_t* dst, int gx, int gy,
                           const uint8_t* rows, uint16_t colour) {
    if (!rows) return;
    for (int ry = 0; ry < 7; ry++) {
        uint8_t bits = rows[ry];
        int py = gy + ry;
        if (py < 0 || py >= NV_FRAME_HEIGHT) continue;
        volatile uint16_t* row = dst + (size_t)py * NV_FRAME_WIDTH;
        for (int rx = 0; rx < 5; rx++) {
            if (!(bits & (0x10 >> rx))) continue;
            int px = gx + rx;
            if (px < 0 || px >= NV_FRAME_WIDTH) continue;
            row[px] = colour;
        }
    }
}

/* ==========================================================================
 * NOTICE OVERLAY
 *
 * Called from the recorder in openbor.c. Every message it emits went through
 * printf, which the engine #defines to writeToLogFile() -> OpenBorLog.txt. So
 * "wrong PAK", "not a valid recording", "recorded on an older build", "you took
 * over" and "playback finished" all presented identically to the player: the
 * PAK reset and then behaved oddly.
 *
 * Held for a few seconds, top-left, so it never collides with the bottom-right
 * fps read-out. Drawn here in WriteFrame, i.e. AFTER the downscale, exactly
 * like the fps overlay -- an engine-space notice would be squished with the
 * game image on a PAK that renders at 960x480.
 *
 * WALL-CLOCK deadline, deliberately, and NOT a frame count. The engine is
 * uncapped, so `seconds * 60` assumed 60 fps while a PAK ran ~100 and every
 * notice was ~40% shorter than asked -- which reads as flickery, not as brief.
 *
 * 🛑 The goals genuinely conflict here and this comment used to claim the other
 * one. A frame count is replay-deterministic but wrong; scaling it by measured
 * fps trades a wrong duration for a load-dependent one. Both would need a
 * DISPLAY-frame count at 59.92 Hz, and the ARM has no vsync. A notice is a
 * message to a human, so six seconds must be six seconds.
 *
 * The cost is that duration is not frame-identical across replays, so golden
 * captures must never sit inside a notice window -- Phase 0 already requires
 * stable scenes.
 * ========================================================================== */
static char     nv_notice_text[NV_COLS * NV_NOTICE_MAX + 1];

/* THE DEADLINE IS 32-BIT MILLISECONDS, AND THAT WIDTH IS THE POINT.
 *
 * It is written by the SWAP THREAD (the .s0/.s1/replay pollers all raise
 * notices) and read by the ENGINE THREAD every single frame, in
 * nv_notice_rows_now(), to decide the load-bearing row skip.
 *
 * As a uint64_t that was a TORN READ waiting to happen: ARM32 has no atomic
 * 64-bit plain load, so the reader could observe a new low word beside an old
 * high word. The dangerous direction lands the deadline far in the future --
 * nv_notice_rows_now() then never returns 0, every copy path permanently starts
 * below the band, and the top rows hold one frozen message for the REST OF THE
 * SESSION. A naturally-aligned 32-bit access IS single-copy atomic on ARM32, so
 * narrowing the value removes the tear rather than guarding it.
 *
 * 🛑 Do NOT "improve" this back to a 64-bit ns deadline with a seqlock: a
 * seqlock assumes ONE writer and there are two threads here, so it would need a
 * mutex in the frame path to be correct. Do NOT reach for __atomic_load_n on 8
 * bytes either -- on ARM32 that can emit a libatomic call this build does not
 * link. Millisecond resolution is far finer than a multi-second human-facing
 * message needs.
 *
 * Range is 49.7 days, measured from the same monotonic clock as everything
 * else, and the binary is respawned per content load. Wrap is handled anyway:
 * the reader compares with a SIGNED difference, which is correct across the
 * wrap for any interval under ~24 days. */
static volatile uint32_t nv_notice_until_ms = 0;   /* 0 == no notice live */

/* No notice runs longer than this. A value further out than this cannot have
 * come from a real call, so the reader treats it as expired instead of trusting
 * it -- belt-and-braces against any future path that publishes a bad deadline.
 * Raise it if a caller ever legitimately needs a longer notice. */
#define NV_NOTICE_MAX_MS  30000u

/* THE NOTICE IS STATIC: written into each buffer ONCE and then left alone.
 *
 * nv_notice_rows is the full-width band at the top of the frame that the notice
 * occupies. While a notice is live EVERY publisher starts its per-frame game
 * copy at that row instead of 0 (NativeVideoWriter_NoticeRows), so nothing
 * rewrites those pixels, and nv_notice_painted_gen records which buffers have
 * already received them.
 *
 * This is the letterbox rule applied to the notice, and it is the actual fix
 * for the flicker. The border on a sub-native PAK never flickers because
 * mister_present clears the buffers once and the per-frame copy never touches
 * those pixels again. The notice had the opposite property: the game copy
 * overwrote its region every frame and we repainted it afterwards, so any scan
 * landing between those two writes showed game pixels and no notice. THAT
 * repaint was the race -- it was never about how many buffers there are. A
 * third buffer was implemented and reverted the same day precisely because it
 * only narrows the window (safe below (N-1) x 59.9 fps; ATOV runs ~166).
 *
 * Painting once per buffer still has a two-frame exposure at the moment a
 * notice appears -- those two writes can be scanned like any other. That is
 * ~10-33 ms once, against a repaint on every frame for the notice's whole
 * lifetime, and it is the same exposure the game image already has.
 *
 * The band is full width, not just the text panel: the skipped rows keep
 * whatever was in them when the notice went up, so anything not painted would
 * be a frozen strip of stale game image beside the text. */
/* 🛑 volatile, and PAIRED with nv_notice_until_ms below.
 *
 * These two are written together by the swap thread and read together by
 * the engine thread every frame. Making only the deadline volatile left the
 * height as a plain load the compiler may cache (-flto) and the CPU may
 * reorder on weakly-ordered ARM32 -- so the reader could pair the NEW
 * deadline with the PREVIOUS message's height, skipping the wrong number of
 * rows: stale pixels below the band, or a black strip the paint never
 * covers. That is the artifact class the static-notice design exists to
 * eliminate.
 *
 * The writer's __sync_synchronize() is a release; without the acquire in
 * nv_notice_rows_now() it guarantees nothing to the reader. */
static volatile int nv_notice_rows = 0;

/* Which notice each buffer currently holds, as a generation number rather than
 * a "painted" flag.
 *
 * Notice() is called from the .s1 swap-detect thread (sdlport_patch.c) while
 * the engine thread is inside DrawOverlays, and a flag loses that race in the
 * worst possible direction: the engine reads the flags, Notice() clears them
 * for a NEW message, the engine then stores its stale read back -- and the new
 * notice is marked already-painted, so the OLD text sits on screen for the new
 * notice's full duration. Making the flag atomic does not help; the race is
 * logical, not a torn word.
 *
 * With a generation, a stale store records an OLD generation, which cannot
 * match the current one, so the next frame repaints. Every interleaving
 * self-heals within a frame, and the worst case is one frame of a band drawn
 * from a message that has just been replaced.
 *
 * Generation 0 means "holds no notice", so Notice() skips it on wrap. */
static volatile unsigned nv_notice_gen = 0;
static unsigned nv_notice_painted_gen[2] = { 0, 0 };

/* Advance *p past leading spaces and return how many characters fit on one
 * line, breaking at the last space rather than mid-word. 0 at end of text.
 *
 * ONE implementation, used by both the measure pass in Notice() and the draw
 * pass in nv_paint_notice_band(). They were separate copies of the same loop;
 * if they ever drifted the band would be sized for different text than it
 * holds, and the skipped rows are exactly the band height. */
static int nv_wrap_take(const char** p) {
    const char* q = *p;
    int take = 0, brk = 0;
    while (*q == ' ') q++;
    *p = q;
    if (!*q) return 0;
    while (q[take] && take < NV_COLS) {
        if (q[take] == ' ') brk = take;
        take++;
    }
    if (q[take] && brk > 0) take = brk;
    return take;
}

static uint32_t nv_now_ms(void) {
    return (uint32_t)(nv_now_ns() / 1000000ull);
}

/* 0 when no notice is live. Publishers start their destination row loop here.
 *
 * Read once into a local: this runs on the engine thread while the swap thread
 * can be writing, and re-reading would let the two comparisons below disagree. */
static int nv_notice_rows_now(void) {
    const uint32_t until = nv_notice_until_ms;
    int32_t rem;
    if (!until) return 0;
    /* SIGNED difference, not `now >= until`: this is the wrap-correct form (the
     * same idiom as the kernel's time_after) and stays right across the 49-day
     * rollover of the millisecond counter. */
    rem = (int32_t)(until - nv_now_ms());
    if (rem <= 0) return 0;
    if ((uint32_t)rem > NV_NOTICE_MAX_MS) return 0;   /* cannot be a real deadline */
    /* ACQUIRE side of the writer's release. Ordered AFTER the deadline read, so
     * a live deadline implies the height published with it. */
    __sync_synchronize();
    return nv_notice_rows;
}

int NativeVideoWriter_NoticeRows(void) { return nv_notice_rows_now(); }

/* Forget which buffers hold the band, so the live notice is painted into them
 * again. Call after ANYTHING wipes a frame buffer.
 *
 * The band is written once and then protected by the row skip, so a wipe that
 * does not tell us leaves the mask claiming a buffer still has text that has
 * just been zeroed -- and the skip then keeps the game from ever covering it.
 * The result is a black bar with nothing in it until the notice expires.
 *
 * Both wipe sites honour the invariant, by different means: mister_present's
 * one-shot letterbox clear calls this, while Init() cancels the notice outright
 * (a re-init is a fresh start, so there is no message worth preserving). */
void NativeVideoWriter_NoticeRepaint(void) {
    nv_notice_painted_gen[0] = 0;
    nv_notice_painted_gen[1] = 0;
}

/* 🛑 SERIALIZES WRITERS. The barriers further down order this publish against
 * a READER; they do nothing between two concurrent WRITERS, and there are two
 * threads here: the engine thread (pause menu, recorder, hot-swap) and the swap
 * thread (the .s1 pick and every refusal inside mrec_arm_slot_play, reached
 * from the OSD poll). Two overlapping calls interleave the byte-by-byte copy
 * into nv_notice_text and splice the two messages together.
 *
 * Worse than cosmetic: the line count measured from that text is the band
 * height every frame-copy path skips, so a spliced message can leave the
 * skipped band and the painted text disagreeing -- a stale strip, or a notice
 * clipped mid-word.
 *
 * WRITER-SIDE ONLY, deliberately. The per-frame draw is not locked: the
 * generation counter already makes a repaint follow a completed publish, and
 * putting a lock in the frame path would put contention on the hot loop to
 * close a one-frame, self-correcting window. No caller is a signal handler and
 * Notice() never re-enters itself, so a plain non-recursive mutex cannot
 * deadlock here. */
static pthread_mutex_t nv_notice_lock = PTHREAD_MUTEX_INITIALIZER;

void NativeVideoWriter_Notice(const char* msg, int seconds) {
    if (!msg) { nv_notice_until_ms = 0; return; }
    pthread_mutex_lock(&nv_notice_lock);
    size_t i = 0;
    while (msg[i] && i < sizeof(nv_notice_text) - 1) {
        char c = msg[i];
        nv_notice_text[i] = (c >= 'a' && c <= 'z') ? (char)(c - 32) : c;
        i++;
    }
    nv_notice_text[i] = 0;
    if (seconds <= 0) seconds = 4;
    /* Wall clock, not a frame count -- and this is a deliberate trade, not an
     * oversight. On an uncapped engine the two goals CONFLICT: `seconds * 60`
     * is deterministic but wrong (a 6 s notice lasted ~3.6 s at ~100 fps), and
     * `seconds * measured_fps` is right but load-dependent. Getting both would
     * need a DISPLAY-frame count at 59.92 Hz, and the ARM has no vsync signal --
     * MiSTer Main's vsync callback is process-internal.
     *
     * So keep the goal that matters: a notice is a message to a HUMAN, and six
     * seconds should be six seconds. This matches PICO-8, which has always done
     * it this way -- one mechanism on both cores.
     *
     * What is given up: notice duration is no longer identical frame-for-frame
     * between two replays of the same take. That is recoverable the right way --
     * golden captures are taken at STABLE scenes (Phase 0), which are not notice
     * moments. Do NOT capture a golden inside a notice window. */

    /* Measure the wrap ONCE, here, rather than on every frame in the draw: the
     * band height is what the publishers skip, so it has to be a stored fact
     * about this message, not something recomputed per frame per publisher.
     * lines*9 + 6 spans row 0 down to the bottom edge of the old text panel. */
    {
        const char* q = nv_notice_text;
        int lines = 0, take;
        while (lines < NV_NOTICE_MAX && (take = nv_wrap_take(&q)) > 0) {
            q += take;
            lines++;
        }
        if (lines == 0) { nv_notice_until_ms = 0;
                          pthread_mutex_unlock(&nv_notice_lock); return; }
        {   /* clamp in a LOCAL, publish once: nv_notice_rows is volatile,
             * so a two-step store makes the unclamped value observable. */
            int r = lines * 9 + 6;
            if (r > NV_FRAME_HEIGHT) r = NV_FRAME_HEIGHT;
            nv_notice_rows = r;
        }
    }

    /* 🛑 nv_notice_rows is written HERE, on whichever thread called Notice(),
     * and read by nv_notice_rows_now() on the ENGINE thread. Do not restate
     * the old claim that both run on the engine thread -- this function's
     * callers include the swap thread (the .s1 pick and every refusal inside
     * mrec_arm_slot_play, reached from the OSD poll). The barriers below are
     * load-bearing because of that, and the next reader must not delete them
     * on the authority of a comment saying the hazard does not exist.
     *
     * Order the height BEFORE the deadline, so a reader that sees a live
     * deadline necessarily sees the height that goes with it -- otherwise the
     * band could be skipped at the PREVIOUS message's height for a frame. */
    __sync_synchronize();
    {
        uint32_t until = nv_now_ms() + (uint32_t)seconds * 1000u;
        if (!until) until = 1u;   /* 0 is the "no notice" sentinel; never publish it */
        nv_notice_until_ms = until;
    }

    /* Bump the generation LAST, behind a barrier: it is what triggers the
     * repaint, so it must change only once the text and height it refers to are
     * in place, or a repaint racing it would draw the PREVIOUS message at the
     * new height. Skip 0 on wrap -- 0 means "this buffer holds no notice". */
    __sync_synchronize();
    if (++nv_notice_gen == 0) nv_notice_gen = 1;
    pthread_mutex_unlock(&nv_notice_lock);
}

/* Solid backing panel for the notice text.
 *
 * The text used to be drawn straight onto the game with only a 1 px drop
 * shadow. Over busy art that is hard to read at 1x, and because the game keeps
 * ANIMATING underneath and between the glyphs -- on ATOV's attract screen the
 * engine's own "PRESS START"/"CREDIT 19" blinks in the very same band -- the
 * whole region shimmers, which is what reads as the notice flickering. A solid
 * panel removes the moving background entirely, so the text is stationary
 * against a constant colour. Verified against a real capture 2026-08-04: the
 * notice itself was stable frame to frame; it was the content around and behind
 * it that was moving. */
static void nv_fill_rect(volatile uint16_t* dst, int x0, int y0, int w, int h,
                         uint16_t colour) {
    for (int y = y0; y < y0 + h; y++) {
        if (y < 0 || y >= NV_FRAME_HEIGHT) continue;
        volatile uint16_t* row = dst + (size_t)y * NV_FRAME_WIDTH;
        for (int x = x0; x < x0 + w; x++) {
            if (x < 0 || x >= NV_FRAME_WIDTH) continue;
            row[x] = colour;
        }
    }
}

/* Paint the band into ONE buffer. Word-wrapped, not cropped: a word longer than
 * a line is hard-broken rather than dropped, so a long PAK name still shows
 * something useful.
 *
 * Called once per buffer per notice, never per frame -- see nv_notice_rows. */
static void nv_paint_notice_band(volatile uint16_t* dst, int rows) {
    const char* p = nv_notice_text;
    int line = 0, take;

    nv_fill_rect(dst, 0, 0, NV_FRAME_WIDTH, rows, 0x0000);

    while (line < NV_NOTICE_MAX && (take = nv_wrap_take(&p)) > 0) {
        int y = NV_FPS_MARGIN + line * 9;
        /* Shadow pass kept: the band is black, so the white glyphs already have
         * contrast, but the offset copy keeps them readable if a future caller
         * ever draws a notice without the band behind it. */
        for (int pass = 0; pass < 2; pass++) {
            uint16_t c   = (pass == 0) ? 0x0000 : 0xFFFF;
            int      off = (pass == 0) ? 1 : 0;
            for (int i = 0; i < take; i++)
                nv_blit_rows1x(dst, NV_FPS_MARGIN + i * 6 + off, y + off,
                               nv_glyph_rows(p[i]), c);
        }
        p += take;
        line++;
    }
}

/* ---------------------------------------------------------------- loading %
 *
 * A MiSTer-side loading read-out, driven by the engine's real
 * update_loading(pos, max) progress and painted here in DISPLAY space.
 *
 * WHY NOT USE THE CART'S BAR: measured across the 450-PAK library, a cart's
 * `loadingbg` cannot tell us what we need. Ultimate Double Dragon, Avengers and
 * PDC2 all declare the SAME thing (set=1, background only, bar coords
 * off-screen) yet one shows nothing while the other two draw their own bar from
 * script -- which the engine cannot see. An earlier attempt to rescue the bar by
 * clamping its coordinates therefore fixed UDD and gave Avengers/PDC2 a SECOND
 * bar. Reading cart config is a dead end; this ignores it entirely and is
 * identical on every PAK.
 *
 * Deliberately NOT bar-shaped: a bar reads as a duplicate of the game's own on
 * the PAKs that have one. A percentage reads as a system indicator, like the fps
 * digits, and tells you how far in you are -- which is what matters on a 69 s
 * Justice League load.
 *
 * Expiry is a WALL-CLOCK deadline refreshed on every progress call, so the
 * read-out disappears on its own shortly after loading stops. That avoids having
 * to find and hook every exit from every loading path -- a missed one would
 * leave the text burned on screen for the rest of the session. */
#define NV_LOAD_HOLD_MS 700u

static volatile int      nv_load_pct      = 0;
static volatile uint32_t nv_load_until_ms = 0;

void NativeVideoWriter_SetLoadingProgress(int pos, int max) {
    int pct;
    if (max <= 0 || pos < 0) {          /* update_loading(-1, 1) = init */
        pct = 0;
    } else {
        pct = (int)(((long)pos * 100L) / (long)max);
        if (pct < 0)   pct = 0;
        if (pct > 100) pct = 100;
    }
    nv_load_pct      = pct;
    nv_load_until_ms = nv_now_ms() + NV_LOAD_HOLD_MS;
}

/* Bottom-LEFT: clear of the notice band (top, full width) and of the fps digits
 * (bottom-right), so all three can be live without overlapping. */
static void nv_draw_loading(volatile uint16_t* dst) {
    char buf[16];
    int  n = 0, v = nv_load_pct, i;
    const char* prefix = "LOADING ";
    int y = NV_FRAME_HEIGHT - NV_FPS_MARGIN - 7;   /* 1x glyphs are 7 rows */

    for (i = 0; prefix[i] && n < (int)sizeof(buf) - 5; i++) buf[n++] = prefix[i];
    if (v >= 100) { buf[n++] = '1'; buf[n++] = '0'; buf[n++] = '0'; }
    else if (v >= 10) { buf[n++] = (char)('0' + v / 10); buf[n++] = (char)('0' + v % 10); }
    else { buf[n++] = (char)('0' + v); }
    buf[n++] = '%';
    buf[n]   = '\0';

    /* Shadow then text, exactly as the notice band and fps do -- the loading
     * background is cart art and can be any colour, so the offset black copy is
     * what keeps this legible rather than a backing panel. */
    for (int pass = 0; pass < 2; pass++) {
        uint16_t c   = (pass == 0) ? 0x0000 : 0xFFFF;
        int      off = (pass == 0) ? 1 : 0;
        for (i = 0; i < n; i++)
            nv_blit_rows1x(dst, NV_FPS_MARGIN + i * 6 + off, y + off,
                           nv_glyph_rows(buf[i]), c);
    }
}

/* Which of the two frame buffers `dst` is, or -1 if it is neither.
 *
 * Derived from the POINTER rather than taken from the caller on purpose. The
 * two publishers keep separate active_buf state, and every bug this file has
 * had around buffer identity came from those two views disagreeing. The pointer
 * is the buffer. */
static int nv_buf_index(volatile uint16_t* dst) {
    const volatile uint8_t* p = (const volatile uint8_t*)dst;
    if (!ddr_base) return -1;
    if (p == ddr_base + NV_BUF0_OFFSET) return 0;
    if (p == ddr_base + NV_BUF1_OFFSET) return 1;
    return -1;
}

static void nv_blit_glyph(volatile uint16_t* dst, int gx, int gy,
                          int digit, uint16_t colour) {
    const uint8_t* rows = nv_font5x7[digit];
    for (int ry = 0; ry < 7; ry++) {
        uint8_t bits = rows[ry];
        for (int rx = 0; rx < 5; rx++) {
            if (!(bits & (0x10 >> rx))) continue;
            /* 2x scale */
            for (int sy = 0; sy < 2; sy++) {
                int py = gy + ry * 2 + sy;
                if (py < 0 || py >= NV_FRAME_HEIGHT) continue;
                volatile uint16_t* row = dst + (size_t)py * NV_FRAME_WIDTH;
                for (int sx = 0; sx < 2; sx++) {
                    int px = gx + rx * 2 + sx;
                    if (px < 0 || px >= NV_FRAME_WIDTH) continue;
                    row[px] = colour;
                }
            }
        }
    }
}

/* The pixels the fps digits are about to cover, kept per buffer so
 * CaptureDisplay can hand back a frame WITHOUT them.
 *
 * CaptureDisplay copies the last published buffer, and the overlays have
 * already been drawn into it -- so the A9 pause menu, which captures that
 * frame ONCE as its backdrop, was baking the fps number into the background.
 * Both numbers sit in the same bottom-right rect, so with the overlay on the
 * live value painted over the stale one and hid it; switching the overlay off
 * revealed a frozen number that stayed for the life of the menu (reported on
 * ATOV, 2026-08-07: "a 60 showed underneath and remained frozen until I
 * unpaused").
 *
 * Saving the rect costs a few hundred bytes and keeps the LIVE fps working
 * while paused, which is wanted -- the pause loop is uncapped, and its rate is
 * exactly the thing the overlay is there to show. Blanking the rect instead
 * would leave a black box in the backdrop; suppressing the overlay during
 * pause would throw away the measurement. */
#define NV_FPS_BAK_W (3 * NV_GLYPH_W + 2 * NV_GLYPH_GAP + 1)  /* 3 digits + shadow */
#define NV_FPS_BAK_H (NV_GLYPH_H + 1)                         /* + shadow          */
static uint16_t nv_fps_bak[2][NV_FPS_BAK_H][NV_FPS_BAK_W];
static int      nv_fps_bak_valid[2] = { 0, 0 };

/* Widest possible origin, so the saved rect covers any digit count. */
static int nv_fps_bak_x0(void) {
    int x = NV_FRAME_WIDTH - NV_FPS_MARGIN - (3 * NV_GLYPH_W + 2 * NV_GLYPH_GAP);
    return x < 0 ? 0 : x;
}
static int nv_fps_bak_y0(void) {
    int y = NV_FRAME_HEIGHT - NV_FPS_MARGIN - NV_GLYPH_H;
    return y < 0 ? 0 : y;
}

static void nv_fps_save(volatile uint16_t* dst, int buf) {
    int x0 = nv_fps_bak_x0(), y0 = nv_fps_bak_y0();
    for (int y = 0; y < NV_FPS_BAK_H; y++) {
        int sy = y0 + y;
        if (sy >= NV_FRAME_HEIGHT) break;
        for (int x = 0; x < NV_FPS_BAK_W; x++) {
            int sx = x0 + x;
            if (sx >= NV_FRAME_WIDTH) break;
            nv_fps_bak[buf][y][x] = dst[(size_t)sy * NV_FRAME_WIDTH + sx];
        }
    }
    nv_fps_bak_valid[buf] = 1;
}

/* Into a PLAIN copy (the caller's capture), not DDR3. */
static void nv_fps_restore(uint16_t* copy, int buf) {
    int x0 = nv_fps_bak_x0(), y0 = nv_fps_bak_y0();
    for (int y = 0; y < NV_FPS_BAK_H; y++) {
        int sy = y0 + y;
        if (sy >= NV_FRAME_HEIGHT) break;
        for (int x = 0; x < NV_FPS_BAK_W; x++) {
            int sx = x0 + x;
            if (sx >= NV_FRAME_WIDTH) break;
            copy[(size_t)sy * NV_FRAME_WIDTH + sx] = nv_fps_bak[buf][y][x];
        }
    }
}

/* Draw the current fps, bottom-right, colour-coded:
 *   red    0-29    yellow 30-59    green 60+
 * A black copy is laid down one pixel down-right first so the number stays
 * legible over bright or busy backgrounds. */
static void nv_draw_fps(volatile uint16_t* dst) {
    int v = nv_fps_value;
    int digits[3], nd = 0;
    if (v <= 0) { digits[nd++] = 0; }
    else { while (v > 0 && nd < 3) { digits[nd++] = v % 10; v /= 10; } }

    uint16_t colour = (nv_fps_value >= 60) ? 0x07E0        /* green  */
                    : (nv_fps_value >= 30) ? 0xFFE0        /* yellow */
                                           : 0xF800;       /* red    */

    int total_w = nd * NV_GLYPH_W + (nd - 1) * NV_GLYPH_GAP;
    int x0 = NV_FRAME_WIDTH  - NV_FPS_MARGIN - total_w;
    int y0 = NV_FRAME_HEIGHT - NV_FPS_MARGIN - NV_GLYPH_H;

    for (int pass = 0; pass < 2; pass++) {
        uint16_t c  = (pass == 0) ? 0x0000 : colour;
        int      off = (pass == 0) ? 1 : 0;
        for (int i = 0; i < nd; i++) {
            /* digits[] is least-significant first */
            int gx = x0 + (nd - 1 - i) * (NV_GLYPH_W + NV_GLYPH_GAP);
            nv_blit_glyph(dst, gx + off, y0 + off, digits[i], c);
        }
    }
}

/* THE single overlay entry point, for EVERY publisher of a DDR3 frame.
 *
 * There are two: this file's WriteFrame (the direct-write path) and
 * mister_present in the SDL dummy driver (the fallback used for boot/menu/wait
 * surfaces). Both write the SAME two buffers at 0x3A000000, each with its own
 * active_buf. Only WriteFrame ever drew the overlays, so whenever both were
 * live the published frames alternated between "has the notice" and "does not"
 * -- one of the several things reported as the notice flicker on 2026-08-04,
 * and why adding a solid backing panel did not help: the panel was on the
 * frames that had the notice, and absent from the ones that did not.
 *
 * Anything that publishes a frame MUST call this, or it will punch a hole in
 * every overlay -- and it must ALSO honour NativeVideoWriter_NoticeRows(), or
 * it copies game pixels over a notice this function will then not repaint.
 *
 * Call exactly once per published frame. */
/* The publisher tells us WHICH buffer it is, because we cannot work it out.
 *
 * nv_buf_index() identifies a buffer by comparing the pointer to
 * ddr_base + BUF0/BUF1 -- but the SDL-dummy publisher (patch_sdl_dummy.py)
 * performs its OWN mmap of the same physical region, and two mmap(NULL, ...)
 * calls return DIFFERENT virtual addresses. So every frame IT published came
 * back as "not one of ours", which meant:
 *
 *   - the fps rect was neither saved nor invalidated, while the digits were
 *     still drawn -- so a pause capture landing there kept them burned in AND
 *     stamped a stale rect from an older frame over them; and
 *   - the notice fell to the repaint-every-frame path, which is precisely the
 *     per-frame repaint the STATIC notice design exists to eliminate. The
 *     flicker was still live on that publisher, and it is the one that
 *     publishes boot/menu/wait surfaces -- exactly when the handler's
 *     save-isolation notices appear.
 *
 * A comment here used to assert "both publishers pass ddr_base + BUF0/BUF1".
 * That was false, and it is why this read as safe.
 *
 * The publisher already knows its index (it passes the same value to
 * NotePublished), so it hands it over rather than us guessing from a pointer
 * that means different things in different mappings. */
void NativeVideoWriter_DrawOverlaysAt(volatile uint16_t* dst, int buf_index) {
    if (!dst) return;
    nv_fps_tick();

    /* The fps read-out is redrawn every frame and therefore STILL drops on the
     * odd frame at high render rates, exactly as the notice used to. Accepted,
     * and a COSTED CHOICE rather than an impossibility.
     *
     * 🛑 It is NOT exempt because "it changes every frame". nv_fps_value is
     * recomputed only twice a second, so the same digits are drawn for ~83
     * consecutive frames at 166 fps -- it could be made static like the notice.
     * It is not, because it is a bottom-RIGHT rectangle rather than a
     * full-width band: skipping a rectangle needs per-row X guards in every
     * copy path (the partial-coverage trap the band design avoids), and the
     * cheap band form would put a permanent black bar over rows 206-223, 8% of
     * the frame, the whole time the overlay is on. A dropped frame on a number
     * identical for ~83 frames is a thinning, not lost information. */
    /* Save what the digits are about to cover, so CaptureDisplay can undo them
     * (see nv_fps_bak). When the overlay is off nothing is burned in, so the
     * backup is invalidated rather than left stale -- a stale one would restore
     * an OLD rect over a capture that never needed correcting. */
    {
        /* Read the flag ONCE. It was read here and again below; single-
         * threaded today so the two always agreed, but a mismatch would
         * either restore a rect nothing drew or leave one burned in. */
        const int fps_on = mister_fps_overlay;
        const int fb     = buf_index;
        if (fb >= 0 && fb < 2) {
            if (fps_on) nv_fps_save(dst, fb);
            else        nv_fps_bak_valid[fb] = 0;
        }
        if (fps_on) nv_draw_fps(dst);
    }

    /* The loading read-out, while its deadline is live. No save/restore rect
     * like the fps digits need: those exist so the pause menu's CaptureDisplay
     * does not freeze the fps number into its backdrop, and the pause menu
     * cannot be open during a load. Drawn every frame rather than once per
     * buffer because the number changes -- same as the fps digits, and unlike
     * the notice band, which is static precisely because its text does not. */
    if ((int32_t)(nv_load_until_ms - nv_now_ms()) > 0) {
        nv_draw_loading(dst);
    }

    /* The notice, once per buffer. Bottom-right fps first so a notice is never
     * painted over by it; they cannot overlap at the current sizes (the band is
     * at most 33 rows, the fps digits start at row 206) but the order costs
     * nothing and survives either one growing. */
    {
        int rows = nv_notice_rows_now();
        if (rows > 0) {
            unsigned gen = nv_notice_gen;
            int      idx = (buf_index >= 0 && buf_index < 2)
                         ? buf_index : -1;
            if (idx < 0) {
                /* A caller that does not know its buffer. Repaint every frame
                 * rather than skip -- correct pixels, just not static. No
                 * shipped caller takes this path any more. */
                nv_paint_notice_band(dst, rows);
            } else if (nv_notice_painted_gen[idx] != gen) {
                nv_paint_notice_band(dst, rows);
                /* Record the generation READ ABOVE, not the current one. If
                 * Notice() bumped it while we were painting, this stores a
                 * stale value, which cannot match next frame -- so the buffer
                 * is repainted with the new message instead of being left
                 * holding the old one. */
                nv_notice_painted_gen[idx] = gen;
            }
        }
    }

    /* Expiry needs nothing here. The band pixels simply stop being protected --
     * NoticeRows() returns 0, the copy covers the whole frame again and the game
     * paints over them. Whichever way the copy and this function straddle the
     * expiry instant is benign: one frame keeps the band a moment longer, or
     * loses it a moment early. Neither shows stale or half-written pixels. */
}

/* Pointer-resolving form, kept for callers that share this file's mapping. */
void NativeVideoWriter_DrawOverlays(volatile uint16_t* dst) {
    NativeVideoWriter_DrawOverlaysAt(dst, nv_buf_index(dst));
}

/* ==========================================================================
 * DISPLAY-SPACE OVERLAY  (the pause menu)
 *
 * The pause menu used to be drawn by the engine at the PAK's NATIVE resolution
 * and then squished with the game image -- on He-Man (960x480 -> 320x224) it
 * came out about a third of its intended size. Same class of mistake as drawing
 * the fps read-out on the engine's vscreen: an overlay belongs in DISPLAY space,
 * after every transform, not in the space the content happens to render at.
 *
 * So the menu now renders into its own 320x224 surface and hands it here. While
 * one is set, WriteFrame publishes IT instead of downscaling the engine frame.
 *
 * It is a FULL frame, not a keyed overlay, deliberately: the alternative needs a
 * transparent colour, and any colour we reserve is one the engine's palette can
 * also produce -- a guess that would show up as holes punched in the menu text
 * on whichever PAK happened to hit it. The caller seeds the overlay with
 * NativeVideoWriter_CaptureDisplay() (the last published frame, already
 * correctly downscaled) and draws on top, so the background stays pixel-
 * identical to the live image with no second scaler in the codebase.
 * ========================================================================== */
static const void* volatile nv_overlay = NULL;

void NativeVideoWriter_SetOverlay(const void* pixels) { nv_overlay = pixels; }

/* The display size an overlay must be. A getter rather than two more #defines
 * on the engine side: the frame dimensions already exist here, in the header
 * and in the RTL, and this project has been bitten more than once by a fourth
 * hand-copied constant drifting out of step with the other three. */
void NativeVideoWriter_GetDisplaySize(int* w, int* h) {
    if (w) *w = NV_FRAME_WIDTH;
    if (h) *h = NV_FRAME_HEIGHT;
}

void NativeVideoWriter_CaptureDisplay(void* dst) {
    if (!ddr_base || !dst) return;
    /* The LAST PUBLISHED buffer -- the frame currently on screen. Not
     * active_buf, which is the one WriteFrame fills next and may be half
     * written or two frames stale. */
    const volatile uint8_t* src = ddr_base
        + ((nv_last_published & 1) ? NV_BUF1_OFFSET : NV_BUF0_OFFSET);
    memcpy(dst, (const void*)src, NV_FRAME_BYTES);

    /* Undo the fps digits. The caller freezes this frame as the pause-menu
     * backdrop, so anything left here is burned in for the life of the menu --
     * and a number frozen at the moment of pause is worse than no number,
     * because it looks live until you notice it never changes. */
    {
        int b = nv_last_published & 1;
        if (nv_fps_bak_valid[b]) nv_fps_restore((uint16_t*)dst, b);
    }
}

void NativeVideoWriter_WriteFrame(const void* pixels, int width, int height,
                                  int pitch, int bpp, const void* palette) {
    if (!ddr_base || !pixels) return;
    if (width <= 0 || height <= 0) return;

    /* Anisotropic nearest-neighbor squish: source W×H → NV_FRAME_WIDTH×HEIGHT.
     * Sega CD V28 NTSC active area = 320×224. 320×240 PAKs (ATOV, etc.)
     * get ~7% Y compress; sub-native PAKs (480×272, 960×480) get larger
     * downscale. Aspect distortion intentional — matches Sega CD displayed
     * area edge-to-edge per NTSC region match rule. NN avoids the per-pixel
     * cost of bilinear (~4× faster on Cortex-A9 — 2026-05-22 measurement). */
    int sx256 = (width * 256) / NV_FRAME_WIDTH;
    int sy256 = (height * 256) / NV_FRAME_HEIGHT;
    if (sx256 == 0) sx256 = 1;
    if (sy256 == 0) sy256 = 1;

    /* MiSTer 2026-05-27 Step 18: precompute src_x lookup table once per
     * frame. Hoists (x * sx256) / 256 + clamp out of the inner pixel loop.
     * Saves 1 mul + 1 div + 1 compare per dest pixel (71680 px/frame on
     * 320x224). Same arithmetic, byte-identical output, ~20-30% lift on
     * the WriteFrame inner loop. Identifies vcopy as JL Legacy's dominant
     * cost (53% of per-frame budget; SUB-PROFILE v9 measurement 2026-05-27). */
    uint16_t src_x_table[NV_FRAME_WIDTH];
    {
        int wm1 = width - 1;
        for (int x = 0; x < NV_FRAME_WIDTH; x++) {
            int sx = (x * sx256) / 256;
            src_x_table[x] = (uint16_t)((sx >= width) ? wm1 : sx);
        }
    }

    uint32_t buf_offset = (active_buf == 0) ? NV_BUF0_OFFSET : NV_BUF1_OFFSET;
    volatile uint16_t* dst = (volatile uint16_t*)(ddr_base + buf_offset);

    /* First destination row this frame. Non-zero while a notice is up: those
     * rows already hold the notice, drawn once, and copying game pixels over
     * them is precisely what made the notice flicker. See nv_notice_rows.
     *
     * Read ONCE per frame, not per path. The 32bpp case writes an intermediate
     * in one loop and reads it back in another, so two paths evaluating the
     * expiry independently could disagree inside a single frame and leave a band
     * of last frame's averaged pixels on screen. */
    const int nv_top = nv_notice_rows_now();

    /* A display-space overlay replaces the whole frame: it is already at
     * 320x224, so every scale path below is skipped.
     *
     * It still starts at nv_top. The notice band is written once and then
     * protected by that skip, and an overlay copying over rows 0..nv_top-1
     * every frame would put the notice straight back to being repainted --
     * which is the flicker. Missing this is exactly the partial-coverage trap:
     * it would look fine until a notice happened to fire during a pause. */
    {
        const void* ov = nv_overlay;
        if (ov) {
            const uint16_t* orow = (const uint16_t*)ov + (size_t)nv_top * NV_FRAME_WIDTH;
            volatile uint16_t* drow = dst + (size_t)nv_top * NV_FRAME_WIDTH;
            size_t n = (size_t)(NV_FRAME_HEIGHT - nv_top) * NV_FRAME_WIDTH;
            memcpy((void*)drow, orow, n * 2);

            NativeVideoWriter_DrawOverlays(dst);
            __sync_synchronize();
            nv_last_published = active_buf & 1;
            __sync_synchronize();
            uint32_t ofc = __sync_add_and_fetch(&frame_counter, 1);
            volatile uint32_t* octrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
            *octrl = (ofc << 2) | (active_buf & 1);
            active_buf ^= 1;
            return;
        }
    }

    if (bpp == 16) {
        /* OpenBOR's 16bpp surfaces are BGR565 (B in high bits). The FPGA
         * decoder expects RGB565. Swap R and B 5-bit fields per pixel. */
        const uint8_t* src = (const uint8_t*)pixels;
        /* nv_top: skip the notice band. This ONE loop covers every 16bpp scale
         * path -- the NEON 1:1, the 3x, 2x and 3:2 boxes and the scalar box are
         * all branches INSIDE it, each deriving its row pointer from this y. */
        for (int y = nv_top; y < NV_FRAME_HEIGHT; y++) {
            int src_y = (y * sy256) / 256;
            if (src_y >= height) src_y = height - 1;
            const uint16_t* src_row = (const uint16_t*)(src + src_y * pitch);
            volatile uint16_t* dst_row = dst + y * NV_FRAME_WIDTH;

            /* Step 20 (2026-05-27): wider DDR3 stores. JL Legacy vcopy
             * measured 53% of per-frame budget on heavy combat scenes
             * (SUB-PROFILE v9, 2026-05-27). Per-pixel scalar 16-bit stores
             * to DDR3 are bus-inefficient; widening to uint64_t (4 px) and
             * NEON 128-bit (8 px) lets the write-combine buffer issue
             * fuller DDR3 bursts. NV_FRAME_WIDTH=320 is divisible by 8 so
             * neither path needs a scalar tail. */
            if (width == NV_FRAME_WIDTH && ((uintptr_t)src_row & 15) == 0) {
                /* NEON fast path — no squish + 16-byte-aligned source.
                 * Process 8 pixels per iteration. BGR565 -> RGB565: swap
                 * the low-5 (R) and high-5 (B) fields per pixel. Green (mid
                 * 6 bits) stays in place. */
                const uint16x8_t mask_r = vdupq_n_u16(0x001F);
                const uint16x8_t mask_g = vdupq_n_u16(0x07E0);
                const uint16x8_t mask_b = vdupq_n_u16(0xF800);
                for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                    uint16x8_t px = vld1q_u16(src_row + x);
                    uint16x8_t r = vandq_u16(px, mask_r);
                    uint16x8_t g = vandq_u16(px, mask_g);
                    uint16x8_t b = vandq_u16(px, mask_b);
                    uint16x8_t r_shifted = vshlq_n_u16(r, 11);
                    uint16x8_t b_shifted = vshrq_n_u16(b, 11);
                    uint16x8_t out = vorrq_u16(vorrq_u16(r_shifted, g), b_shifted);
                    vst1q_u16((uint16_t*)(dst_row + x), out);
                }
            } else if (width == NV_FRAME_WIDTH * 3) {
                /* MiSTer Tier-0 (2026-06-15): NEON 3x area-average box for the
                 * 16bpp BGR565 squish (He-Man 960x480 -> 320x224, 3x X). hcnt==3
                 * for every output column; vcnt varies per row. Byte-identical to
                 * the scalar box below: expand each 5/6-bit tap to 8-bit
                 * ((v<<3)|(v>>2) / (v<<2)|(v>>4)), sum, divide by 3*vcnt via the
                 * same recip, pack RGB565. vld3q_u16 deinterleaves the 3 taps. */
                int yy0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
                int yy1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
                if (yy1 <= yy0) yy1 = yy0 + 1;
                if (yy1 > height) yy1 = height;
                if (yy0 >= height) yy0 = height - 1;
                int vcnt = yy1 - yy0;
                const uint8_t* sbase = src + (size_t)yy0 * pitch;
                uint32_t rc = (uint32_t)((1u << 20) / ((uint32_t)3 * (uint32_t)vcnt));

                uint16_t acc_r[NV_FRAME_WIDTH], acc_g[NV_FRAME_WIDTH], acc_b[NV_FRAME_WIDTH];
                memset(acc_r, 0, sizeof(acc_r));
                memset(acc_g, 0, sizeof(acc_g));
                memset(acc_b, 0, sizeof(acc_b));

                const uint16x8_t m5 = vdupq_n_u16(0x001F);
                const uint16x8_t m6 = vdupq_n_u16(0x003F);
                const uint8_t* rowp = sbase;
                for (int syy = 0; syy < vcnt; syy++) {
                    const uint16_t* sp = (const uint16_t*)rowp;
                    for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                        uint16x8x3_t g3 = vld3q_u16(sp + (size_t)x * 3);
                        uint16x8_t hr = vdupq_n_u16(0), hg = vdupq_n_u16(0), hb = vdupq_n_u16(0);
                        for (int t = 0; t < 3; t++) {
                            uint16x8_t p  = g3.val[t];
                            uint16x8_t r5 = vandq_u16(p, m5);
                            uint16x8_t g6 = vandq_u16(vshrq_n_u16(p, 5), m6);
                            uint16x8_t b5 = vandq_u16(vshrq_n_u16(p, 11), m5);
                            hr = vaddq_u16(hr, vorrq_u16(vshlq_n_u16(r5, 3), vshrq_n_u16(r5, 2)));
                            hg = vaddq_u16(hg, vorrq_u16(vshlq_n_u16(g6, 2), vshrq_n_u16(g6, 4)));
                            hb = vaddq_u16(hb, vorrq_u16(vshlq_n_u16(b5, 3), vshrq_n_u16(b5, 2)));
                        }
                        vst1q_u16(&acc_r[x], vaddq_u16(vld1q_u16(&acc_r[x]), hr));
                        vst1q_u16(&acc_g[x], vaddq_u16(vld1q_u16(&acc_g[x]), hg));
                        vst1q_u16(&acc_b[x], vaddq_u16(vld1q_u16(&acc_b[x]), hb));
                    }
                    rowp += pitch;
                }

                const uint32x4_t rc_v  = vdupq_n_u32(rc);
                const uint32x4_t halfv = vdupq_n_u32(1u << 19);
                volatile uint16_t* drow = dst + (size_t)y * NV_FRAME_WIDTH;
                for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                    uint16x8_t ar = vld1q_u16(&acc_r[x]);
                    uint16x8_t ag = vld1q_u16(&acc_g[x]);
                    uint16x8_t ab = vld1q_u16(&acc_b[x]);
                    uint32x4_t rl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ar)),  rc_v), halfv), 20);
                    uint32x4_t rh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ar)), rc_v), halfv), 20);
                    uint32x4_t gl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ag)),  rc_v), halfv), 20);
                    uint32x4_t gh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ag)), rc_v), halfv), 20);
                    uint32x4_t bl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ab)),  rc_v), halfv), 20);
                    uint32x4_t bh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ab)), rc_v), halfv), 20);
                    uint16x8_t r8 = vcombine_u16(vmovn_u32(rl), vmovn_u32(rh));
                    uint16x8_t g8 = vcombine_u16(vmovn_u32(gl), vmovn_u32(gh));
                    uint16x8_t b8 = vcombine_u16(vmovn_u32(bl), vmovn_u32(bh));
                    uint16x8_t out = vorrq_u16(vorrq_u16(
                                        vshlq_n_u16(vshrq_n_u16(r8, 3), 11),
                                        vshlq_n_u16(vshrq_n_u16(g8, 2), 5)),
                                        vshrq_n_u16(b8, 3));
                    vst1q_u16((uint16_t*)(drow + x), out);
                }
            } else if (width == NV_FRAME_WIDTH * 2) {
                /* MiSTer #9 (2026-06-16): NEON 2x area-average box (16bpp BGR565,
                 * 640x.. -> 320x224, 2x X). hcnt==2 for every output column;
                 * byte-identical to the scalar box (same expand / recip / round).
                 * vld2q_u16 deinterleaves the 2 taps per dest pixel. */
                int yy0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
                int yy1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
                if (yy1 <= yy0) yy1 = yy0 + 1;
                if (yy1 > height) yy1 = height;
                if (yy0 >= height) yy0 = height - 1;
                int vcnt = yy1 - yy0;
                const uint8_t* sbase = src + (size_t)yy0 * pitch;
                uint32_t rc = (uint32_t)((1u << 20) / ((uint32_t)2 * (uint32_t)vcnt));

                uint16_t acc_r[NV_FRAME_WIDTH], acc_g[NV_FRAME_WIDTH], acc_b[NV_FRAME_WIDTH];
                memset(acc_r, 0, sizeof(acc_r));
                memset(acc_g, 0, sizeof(acc_g));
                memset(acc_b, 0, sizeof(acc_b));
                const uint16x8_t m5 = vdupq_n_u16(0x001F);
                const uint16x8_t m6 = vdupq_n_u16(0x003F);
                const uint8_t* rowp = sbase;
                for (int syy = 0; syy < vcnt; syy++) {
                    const uint16_t* sp = (const uint16_t*)rowp;
                    for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                        uint16x8x2_t g2 = vld2q_u16(sp + (size_t)x * 2);
                        uint16x8_t hr = vdupq_n_u16(0), hg = vdupq_n_u16(0), hb = vdupq_n_u16(0);
                        for (int t = 0; t < 2; t++) {
                            uint16x8_t p  = g2.val[t];
                            uint16x8_t r5 = vandq_u16(p, m5);
                            uint16x8_t g6 = vandq_u16(vshrq_n_u16(p, 5), m6);
                            uint16x8_t b5 = vandq_u16(vshrq_n_u16(p, 11), m5);
                            hr = vaddq_u16(hr, vorrq_u16(vshlq_n_u16(r5, 3), vshrq_n_u16(r5, 2)));
                            hg = vaddq_u16(hg, vorrq_u16(vshlq_n_u16(g6, 2), vshrq_n_u16(g6, 4)));
                            hb = vaddq_u16(hb, vorrq_u16(vshlq_n_u16(b5, 3), vshrq_n_u16(b5, 2)));
                        }
                        vst1q_u16(&acc_r[x], vaddq_u16(vld1q_u16(&acc_r[x]), hr));
                        vst1q_u16(&acc_g[x], vaddq_u16(vld1q_u16(&acc_g[x]), hg));
                        vst1q_u16(&acc_b[x], vaddq_u16(vld1q_u16(&acc_b[x]), hb));
                    }
                    rowp += pitch;
                }
                const uint32x4_t rc_v  = vdupq_n_u32(rc);
                const uint32x4_t halfv = vdupq_n_u32(1u << 19);
                volatile uint16_t* drow = dst + (size_t)y * NV_FRAME_WIDTH;
                for (int x = 0; x < NV_FRAME_WIDTH; x += 8) {
                    uint16x8_t ar = vld1q_u16(&acc_r[x]);
                    uint16x8_t ag = vld1q_u16(&acc_g[x]);
                    uint16x8_t ab = vld1q_u16(&acc_b[x]);
                    uint32x4_t rl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ar)),  rc_v), halfv), 20);
                    uint32x4_t rh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ar)), rc_v), halfv), 20);
                    uint32x4_t gl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ag)),  rc_v), halfv), 20);
                    uint32x4_t gh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ag)), rc_v), halfv), 20);
                    uint32x4_t bl = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(ab)),  rc_v), halfv), 20);
                    uint32x4_t bh = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(ab)), rc_v), halfv), 20);
                    uint16x8_t r8 = vcombine_u16(vmovn_u32(rl), vmovn_u32(rh));
                    uint16x8_t g8 = vcombine_u16(vmovn_u32(gl), vmovn_u32(gh));
                    uint16x8_t b8 = vcombine_u16(vmovn_u32(bl), vmovn_u32(bh));
                    uint16x8_t out = vorrq_u16(vorrq_u16(
                                        vshlq_n_u16(vshrq_n_u16(r8, 3), 11),
                                        vshlq_n_u16(vshrq_n_u16(g8, 2), 5)),
                                        vshrq_n_u16(b8, 3));
                    vst1q_u16((uint16_t*)(drow + x), out);
                }
            } else if (width * 2 == NV_FRAME_WIDTH * 3) {
                /* MiSTer #9 (2026-06-16): NEON 3:2 area-average box (16bpp BGR565,
                 * 480x.. -> 320x224, 1.5x X). Each dest PAIR (2p,2p+1) spans 3 src
                 * px: dest 2p = src 3p (hcnt=1), dest 2p+1 = src 3p+1 + 3p+2
                 * (hcnt=2) -- exactly the scalar box's x0/x1 for width=480.
                 * vld3q_u16 gives the 3 taps; even/odd accumulate separately (they
                 * have different divisors), vst2q_u16 re-interleaves the pair.
                 * Byte-identical to the scalar box. Processes 16 dest (8 pairs)/iter. */
                int yy0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
                int yy1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
                if (yy1 <= yy0) yy1 = yy0 + 1;
                if (yy1 > height) yy1 = height;
                if (yy0 >= height) yy0 = height - 1;
                int vcnt = yy1 - yy0;
                const uint8_t* sbase = src + (size_t)yy0 * pitch;
                uint32_t rc_e = (uint32_t)((1u << 20) / ((uint32_t)1 * (uint32_t)vcnt)); /* hcnt=1 */
                uint32_t rc_o = (uint32_t)((1u << 20) / ((uint32_t)2 * (uint32_t)vcnt)); /* hcnt=2 */

                /* 160 pairs = NV_FRAME_WIDTH/2; even/odd dest accumulated apart. */
                uint16_t er[NV_FRAME_WIDTH/2], eg[NV_FRAME_WIDTH/2], eb[NV_FRAME_WIDTH/2];
                uint16_t orr[NV_FRAME_WIDTH/2], og[NV_FRAME_WIDTH/2], ob[NV_FRAME_WIDTH/2];
                memset(er,0,sizeof(er)); memset(eg,0,sizeof(eg)); memset(eb,0,sizeof(eb));
                memset(orr,0,sizeof(orr)); memset(og,0,sizeof(og)); memset(ob,0,sizeof(ob));
                const uint16x8_t m5 = vdupq_n_u16(0x001F);
                const uint16x8_t m6 = vdupq_n_u16(0x003F);
                const uint8_t* rowp = sbase;
                for (int syy = 0; syy < vcnt; syy++) {
                    const uint16_t* sp = (const uint16_t*)rowp;
                    for (int x = 0; x < NV_FRAME_WIDTH; x += 16) {
                        int p = x >> 1;                       /* pair base index */
                        uint16x8x3_t g3 = vld3q_u16(sp + (size_t)(x * 3) / 2);
                        /* even tap = val[0]; odd taps = val[1] + val[2] */
                        uint16x8_t e_r5 = vandq_u16(g3.val[0], m5);
                        uint16x8_t e_g6 = vandq_u16(vshrq_n_u16(g3.val[0], 5), m6);
                        uint16x8_t e_b5 = vandq_u16(vshrq_n_u16(g3.val[0], 11), m5);
                        uint16x8_t hr_e = vorrq_u16(vshlq_n_u16(e_r5,3), vshrq_n_u16(e_r5,2));
                        uint16x8_t hg_e = vorrq_u16(vshlq_n_u16(e_g6,2), vshrq_n_u16(e_g6,4));
                        uint16x8_t hb_e = vorrq_u16(vshlq_n_u16(e_b5,3), vshrq_n_u16(e_b5,2));
                        uint16x8_t hr_o = vdupq_n_u16(0), hg_o = vdupq_n_u16(0), hb_o = vdupq_n_u16(0);
                        for (int t = 1; t < 3; t++) {
                            uint16x8_t pp = g3.val[t];
                            uint16x8_t r5 = vandq_u16(pp, m5);
                            uint16x8_t g6 = vandq_u16(vshrq_n_u16(pp,5), m6);
                            uint16x8_t b5 = vandq_u16(vshrq_n_u16(pp,11), m5);
                            hr_o = vaddq_u16(hr_o, vorrq_u16(vshlq_n_u16(r5,3), vshrq_n_u16(r5,2)));
                            hg_o = vaddq_u16(hg_o, vorrq_u16(vshlq_n_u16(g6,2), vshrq_n_u16(g6,4)));
                            hb_o = vaddq_u16(hb_o, vorrq_u16(vshlq_n_u16(b5,3), vshrq_n_u16(b5,2)));
                        }
                        vst1q_u16(&er[p], vaddq_u16(vld1q_u16(&er[p]), hr_e));
                        vst1q_u16(&eg[p], vaddq_u16(vld1q_u16(&eg[p]), hg_e));
                        vst1q_u16(&eb[p], vaddq_u16(vld1q_u16(&eb[p]), hb_e));
                        vst1q_u16(&orr[p], vaddq_u16(vld1q_u16(&orr[p]), hr_o));
                        vst1q_u16(&og[p], vaddq_u16(vld1q_u16(&og[p]), hg_o));
                        vst1q_u16(&ob[p], vaddq_u16(vld1q_u16(&ob[p]), hb_o));
                    }
                    rowp += pitch;
                }
                const uint32x4_t halfv = vdupq_n_u32(1u << 19);
                const uint32x4_t rce = vdupq_n_u32(rc_e), rco = vdupq_n_u32(rc_o);
                volatile uint16_t* drow = dst + (size_t)y * NV_FRAME_WIDTH;
                for (int x = 0; x < NV_FRAME_WIDTH; x += 16) {
                    int p = x >> 1;
                    /* helper: divide one acc-vector by rc and pack the 5/6-bit field shift */
                    #define DIV8(acc, rcv) ({ \
                        uint16x8_t _a = vld1q_u16(acc); \
                        uint32x4_t _l = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_low_u16(_a)),  rcv), halfv), 20); \
                        uint32x4_t _h = vshrq_n_u32(vaddq_u32(vmulq_u32(vmovl_u16(vget_high_u16(_a)), rcv), halfv), 20); \
                        vcombine_u16(vmovn_u32(_l), vmovn_u32(_h)); })
                    uint16x8_t er8 = DIV8(&er[p], rce), eg8 = DIV8(&eg[p], rce), eb8 = DIV8(&eb[p], rce);
                    uint16x8_t or8 = DIV8(&orr[p], rco), og8 = DIV8(&og[p], rco), ob8 = DIV8(&ob[p], rco);
                    #undef DIV8
                    uint16x8x2_t out;
                    out.val[0] = vorrq_u16(vorrq_u16(vshlq_n_u16(vshrq_n_u16(er8,3),11),
                                                     vshlq_n_u16(vshrq_n_u16(eg8,2),5)),
                                                     vshrq_n_u16(eb8,3));
                    out.val[1] = vorrq_u16(vorrq_u16(vshlq_n_u16(vshrq_n_u16(or8,3),11),
                                                     vshlq_n_u16(vshrq_n_u16(og8,2),5)),
                                                     vshrq_n_u16(ob8,3));
                    vst2q_u16((uint16_t*)(drow + x), out);
                }
            } else {
                /* MiSTer Path B Build 2: AREA-AVERAGE (box) 16-bit downscale,
                 * replacing nearest-neighbor for the squish case (He-Man
                 * 960x480 -> 320x224, 3x X). Source is BGR565; unpack to 8-bit
                 * R/G/B, average the output pixel's source-block footprint,
                 * pack to RGB565 (same output layout as the 32bpp box + the
                 * FPGA decoder). recip[] avoids a per-pixel divide (A9 has no
                 * HW integer divide). Scalar: the 16-bit vscreen already halved
                 * the bandwidth; a NEON box-16 is a later optimization. Self-
                 * degenerates to a copy on any axis not downscaled. */
                int yy0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
                int yy1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
                if (yy1 <= yy0) yy1 = yy0 + 1;
                if (yy1 > height) yy1 = height;
                if (yy0 >= height) yy0 = height - 1;
                int vcnt = yy1 - yy0;
                const uint8_t* sbase = src + (size_t)yy0 * pitch;
                uint32_t recip16[8];
                {
                    int h;
                    for (h = 1; h < 8; h++) recip16[h] = (uint32_t)((1u << 20) / ((uint32_t)h * (uint32_t)vcnt));
                }
                for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                    int x0 = (int)(((long)x * width) / NV_FRAME_WIDTH);
                    int x1 = (int)(((long)(x + 1) * width) / NV_FRAME_WIDTH);
                    if (x1 <= x0) x1 = x0 + 1;
                    if (x1 > width) x1 = width;
                    if (x0 >= width) x0 = width - 1;
                    int hcnt = x1 - x0;
                    if (hcnt > 7) hcnt = 7;
                    uint32_t rs = 0, gs = 0, bs = 0;
                    const uint8_t* rowp = sbase;
                    for (int syy = 0; syy < vcnt; syy++) {
                        const uint16_t* sp = (const uint16_t*)(rowp) + x0;
                        for (int k = 0; k < hcnt; k++) {
                            uint16_t pix = sp[k];
                            uint32_t b5 = (pix >> 11) & 0x1F;
                            uint32_t g6 = (pix >> 5) & 0x3F;
                            uint32_t r5 = pix & 0x1F;
                            rs += (r5 << 3) | (r5 >> 2);
                            gs += (g6 << 2) | (g6 >> 4);
                            bs += (b5 << 3) | (b5 >> 2);
                        }
                        rowp += pitch;
                    }
                    uint32_t rc = recip16[hcnt];
                    uint32_t r8 = (rs * rc + (1u << 19)) >> 20;
                    uint32_t g8 = (gs * rc + (1u << 19)) >> 20;
                    uint32_t b8 = (bs * rc + (1u << 19)) >> 20;
                    dst_row[x] = (uint16_t)(((r8 >> 3) << 11) | ((g8 >> 2) << 5) | (b8 >> 3));
                }
            }
        }
    }
    else if (bpp == 8 && palette) {
        /* 8bpp paletted — convert through palette to RGB565.
         * OpenBOR s_screen palette: 3 bytes per entry (R, G, B), 256 entries. */
        const uint8_t* src = (const uint8_t*)pixels;
        const uint8_t* pal = (const uint8_t*)palette;
        /* nv_top: skip the notice band (8bpp paletted path). */
        for (int y = nv_top; y < NV_FRAME_HEIGHT; y++) {
            int src_y = (y * sy256) / 256;
            if (src_y >= height) src_y = height - 1;
            const uint8_t* row = src + src_y * pitch;
            volatile uint16_t* dst_row = dst + y * NV_FRAME_WIDTH;
            /* Step 20: uint64_t-packed writes (4 px per store). Palette
             * lookup gather makes NEON impractical; uint64_t packing alone
             * gives ~1.5-2x DDR3 write-side speedup. */
            for (int x = 0; x < NV_FRAME_WIDTH; x += 4) {
                uint16_t out[4];
                for (int k = 0; k < 4; k++) {
                    uint8_t idx = row[src_x_table[x + k]];
                    uint8_t r = pal[idx * 3 + 0];
                    uint8_t g = pal[idx * 3 + 1];
                    uint8_t b = pal[idx * 3 + 2];
                    out[k] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
                }
                uint64_t packed = ((uint64_t)out[0]) | ((uint64_t)out[1] << 16)
                                | ((uint64_t)out[2] << 32) | ((uint64_t)out[3] << 48);
                *(volatile uint64_t*)(dst_row + x) = packed;
            }
        }
    }
    else if (bpp == 32) {
        /* 32bpp RGBA -- byte-0=R, byte-1=G, byte-2=B, byte-3=A.
         *
         * MiSTer 2026-06-06 (v3.2 quality): AREA-AVERAGE (box filter) downscale,
         * replacing the nearest-neighbor src_x_table path for the 32bpp case.
         * The v3.9/v3.10 palette pipeline forces hi-res PAKs (He-Man 960x480,
         * Avengers/PDC2 480x272) through PIXEL_32, so this is the path where
         * downscale quality matters most on a CRT. NN kept only 1 of every 3
         * He-Man columns (aliased/janky text); box average folds every source
         * pixel in each output pixel's footprint into the result.
         *
         * Separable box, single divide: accumulate the raw 2D block sum, then
         * divide ONCE per output pixel via a reciprocal-multiply keyed on the
         * block area (h*vcnt). Fewer multiplies than per-row averaging and one
         * rounding step instead of two. The horizontal block table is cached
         * across frames (recomputed only on a resolution change). The filter
         * self-degenerates to a 1:1 copy on any axis that isn't downscaled
         * (ATOV is 320 wide -> hcnt==1), so near-native PAKs look identical.
         *
         * 8bpp/16bpp paths still use NN (src_x_table) -- extended in a later
         * step. See CLAUDE.md video-pipeline section (this reverses the prior
         * "NN not bilinear" squish decision for sub-native downscale). */
        const uint8_t* src = (const uint8_t*)pixels;

        /* Cached horizontal block table: source column span [hx0, hx0+hcnt)
         * per output column. Only recomputed when the PAK width changes, not
         * per frame. static is safe -- WriteFrame runs only on the render
         * thread; the keepalive thread uses KeepaliveTick and never enters. */
        static uint16_t s_hx0[NV_FRAME_WIDTH];
        static uint16_t s_hcnt[NV_FRAME_WIDTH];
        static int s_htab_width   = -1;
        static int s_htab_maxhcnt = 1;
        if (width != s_htab_width) {
            int maxh = 1;
            for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                int x0 = (int)(((long)x * width) / NV_FRAME_WIDTH);
                int x1 = (int)(((long)(x + 1) * width) / NV_FRAME_WIDTH);
                if (x1 <= x0) x1 = x0 + 1;
                if (x1 > width)  x1 = width;
                if (x0 >= width) x0 = width - 1;
                s_hx0[x]  = (uint16_t)x0;
                s_hcnt[x] = (uint16_t)(x1 - x0);
                if ((x1 - x0) > maxh) maxh = x1 - x0;
            }
            if (maxh > 63) maxh = 63;   /* recip[] table bound (defensive) */
            s_htab_width   = width;
            s_htab_maxhcnt = maxh;
        }

        /* Raw 2D block-sum accumulators; one divide per output pixel. uint32
         * is ample: block_sum <= block_area * 255 (~7650 at 1080p source). */
        uint32_t vr[NV_FRAME_WIDTH], vg[NV_FRAME_WIDTH], vb[NV_FRAME_WIDTH];

        /* Averaged 8-bit RGB intermediate. Pass 1 fills it from the box
         * average; pass 2 reads 3x3 neighborhoods for the unsharp + packs to
         * DDR3. static (render-thread only). ~215 KB. */
        static uint8_t s_avg[NV_FRAME_HEIGHT * NV_FRAME_WIDTH * 3];

        /* PASS 1: box area-average -> 8-bit s_avg (no DDR3 write yet).
         *
         * nv_top: skipped for the same rows as PASS 2. This pass writes the
         * static s_avg intermediate rather than DDR3, so skipping is a pure
         * saving here -- but the two passes MUST use the same start row, or
         * PASS 2 would publish a band of the previous frame's averages. */
        for (int y = nv_top; y < NV_FRAME_HEIGHT; y++) {
            int y0 = (int)(((long)y * height) / NV_FRAME_HEIGHT);
            int y1 = (int)(((long)(y + 1) * height) / NV_FRAME_HEIGHT);
            if (y1 <= y0) y1 = y0 + 1;
            if (y1 > height)  y1 = height;
            if (y0 >= height) y0 = height - 1;
            int vcnt = y1 - y0;

            /* >4x downscale (e.g. Lust Rush 1920x1080): cap the box to a 4x4
             * evenly-spaced sample grid per output pixel so read cost stays
             * bounded regardless of ratio. Exact for blocks <=4x4; a clean
             * subsample above that. Only fires when source >1280 wide or >896
             * tall -- every PAK <=960x480 keeps the exact box/NEON path below. */
            if (width > NV_FRAME_WIDTH * 4 || height > NV_FRAME_HEIGHT * 4) {
                uint8_t* arow = s_avg + (size_t)y * NV_FRAME_WIDTH * 3;
                int bh = y1 - y0;
                for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                    int x0 = (int)(((long)x * width) / NV_FRAME_WIDTH);
                    int x1 = (int)(((long)(x + 1) * width) / NV_FRAME_WIDTH);
                    if (x1 <= x0) x1 = x0 + 1;
                    if (x1 > width)  x1 = width;
                    if (x0 >= width) x0 = width - 1;
                    int bw = x1 - x0;
                    uint32_t rs = 0, gs = 0, bs = 0;
                    for (int j = 0; j < 4; j++) {
                        int sy = y0 + (j * bh) / 4;
                        if (sy >= height) sy = height - 1;
                        const uint8_t* row = src + (size_t)sy * pitch;
                        for (int k = 0; k < 4; k++) {
                            int sx = x0 + (k * bw) / 4;
                            if (sx >= width) sx = width - 1;
                            const uint8_t* p = row + (size_t)sx * 4;
                            rs += p[0]; gs += p[1]; bs += p[2];
                        }
                    }
                    rs = (rs + 8) >> 4;   /* divide by 16 (4x4 samples), rounded */
                    gs = (gs + 8) >> 4;
                    bs = (bs + 8) >> 4;
                    arow[x * 3 + 0] = (uint8_t)rs;
                    arow[x * 3 + 1] = (uint8_t)gs;
                    arow[x * 3 + 2] = (uint8_t)bs;
                }
                continue;   /* stride-cap wrote s_avg directly; skip the box path */
            }

            /* Combined reciprocal per distinct horizontal block width:
             * recip[h] = (1<<20) / (h * vcnt). out = (block_sum*recip + half)
             * >> 20 = block_sum / (h*vcnt). Only a few distinct h values, so
             * this replaces a per-pixel integer divide. */
            uint32_t recip[64];
            for (int h = 1; h <= s_htab_maxhcnt; h++) {
                recip[h] = (uint32_t)((1u << 20) / ((uint32_t)h * (uint32_t)vcnt));
            }

            memset(vr, 0, sizeof(vr));
            memset(vg, 0, sizeof(vg));
            memset(vb, 0, sizeof(vb));

            /* Accumulate raw block sums across the source-row band (no divide). */
#ifdef __ARM_NEON
            if (width == NV_FRAME_WIDTH * 3) {
                /* MiSTer 2026-06-07: exact 3x horizontal box via NEON (He-Man
                 * 960->320). Deinterleave RGBA -> R/G/B planes (vld4q), then
                 * vld3 deinterleave-by-3 yields group-of-3 sums directly --
                 * byte-identical to the scalar box for hcnt==3 (buffer-verified).
                 * Only the exact 3x case qualifies (320*3==960, 48-aligned, no
                 * boundary spill); every other ratio uses the scalar path. */
                static uint8_t planeR[NV_FRAME_WIDTH * 3];
                static uint8_t planeG[NV_FRAME_WIDTH * 3];
                static uint8_t planeB[NV_FRAME_WIDTH * 3];
                for (int sy = y0; sy < y1; sy++) {
                    const uint8_t* row = src + (size_t)sy * pitch;
                    for (int sx = 0; sx < NV_FRAME_WIDTH * 3; sx += 16) {
                        uint8x16x4_t px = vld4q_u8(row + (size_t)sx * 4);
                        vst1q_u8(planeR + sx, px.val[0]);
                        vst1q_u8(planeG + sx, px.val[1]);
                        vst1q_u8(planeB + sx, px.val[2]);
                    }
                    for (int x = 0; x < NV_FRAME_WIDTH; x += 16) {
                        int sx = x * 3;
                        uint8x16x3_t gr = vld3q_u8(planeR + sx);
                        uint8x16x3_t gg = vld3q_u8(planeG + sx);
                        uint8x16x3_t gb = vld3q_u8(planeB + sx);
                        uint16x8_t rlo = vaddw_u8(vaddl_u8(vget_low_u8(gr.val[0]),  vget_low_u8(gr.val[1])),  vget_low_u8(gr.val[2]));
                        uint16x8_t rhi = vaddw_u8(vaddl_u8(vget_high_u8(gr.val[0]), vget_high_u8(gr.val[1])), vget_high_u8(gr.val[2]));
                        uint16x8_t glo = vaddw_u8(vaddl_u8(vget_low_u8(gg.val[0]),  vget_low_u8(gg.val[1])),  vget_low_u8(gg.val[2]));
                        uint16x8_t ghi = vaddw_u8(vaddl_u8(vget_high_u8(gg.val[0]), vget_high_u8(gg.val[1])), vget_high_u8(gg.val[2]));
                        uint16x8_t blo = vaddw_u8(vaddl_u8(vget_low_u8(gb.val[0]),  vget_low_u8(gb.val[1])),  vget_low_u8(gb.val[2]));
                        uint16x8_t bhi = vaddw_u8(vaddl_u8(vget_high_u8(gb.val[0]), vget_high_u8(gb.val[1])), vget_high_u8(gb.val[2]));
                        vst1q_u32(&vr[x],      vaddq_u32(vld1q_u32(&vr[x]),      vmovl_u16(vget_low_u16(rlo))));
                        vst1q_u32(&vr[x + 4],  vaddq_u32(vld1q_u32(&vr[x + 4]),  vmovl_u16(vget_high_u16(rlo))));
                        vst1q_u32(&vr[x + 8],  vaddq_u32(vld1q_u32(&vr[x + 8]),  vmovl_u16(vget_low_u16(rhi))));
                        vst1q_u32(&vr[x + 12], vaddq_u32(vld1q_u32(&vr[x + 12]), vmovl_u16(vget_high_u16(rhi))));
                        vst1q_u32(&vg[x],      vaddq_u32(vld1q_u32(&vg[x]),      vmovl_u16(vget_low_u16(glo))));
                        vst1q_u32(&vg[x + 4],  vaddq_u32(vld1q_u32(&vg[x + 4]),  vmovl_u16(vget_high_u16(glo))));
                        vst1q_u32(&vg[x + 8],  vaddq_u32(vld1q_u32(&vg[x + 8]),  vmovl_u16(vget_low_u16(ghi))));
                        vst1q_u32(&vg[x + 12], vaddq_u32(vld1q_u32(&vg[x + 12]), vmovl_u16(vget_high_u16(ghi))));
                        vst1q_u32(&vb[x],      vaddq_u32(vld1q_u32(&vb[x]),      vmovl_u16(vget_low_u16(blo))));
                        vst1q_u32(&vb[x + 4],  vaddq_u32(vld1q_u32(&vb[x + 4]),  vmovl_u16(vget_high_u16(blo))));
                        vst1q_u32(&vb[x + 8],  vaddq_u32(vld1q_u32(&vb[x + 8]),  vmovl_u16(vget_low_u16(bhi))));
                        vst1q_u32(&vb[x + 12], vaddq_u32(vld1q_u32(&vb[x + 12]), vmovl_u16(vget_high_u16(bhi))));
                    }
                }
            } else
#endif
            {
                for (int sy = y0; sy < y1; sy++) {
                    const uint8_t* row = src + (size_t)sy * pitch;
                    for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                        const uint8_t* p = row + (size_t)s_hx0[x] * 4;
                        uint32_t rs = 0, gs = 0, bs = 0;
                        int n = s_hcnt[x];
                        for (int k = 0; k < n; k++) {
                            rs += p[0]; gs += p[1]; bs += p[2];
                            p += 4;
                        }
                        vr[x] += rs; vg[x] += gs; vb[x] += bs;
                    }
                }
            }

            /* One rounded divide per output pixel -> store 8-bit RGB. */
            uint8_t* arow = s_avg + (size_t)y * NV_FRAME_WIDTH * 3;
            for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                uint32_t rc = recip[s_hcnt[x]];
                uint32_t r = (vr[x] * rc + (1u << 19)) >> 20;
                uint32_t g = (vg[x] * rc + (1u << 19)) >> 20;
                uint32_t b = (vb[x] * rc + (1u << 19)) >> 20;
                if (r > 255) r = 255;
                if (g > 255) g = 255;
                if (b > 255) b = 255;
                arow[x * 3 + 0] = (uint8_t)r;
                arow[x * 3 + 1] = (uint8_t)g;
                arow[x * 3 + 2] = (uint8_t)b;
            }
        }

        /* PASS 2: pack the box-averaged image straight to RGB565 — NO sharpen.
         * (2026-06-08: the cross-Laplacian unsharp was removed; it read jaggier
         * than 4086 on ATOV. The box area-average in PASS 1 is the only filter.)
         *
         * nv_top: skip the notice band. This is the 32bpp path's only DDR3
         * write, and it must start on the same row as PASS 1. */
        for (int y = nv_top; y < NV_FRAME_HEIGHT; y++) {
            const uint8_t* arow = s_avg + (size_t)y * NV_FRAME_WIDTH * 3;
            volatile uint16_t* dst_row = dst + y * NV_FRAME_WIDTH;
            for (int x = 0; x < NV_FRAME_WIDTH; x++) {
                uint8_t r = arow[x * 3 + 0];
                uint8_t g = arow[x * 3 + 1];
                uint8_t b = arow[x * 3 + 2];
                dst_row[x] = (uint16_t)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
            }
        }
    }
    else {
        return;  /* unsupported format, skip frame */
    }

    /* Step 20 (2026-05-27) defensive barrier: ensure ALL pixel writes
     * (scalar, uint64_t-packed, OR NEON 128-bit) drain to DDR3 BEFORE
     * the FPGA sees the new ctrl word and starts reading the buffer we
     * just finished writing. The buffer rotation already protects
     * against most tearing (FPGA reads OPPOSITE buffer from the one we
     * write), but NEON stores can drain through the write-combine buffer
     * at a different rate than scalar stores -- if ctrl is updated before
     * the buffer fully drains AND the FPGA pipeline races ahead, the very
     * first lines of the new frame could read partially-written pixels.
     * __sync_synchronize() generates ARMv7 DMB SY (full memory barrier);
     * costs ~2 cycles, negligible. */
    /* Overlays: after every pixel path, before the publish barrier, so they
     * land in the frame the FPGA is about to scan out. */
    NativeVideoWriter_DrawOverlays(dst);

    /* The per-frame publisher tag that lived here is REMOVED. It answered its
     * question: frames carrying neither tag turned out to be a CAPTURE artifact
     * (fbdump reads ~400 KB while the buffer is rewritten), not a third writer.
     * The real defect is that overlays are dropped whenever the FPGA scans a
     * buffer between the game-pixel write and DrawOverlays -- see
     * #TRIPLE_BUFFER_SCOPE.md. Do not reinstate it to chase that; it corrupts
     * the very pixels the fix is about. */

    __sync_synchronize();

    /* Flip control word.
     *
     * Hand the finished buffer to the keepalive BEFORE publishing it, so a tick
     * landing anywhere around here republishes a fully-drawn buffer -- either
     * this one or the previous one. Both are complete; neither is the one we are
     * about to fill next. */
    /* & 1 is correct at TWO buffers. If a third is ever added this MUST widen to
     * & 3 -- at N=3 a 1-bit mask records BUF2 as 0 and the keepalive then
     * republishes BUF0, two frames stale. That bug was found and fixed during the
     * 2026-08-06 three-buffer attempt, which was then reverted. */
    nv_last_published = active_buf & 1;
    __sync_synchronize();

    uint32_t fc = __sync_add_and_fetch(&frame_counter, 1);
    volatile uint32_t* ctrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
    *ctrl = (fc << 2) | (active_buf & 1);
    active_buf ^= 1;
}

bool NativeVideoWriter_IsActive(void) {
    return ddr_base != NULL;
}

/* The SDL-dummy publisher (mister_present) keeps its own frame counter and
 * active-buffer state and writes the same ctrl word. It must hand its finished
 * buffer over too, or a keepalive tick right after one of its frames would
 * republish a buffer from the OTHER publisher and flip the image. */
void NativeVideoWriter_NotePublished(int buf) { nv_last_published = buf & 1; }

void NativeVideoWriter_KeepaliveTick(void) {
    /* Republish the last COMPLETED buffer so the FPGA's ~500 ms staleness
     * timeout never blanks the screen during a long write-pause (cart load,
     * PAK swap, wait-for-.s0).
     *
     * 🛑 Take the buffer from nv_last_published -- do NOT derive it here as
     * `(!active_buf)`. This runs on a separate pthread, so active_buf can toggle
     * between the read and the ctrl write, and the derived value then names the
     * buffer WriteFrame is about to fill. That published an UNDRAWN buffer and
     * was the notice flicker: measured 2026-08-05, every frame inside a flicker
     * gap came from this path (3/3), carrying neither publisher's tag.
     *
     * frame_counter is shared with WriteFrame across threads, so bump it
     * atomically; a plain ++ can drop an increment, and the FPGA only needs the
     * value to CHANGE, but there is no reason to leave the race in. */
    if (!ddr_base) return;
    uint32_t fc = __sync_add_and_fetch(&frame_counter, 1);
    volatile uint32_t* ctrl = (volatile uint32_t*)(ddr_base + NV_CTRL_OFFSET);
    *ctrl = (fc << 2) | (nv_last_published & 1);
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

/* OSD "Replay Slot" / "Play Replay" -> ARM. Raw word; the caller decodes.
 *   bits [1:0]  cmd   0 = idle, 1 = play
 *   bits [10:8] slot  0..7
 *   bits [23:16] seq  bumped by the FPGA on every captured Play pulse, so a
 *                     repeat of the SAME slot still reads as a new event. */
uint32_t NativeVideoWriter_ReadReplay(void) {
    if (!ddr_base) return 0;
    volatile uint32_t *rs = (volatile uint32_t *)(ddr_base + NV_REPLAY_OFFSET);
    return *rs;
}

/* Pause-menu slot -> OSD. The FPGA edge-detects `seq` and pushes the new slot
 * into the OSD's status word, which is what makes the two pickers show the
 * same number.
 *
 * `slot` is the user-facing 1..8; it is converted to the 0-based wire value
 * here so exactly one place in the codebase knows about the offset.
 *
 * 🛑 THE SEQUENCE IS DERIVED FROM THE WIRE, NEVER FROM A COUNTER, and the
 * caller does not get to supply it. This used to take a `seq` argument fed by
 * a function-local `static` in the caller -- which restarts at 0 in every new
 * process, while 0x04 SURVIVES the _exit()/respawn that Record and Play both
 * perform. So a fresh process could publish a value already sitting on the
 * wire, and the FPGA -- being an edge detector on this byte -- saw no change
 * at all. The publish was dropped, the poll then adopted the unchanged echo
 * and REVERTED the user's press, and the next Record wrote to the wrong slot,
 * destroying whatever take was in it.
 *
 * Reading back is sound: the FPGA only ever READS 0x04, so this byte is
 * whatever the ARM last wrote (or stale DDR3 at first boot, which is equally
 * fine to increment from). Being one-more-than-what-is-there cannot collide
 * with what is there. */
void NativeVideoWriter_PublishReplaySlot(int slot) {
    if (!ddr_base) return;
    if (slot < 1) slot = 1;
    if (slot > 8) slot = 8;
    volatile uint32_t *pub = (volatile uint32_t *)(ddr_base + NV_REPLAY_PUB_OFFSET);
    uint32_t next = ((*pub >> 8) + 1u) & 0xFFu;
    *pub = (uint32_t)((slot - 1) & 7) | (next << 8);
}
