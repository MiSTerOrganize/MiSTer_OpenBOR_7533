//============================================================================
//
//  OpenBOR Native Video DDR3 Reader  (Step 60 / Option Y)
//
//  Reads VARIABLE-RES RGB565 source frames from DDR3 and streams source
//  pixels through line_fifo at native source resolution. The downstream
//  downscale module (Phase 4 — openbor_video_downscale.sv) consumes from
//  line_fifo, buffers N source lines in M10K, performs edge-aware 4x4
//  polyphase downscale to the 320x224 display target.
//
//  Differences from the pre-Step-60 fixed-320x224 reader:
//
//   * Reads the DIM ctrl word at DDR3 byte 0x04 (= upper 32 bits of the
//     same qword as the CTRL word at byte 0x00). The DIM word is laid out
//     as ((height << 16) | width). One DDR3 read fetches both CTRL and
//     DIM — no extra cycle cost.
//
//   * Source line count = src_height (from DIM), not hardcoded V_ACTIVE.
//
//   * Qwords-per-line = ceil(src_width / 4) (since RGB565 packs 4 pixels
//     per 64-bit DDR3 word). For 320-wide source this is 80 qwords/line,
//     matching the pre-Step-60 LINE_BURST constant.
//
//   * Multi-burst per line: an 8-bit ddr_burstcnt caps a single transfer
//     at 255 qwords. Sources wider than 1020 pixels (e.g., 1920-wide
//     hypothetical PAKs) need >1 burst per source line. State machine
//     tracks line_qwords_remaining and fires multiple ST_READ_LINE
//     iterations per logical line. For He-Man (960-wide = 240 qwords)
//     and below this is one burst per line — unchanged from before.
//
//   * Per-line DDR3 address: buf_base + (src_line × qwords_per_line).
//     Computed at frame start so the per-line address is a single
//     32-bit add rather than a multiply each line.
//
//   * Source dimensions are exported as src_width_o / src_height_o for
//     the downscale module to drive its pipelining and pixel mapping.
//     src_frame_start_o pulses once per frame at the start of source
//     read; src_line_done_o pulses after each source line completes.
//
//  Cart loading via ioctl is PRESERVED. PAK byte capture, addressing,
//  flow control via ioctl_wait — all unchanged from previous releases.
//
//  DDR3 Memory Map  (must match src/native_video_writer.c constants):
//    0x3A000000 + 0x000     : CTRL  (frame_counter[31:2] | active_buf[1:0])
//    0x3A000000 + 0x004     : DIM   (height[31:16] | width[15:0])    NEW
//    0x3A000000 + 0x008     : Joystick P1 (FPGA writes, ARM reads)
//    0x3A000000 + 0x010     : Cart control (file_size, ARM polls)
//    0x3A000000 + 0x018     : Joystick P2
//    0x3A000000 + 0x020     : Joystick P3
//    0x3A000000 + 0x028     : Joystick P4
//    0x3A000000 + 0x030     : Audio ring wr_ptr (ARM writes)
//    0x3A000000 + 0x038     : Audio ring rd_ptr (FPGA writes)
//    0x3A000000 + 0x040     : BUF0  (up to 1920x1080 RGB565 = 4.15 MB)
//    0x3A000000 + 0x400040  : BUF1  4MB-stride per buffer            MOVED
//    0x3A000000 + 0x800040  : Cart data buffer (1 MB region)         MOVED
//    0x3A000000 + 0x900040  : Audio ring buffer (64 KiB)             MOVED
//
//  Bandwidth at max source res (960x480 = He-Man):
//    960 × 480 × 2 = 921,600 bytes per frame
//    × 60 fps      = 55.3 MB/s
//  DDR3 capacity is multiple GB/s, so even max-source PAKs are well
//  within budget. Even a hypothetical 1920x1080 source would only need
//  249 MB/s.
//
//  Adapted from MiSTer_PICO-8 by MiSTer Organize
//  Copyright (C) 2026 MiSTer Organize -- GPL-3.0
//
//============================================================================

module openbor_video_reader (
    // DDR3 Avalon-MM master
    input  wire        ddr_clk,
    input  wire        ddr_busy,
    output reg   [7:0] ddr_burstcnt,
    output reg  [28:0] ddr_addr,
    input  wire [63:0] ddr_dout,
    input  wire        ddr_dout_ready,
    output reg         ddr_rd,
    output reg  [63:0] ddr_din,
    output wire  [7:0] ddr_be,
    output reg         ddr_we,

    // Pixel output (clk_vid domain)
    input  wire        clk_vid,
    input  wire        ce_pix,
    input  wire        reset,

    // Timing inputs (from openbor_video_timing)
    input  wire        de,
    input  wire        hblank,
    input  wire        vblank,
    input  wire        new_frame,
    input  wire        new_line,
    input  wire  [8:0] vcount,

    // Cart loading via ioctl (from hps_io)
    input  wire        ioctl_download,
    input  wire        ioctl_wr,
    input  wire [26:0] ioctl_addr,
    input  wire  [7:0] ioctl_dout,
    output wire        ioctl_wait,

    // Joystick input for all 4 players (from hps_io, clk_sys domain = ddr_clk domain)
    input  wire [31:0] joystick_0,
    input  wire [31:0] joystick_1,
    input  wire [31:0] joystick_2,
    input  wire [31:0] joystick_3,
    input  wire [15:0] joystick_l_analog_0,

    // Source-pixel stream output (clk_vid domain).
    // Phase 4: downscale module consumes from line_fifo via these ports.
    // Read interface: assert src_fifo_rd to pop one qword (4 RGB565 pixels).
    input  wire        src_fifo_rd_i,
    output wire [63:0] src_fifo_rd_data_o,
    output wire        src_fifo_empty_o,

    // Audio output (clk_audio domain)
    input  wire        clk_audio,       // 24.576 MHz
    output reg  [15:0] audio_l,
    output reg  [15:0] audio_r,

    // Control
    input  wire        enable,
    output wire        frame_ready,

    // ----- Step 60 / Option Y: source dimensions + frame-pacing -----
    // For Phase 4 downscale module. Currently unconnected at top-level
    // (downscale module not yet instantiated) — port stubs prepared.
    output reg  [10:0] src_width_o,        // 1..1920
    output reg  [10:0] src_height_o,       // 1..1080
    output reg         src_frame_start_o,  // pulses 1 ddr_clk at start of a new src frame read
    output reg         src_line_done_o,    // pulses 1 ddr_clk when src line read fills FIFO

    /* TEMPORARY DIAG: slot_src_line[0..4] packed (5 × 11 bits = 55 bits),
     * driven by downscale module via top.sv. CDC clk_vid -> clk_sys is
     * unsynchronized (acceptable for diag — slot_src_line changes slowly
     * relative to probe period). REVERT AFTER MEASURED. */
    input  wire [54:0] dbg_slot_src_line_packed_i,

    /* TEMPORARY DIAG v2: V-pass state snapshot at dest_line 100. 69 bits
     * (v3 src_target snap reverted to reclaim pll_hdmi slack). */
    input  wire [68:0] dbg_vpass_snap_dest100_i,

    /* Phase 5 fix (2026-06-04): V-pass's dest_line gray-coded for safe
     * multi-bit CDC clk_vid -> clk_sys. Reader uses this to compute
     * src_target directly, replacing phase-misaligned pulse accumulator. */
    input  wire [10:0] dest_line_gray_i
);

// DDR3 byte enable (always all bytes)
assign ddr_be  = 8'hFF;

// -- DDR3 Address Constants --------------------------------------------
// 29-bit qword addresses = physical >> 3.
//
//   Physical          Qword (>>3)     Purpose
//   0x3A000000        0x07400000      CTRL (low 32 bits) + DIM (high 32 bits)
//   0x3A000008        0x07400001      Joystick P1
//   0x3A000010        0x07400002      Cart control
//   0x3A000018        0x07400003      Joystick P2
//   0x3A000020        0x07400004      Joystick P3
//   0x3A000028        0x07400005      Joystick P4
//   0x3A000030        0x07400006      Audio wr_ptr
//   0x3A000038        0x07400007      Audio rd_ptr
//   0x3A000040        0x07400008      BUF0 base
//   0x3A400040        0x07480008      BUF1 base (4MB stride from BUF0)
//   0x3A800040        0x07500008      Cart data buffer
//   0x3A900040        0x07520008      Audio ring buffer
//
localparam [28:0] CTRL_ADDR      = 29'h07400000;
localparam [28:0] JOY0_ADDR      = 29'h07400001;
localparam [28:0] CART_CTRL_ADDR = 29'h07400002;
localparam [28:0] JOY1_ADDR      = 29'h07400003;
localparam [28:0] JOY2_ADDR      = 29'h07400004;
localparam [28:0] JOY3_ADDR      = 29'h07400005;
localparam [28:0] AUDIO_WR_ADDR   = 29'h07400006;
localparam [28:0] AUDIO_RD_ADDR   = 29'h07400007;
localparam [28:0] BUF0_ADDR      = 29'h07400008;
localparam [28:0] BUF1_ADDR      = 29'h07480008;  // MOVED (was 0x07408008)
localparam [28:0] CART_DATA_ADDR = 29'h07500008;  // MOVED (was 0x07410000)
localparam [28:0] AUDIO_RING_ADDR = 29'h07520008; // MOVED (was 0x0741A000)
localparam [31:0] AUDIO_RING_BYTES = 32'h00010000; // 64 KiB
localparam [31:0] AUDIO_RING_MASK  = 32'h0000FFFF;

// Audio refill threshold: trigger a fetch when FIFO has < this qwords used.
// FIFO is 1024 entries deep; 384 leaves headroom for steady-state mixing.
localparam [9:0]  AUDIO_REFILL_THRESHOLD = 10'd384;

// Max qwords per single DDR3 burst (ddr_burstcnt is 8-bit; 255 max).
// For sources <= 1020 pixels wide this is enough for one burst per source
// line. Wider sources are split across multiple bursts within the same
// logical source line — line_qwords_remaining tracks the remainder.
localparam [7:0]  MAX_BURST_QW = 8'd240;

// Default DIM (320 x 224) — used until the first real CTRL+DIM read.
// Init in native_video_writer.c writes this same value at boot.
localparam [10:0] DEFAULT_WIDTH  = 11'd320;
localparam [10:0] DEFAULT_HEIGHT = 11'd224;

localparam [19:0] TIMEOUT_MAX = 20'hF_FFFF;

// -- Enable synchronizer ----------------------------------------------
reg [1:0] enable_sync;
always @(posedge ddr_clk) begin
    if (reset)
        enable_sync <= 2'b0;
    else
        enable_sync <= {enable_sync[0], enable};
end
wire enable_ddr = enable_sync[1];

// -- CDC: new_frame ----------------------------------------------------
reg [1:0] new_frame_sync;
always @(posedge ddr_clk) begin
    if (reset)
        new_frame_sync <= 2'b0;
    else
        new_frame_sync <= {new_frame_sync[0], new_frame};
end
wire new_frame_ddr = ~new_frame_sync[1] & new_frame_sync[0];

reg new_frame_pending;
reg synced;  // Set after first ctrl read -- prevents stale-DDR3 display

// -- CDC: new_line -----------------------------------------------------
reg [1:0] new_line_sync;
always @(posedge ddr_clk) begin
    if (reset)
        new_line_sync <= 2'b0;
    else
        new_line_sync <= {new_line_sync[0], new_line};
end
wire new_line_ddr = ~new_line_sync[1] & new_line_sync[0];

// -- CDC: vblank level -------------------------------------------------
reg [1:0] vblank_sync;
always @(posedge ddr_clk) begin
    if (reset)
        vblank_sync <= 2'b0;
    else
        vblank_sync <= {vblank_sync[0], vblank};
end
wire vblank_ddr = vblank_sync[1];

// -- Reset synchronizer for clk_vid -----------------------------------
reg [1:0] reset_vid_sync;
always @(posedge clk_vid or posedge reset)
    if (reset) reset_vid_sync <= 2'b11;
    else       reset_vid_sync <= {reset_vid_sync[0], 1'b0};
wire reset_vid = reset_vid_sync[1];

// -- CDC: frame_ready --------------------------------------------------
reg frame_ready_reg;
reg [1:0] frame_ready_sync;
always @(posedge clk_vid) begin
    if (reset_vid)
        frame_ready_sync <= 2'b0;
    else
        frame_ready_sync <= {frame_ready_sync[0], frame_ready_reg};
end
wire frame_ready_vid = frame_ready_sync[1];
assign frame_ready = frame_ready_vid;

// -- DDR3 Read State Machine ------------------------------------------
localparam [4:0] ST_IDLE            = 5'd0;
localparam [4:0] ST_POLL_CTRL       = 5'd1;
localparam [4:0] ST_WAIT_CTRL       = 5'd2;
localparam [4:0] ST_CHECK_CTRL      = 5'd3;
localparam [4:0] ST_READ_LINE       = 5'd4;
localparam [4:0] ST_WAIT_LINE       = 5'd5;
localparam [4:0] ST_LINE_DONE       = 5'd6;
localparam [4:0] ST_WAIT_DISPLAY    = 5'd7;
localparam [4:0] ST_WRITE_JOY0      = 5'd8;
localparam [4:0] ST_WRITE_JOY1      = 5'd9;
localparam [4:0] ST_WRITE_JOY2      = 5'd10;
localparam [4:0] ST_WRITE_JOY3      = 5'd11;
localparam [4:0] ST_WRITE_CART      = 5'd12;
localparam [4:0] ST_WRITE_CART_SIZE = 5'd13;
localparam [4:0] ST_POLL_AUDIO_WR   = 5'd14;
localparam [4:0] ST_WAIT_AUDIO_WR   = 5'd15;
localparam [4:0] ST_PLAN_AUDIO      = 5'd16;
localparam [4:0] ST_READ_AUDIO_RING = 5'd17;
localparam [4:0] ST_WAIT_AUDIO_RING = 5'd18;
localparam [4:0] ST_WRITE_AUDIO_RD  = 5'd19;
/* TEMPORARY DIAG: reader state probe states. Writes 4 qwords to
 * DDR3 at PROBE_BASE_ADDR on every src_frame_start. ARM reads + logs.
 * REVERT AFTER MEASURED. */
localparam [4:0] ST_WRITE_PROBE      = 5'd20;
localparam [4:0] ST_WAIT_PROBE       = 5'd21;
localparam [28:0] PROBE_BASE_ADDR    = 29'h07580000;  /* byte 0x3AC00000 */
localparam [31:0] PROBE_MAGIC        = 32'hDEADBEEF;

reg  [4:0]  state;
reg  [31:0] ctrl_word;
reg  [29:0] prev_frame_counter;
reg         active_buffer;
reg  [28:0] buf_base_addr;
reg  [10:0] src_line;          // 0 .. src_height-1
reg  [7:0]  beat_count;
reg         first_frame_loaded;
reg  [4:0]  stale_vblank_count;
reg         preloading;
reg  [19:0] timeout_cnt;

// Step 60 / Option Y: variable-res registers.
reg  [10:0] src_width;          // 1..1920
reg  [10:0] src_height;         // 1..1080
reg  [9:0]  qwords_per_line;    // ceil(src_width / 4); 1..480
reg  [9:0]  line_qwords_remaining; // for multi-burst sources
reg  [28:0] line_qword_offset;  // accumulates within a line for multi-burst
reg  [28:0] line_base_addr;     // buf_base + (src_line * qwords_per_line)
reg  [7:0]  cur_burst;          // current burst length (1..MAX_BURST_QW)

// Step 60 v2 fix 2026-06-03: reader pacing for variable-res downscale.
//
// Previous pacing read 1 source line per active display line (and gated
// on !vblank_ddr). That worked for 1:1 V scale only. For sources with
// src_height > DEST_HEIGHT (e.g., He-Man 480 source lines to 224 dest
// lines = 2.14x downscale) the reader could only fetch 224 + 2 preload
// = 226 of 480 source lines per frame. The 5-slot ring buffer ended up
// holding only the LAST 5 source lines that were read, so V-pass at
// dest line N (which needs source lines around N*ratio) couldn't find
// the right lines in the ring -> find_slot defaulted to slot 0 -> all
// dest lines rendered with whatever line happened to be in slot 0 at
// the moment. Visible as severe garbled video on He-Man + lesser issue
// on near-1:1 ATOV (where most lines were caught up).
//
// Fix: pace by src/dest RATIO, not 1:1 with display lines.
//   src_target = D * (src_height / DEST_HEIGHT) + LOOKAHEAD
//   where D = dest_line counter tracked via new_line_ddr pulses
//
// With LOOKAHEAD=3 and 5-slot ring (mod-5 slot mapping), reader stays
// exactly 3 lines ahead of V-pass's center tap. V-pass needs 4 lines
// (kernel taps m-1..p+2 = 4 lines starting at D*ratio-1). Reader
// writes line K to slot K%5; with K = D*ratio+3, this slot is
// (D*ratio-2)%5 = ONE SLOT before V-pass's oldest needed slot (slot
// (D*ratio-1)%5). So reader's write slot is distinct from V-pass's
// 4 active read slots in the 5-slot ring -> no conflict.
localparam [10:0] LOOKAHEAD = 11'd3;
reg  [31:0] dest_phase_v;     // accumulated D * step_v in fixed-point
reg  [10:0] src_target;       // floor(dest_phase_v[26:16]) + LOOKAHEAD
/* TEMPORARY DIAG v3 REVERTED 2026-06-04: src_target_o removed */

/* TEMPORARY DIAG v4 REVERTED 2026-06-04: count_raw/count_active counters
 * removed after they confirmed CDC works (raw=262, active=224 per frame
 * = V_TOTAL / V_ACTIVE expected values). Reclaim ~0.1-0.15 ns of pll_hdmi
 * slack. VPASS@d100 snap probe retained for next-layer bug verification. */

/* TEMPORARY DIAG: probe state. probe_pending fires on src_frame_start
 * and clears after 4 qword writes complete. probe_idx selects which
 * qword to write. REVERT AFTER MEASURED. */
reg        probe_pending;
reg  [2:0] probe_idx;  /* 3 bits — supports up to 8 qwords (currently 6) */

// 2026-06-03 fix: original wire-based formula
//   wire [31:0] step_v_per_dest = ({src_height_o, 16'd0}) / DEFAULT_HEIGHT;
// inferred a combinational lpm_divide. At clk_sys 100 MHz (10 ns period)
// the divider's ~37 ns critical path blew timing (Quartus reported
// -37.073 ns slack). Replaced with multiplier-by-reciprocal:
//   step_v = src_height * (2^24 / DEFAULT_HEIGHT) >> 8
//          = src_height * (2^24 / 224) >> 8
//          = src_height * 74898 >> 8
// Reciprocal precomputed: 2^24 / 224 = 74898.286. Truncation to 74898
// gives <0.001% error per step, <1 source line accumulated over 1080
// max frame. The multiplier (11-bit x 17-bit) fits in one Cyclone V
// DSP block, single-cycle, comfortably meeting clk_sys timing.
wire [27:0] step_v_mul    = src_height_o * 17'd74898;
wire [31:0] step_v_per_dest = {12'd0, step_v_mul[27:8]};
wire [31:0] dest_phase_v_next = dest_phase_v + step_v_per_dest;

/* Phase 5 fix (2026-06-04): replace pulse-counted dest_phase_v with
 * V-pass dest_line CDC + multiplier. Bug was: dest_phase_v reset on
 * ARM frame bump (src_frame_start_o) is phase-misaligned with V-pass's
 * dest_line reset (FPGA new_frame). Pulse accumulator counted 224 active
 * pulses per frame correctly but they were not aligned with V-pass mid-
 * frame progress (snap_tgt=16 at V-pass dest=100 = pulses bunched late).
 *
 * Fix: gray-coded CDC of dest_line from V-pass, decoded to binary, then
 * multiplied by step_v to compute src_target directly. Reader pacing
 * now tracks V-pass exactly regardless of ARM frame phase. */
reg  [10:0] dest_line_gray_s1, dest_line_gray_s2;
always @(posedge ddr_clk) begin
    dest_line_gray_s1 <= dest_line_gray_i;
    dest_line_gray_s2 <= dest_line_gray_s1;
end

/* Gray-to-binary decoder (combinational unrolled XOR chain) */
wire [10:0] dest_line_bin;
assign dest_line_bin[10] = dest_line_gray_s2[10];
assign dest_line_bin[ 9] = dest_line_bin[10] ^ dest_line_gray_s2[ 9];
assign dest_line_bin[ 8] = dest_line_bin[ 9] ^ dest_line_gray_s2[ 8];
assign dest_line_bin[ 7] = dest_line_bin[ 8] ^ dest_line_gray_s2[ 7];
assign dest_line_bin[ 6] = dest_line_bin[ 7] ^ dest_line_gray_s2[ 6];
assign dest_line_bin[ 5] = dest_line_bin[ 6] ^ dest_line_gray_s2[ 5];
assign dest_line_bin[ 4] = dest_line_bin[ 5] ^ dest_line_gray_s2[ 4];
assign dest_line_bin[ 3] = dest_line_bin[ 4] ^ dest_line_gray_s2[ 3];
assign dest_line_bin[ 2] = dest_line_bin[ 3] ^ dest_line_gray_s2[ 2];
assign dest_line_bin[ 1] = dest_line_bin[ 2] ^ dest_line_gray_s2[ 1];
assign dest_line_bin[ 0] = dest_line_bin[ 1] ^ dest_line_gray_s2[ 0];

/* src_target = (dest_line_bin * step_v_per_dest) >> 16 + LOOKAHEAD.
 * step_v_per_dest fits in 20 bits (max 316644 for src_height=1080).
 *
 * 3-stage pipeline (REQUIRED for clk_sys timing closure — initial 1-stage
 * version failed at -0.511ns slack 2026-06-04 because gray-decode +
 * 11x20 multiply + adder exceeded 10ns clk_sys period):
 *
 *   Stage 0: register dest_line_bin  (breaks gray-decode chain)
 *   Stage 1: 11x20 multiply (inferred DSP slice, single cycle)
 *   Stage 2: register multiply output
 *   Stage 3: adder + register final src_target_computed
 *
 * Net latency: 3 clk_sys cycles (30 ns). Negligible vs ~64us per dest
 * line — pacing accuracy unaffected. */
reg  [10:0] dest_line_bin_r;
reg  [30:0] src_target_mul_r;
reg  [10:0] src_target_computed;
always @(posedge ddr_clk) begin
    /* Phase 8 (2026-06-04): use LOCAL reader_pulse_count instead of
     * gray-CDC'd dest_line_bin_r as multiplier input. CDC isn't broken
     * (Plan B snap confirmed dest_bin=98 / tgt_computed=107 always
     * correct at snap moment), but src_target_computed must be glitching
     * HIGH briefly somewhere EARLIER in the frame (causing reader to
     * burst all 240 lines), then settling back to 107 by snap time.
     *
     * Using reader_pulse_count eliminates ALL CDC paths from the
     * pacing input. It's a local counter in clk_sys reset on
     * new_frame_ddr, incremented on each active new_line_ddr pulse.
     * Earlier DIAG v4 confirmed pulse counts are reliable (262/224
     * per frame). No CDC = no glitch possible.
     *
     * dest_line_bin_r kept for diagnostic visibility but no longer
     * drives pacing. */
    dest_line_bin_r     <= dest_line_bin;
    src_target_mul_r    <= reader_pulse_count * step_v_per_dest[19:0];
    src_target_computed <= src_target_mul_r[26:16] + LOOKAHEAD;
end

/* Phase 7 v2: hold-counter for src_target mask. Asserts for 10 ddr_clk
 * cycles (~100ns) after new_frame_ddr. Covers the gray-code CDC settling
 * window (~30ns for dest_line_bin) + pipeline latency (4 stages = 40ns)
 * with margin. During the hold, src_target is forced to LOOKAHEAD in
 * the main always block (search for: src_target_force_cnt). */
reg [3:0] src_target_force_cnt;
always @(posedge ddr_clk or posedge reset) begin
    if (reset)
        src_target_force_cnt <= 4'd0;
    else if (new_frame_ddr)
        src_target_force_cnt <= 4'd10;
    else if (src_target_force_cnt != 4'd0)
        src_target_force_cnt <= src_target_force_cnt - 4'd1;
end

/* Plan B DIAG (2026-06-04): snapshot reader-side dest_line_bin_r and
 * src_target_computed at V-pass dest=99 moment. We count active
 * new_line_ddr pulses since the last new_frame_ddr — by the 99th pulse,
 * V-pass should be at dest=99 (transitioning to 100, which is when the
 * downscale snap fires). Snapshot both values then so we can compare
 * what reader THINKS vs what V-pass IS doing. */
reg [10:0] reader_pulse_count;
reg [10:0] snap_dest_line_bin_r;
reg [10:0] snap_src_target_computed;
always @(posedge ddr_clk or posedge reset) begin
    if (reset) begin
        reader_pulse_count       <= 11'd0;
        snap_dest_line_bin_r     <= 11'd0;
        snap_src_target_computed <= 11'd0;
    end else begin
        if (new_frame_ddr) begin
            reader_pulse_count <= 11'd0;
        end else if (new_line_ddr && !vblank_ddr) begin
            reader_pulse_count <= reader_pulse_count + 11'd1;
            /* On the 99th pulse-counted edge, snapshot reader's view. */
            if (reader_pulse_count == 11'd98) begin
                snap_dest_line_bin_r     <= dest_line_bin_r;
                snap_src_target_computed <= src_target_computed;
            end
        end
    end
end

// Audio state
reg  [31:0] audio_wr_ptr;
reg  [31:0] audio_rd_ptr;
reg  [7:0]  audio_burst_rem;
reg  [31:0] audio_burst_bytes;
reg  [19:0] audio_backoff;

// Cart loading registers
reg  [63:0] cart_buf;
reg   [2:0] cart_byte_cnt;
reg         cart_write_pending;
reg  [28:0] cart_write_addr;
reg  [63:0] cart_write_data;
reg         cart_size_pending;
reg  [26:0] cart_total_bytes;
reg         cart_dl_prev;
reg         cart_loading;

assign ioctl_wait = cart_write_pending & ioctl_download;

// -- FIFO write signals -----------------------------------------------
reg         fifo_wr;
reg  [63:0] fifo_wr_data;
wire        fifo_full;

// -- Audio FIFO write signals -----------------------------------------
reg         audio_fifo_wr;
reg  [63:0] audio_fifo_wr_data;
wire        audio_fifo_empty;
wire [9:0]  audio_fifo_wrusedw;
wire        audio_fifo_low = (audio_fifo_wrusedw < AUDIO_REFILL_THRESHOLD);

wire [31:0] audio_bytes_avail = (audio_wr_ptr - audio_rd_ptr) & AUDIO_RING_MASK;
wire        audio_wake        = enable_ddr && audio_fifo_low && (audio_backoff == 20'd0);

wire [31:0] audio_plan_cand_a  = (audio_bytes_avail > 32'd256) ? 32'd256 : audio_bytes_avail;
wire [31:0] audio_plan_wrap    = AUDIO_RING_BYTES - (audio_rd_ptr & AUDIO_RING_MASK);
wire [31:0] audio_plan_cand_b  = (audio_plan_cand_a > audio_plan_wrap) ? audio_plan_wrap : audio_plan_cand_a;
wire [31:0] audio_plan_bytes   = audio_plan_cand_b & 32'hFFFFFFF8;
wire [7:0]  audio_plan_qwords  = audio_plan_bytes[10:3];

// -- FIFO async clear -------------------------------------------------
reg [3:0] fifo_aclr_cnt;
wire fifo_aclr_ddr_active = (fifo_aclr_cnt != 4'd0);
wire fifo_aclr = reset | fifo_aclr_ddr_active;

// -- Main state machine -----------------------------------------------
always @(posedge ddr_clk) begin
    if (reset) begin
        state              <= ST_IDLE;
        ddr_rd             <= 1'b0;
        ddr_we             <= 1'b0;
        ddr_din            <= 64'd0;
        ddr_burstcnt       <= 8'd1;
        ddr_addr           <= 29'd0;
        ctrl_word          <= 32'd0;
        prev_frame_counter <= 30'd0;
        active_buffer      <= 1'b0;
        buf_base_addr      <= 29'd0;
        src_line           <= 11'd0;
        beat_count         <= 8'd0;
        first_frame_loaded <= 1'b0;
        frame_ready_reg    <= 1'b0;
        stale_vblank_count <= 5'd0;
        preloading         <= 1'b0;
        timeout_cnt        <= 20'd0;
        fifo_wr            <= 1'b0;
        fifo_wr_data       <= 64'd0;
        fifo_aclr_cnt      <= 4'd0;
        cart_buf           <= 64'd0;
        cart_byte_cnt      <= 3'd0;
        cart_write_pending <= 1'b0;
        cart_write_addr    <= 29'd0;
        cart_write_data    <= 64'd0;
        cart_size_pending  <= 1'b0;
        cart_total_bytes   <= 27'd0;
        cart_dl_prev       <= 1'b0;
        cart_loading       <= 1'b0;
        new_frame_pending  <= 1'b0;
        synced             <= 1'b0;
        audio_wr_ptr       <= 32'd0;
        audio_rd_ptr       <= 32'd0;
        audio_burst_rem    <= 8'd0;
        audio_burst_bytes  <= 32'd0;
        audio_backoff      <= 20'd0;
        audio_fifo_wr      <= 1'b0;
        audio_fifo_wr_data <= 64'd0;
        // Step 60
        src_width             <= DEFAULT_WIDTH;
        src_height            <= DEFAULT_HEIGHT;
        qwords_per_line       <= 10'd80;   // 320 / 4
        line_qwords_remaining <= 10'd0;
        line_qword_offset     <= 29'd0;
        line_base_addr        <= 29'd0;
        cur_burst             <= 8'd1;
        src_width_o           <= DEFAULT_WIDTH;
        src_height_o          <= DEFAULT_HEIGHT;
        src_frame_start_o     <= 1'b0;
        // Step 60 v2 fix: pacing counters
        dest_phase_v          <= 32'd0;
        src_target            <= LOOKAHEAD;  // preload first 3 source lines for V-pass
        /* TEMPORARY DIAG: probe state reset */
        probe_pending         <= 1'b0;
        probe_idx             <= 3'd0;
        src_line_done_o       <= 1'b0;
    end
    else begin
        fifo_wr           <= 1'b0;
        audio_fifo_wr     <= 1'b0;
        src_frame_start_o <= 1'b0;     // pulses
        src_line_done_o   <= 1'b0;     // pulses
        if (audio_backoff != 20'd0) audio_backoff <= audio_backoff - 20'd1;
        if (fifo_aclr_cnt != 4'd0) fifo_aclr_cnt <= fifo_aclr_cnt - 4'd1;
        if (!ddr_busy) ddr_rd <= 1'b0;
        if (!ddr_busy) ddr_we <= 1'b0;

        if (new_frame_ddr) new_frame_pending <= 1'b1;

        // Phase 5 fix (2026-06-04): drive src_target from V-pass's
        // dest_line CDC + multiplier instead of pulse-counted accumulator.
        // Phase 7 v2 (2026-06-04): mask src_target with LOOKAHEAD for
        // ~100ns after new_frame_ddr to hide gray-code CDC wrap glitch
        // (multi-bit transition 223->0 produces transient garbage
        // dest_line_bin values, briefly making src_target_computed ~240).
        if (src_target_force_cnt != 4'd0)
            src_target <= LOOKAHEAD;
        else
            src_target <= src_target_computed;

        /* TEMPORARY DIAG v4 REVERTED — pulse counters dropped to reclaim
         * pll_hdmi slack. CDC confirmed working at v4 measurement. */

        // Step 60 v2 fix: enable frame_ready as soon as LOOKAHEAD source
        // lines are preloaded. Previously frame_ready_reg only went high
        // after reading ALL src_height source lines (end of frame loop),
        // which never completed before the display frame ended for
        // downscale ratios > 1 (e.g., He-Man src_height=480 but reader
        // could only fetch ~226 lines per frame at 1:1 pacing). With the
        // new ratio-based pacing, reader CAN read all source lines per
        // frame, but V-pass must be allowed to start output as soon as
        // the first 3 lines are in the ring buffer — not wait for line
        // 479. This block latches first_frame_loaded + frame_ready_reg
        // when src_line catches up to LOOKAHEAD. Both signals are also
        // set in ST_LINE_DONE at end-of-frame (line 687-688) for
        // redundancy (no-op if already latched).
        if (!first_frame_loaded && src_line >= LOOKAHEAD) begin
            first_frame_loaded <= 1'b1;
            frame_ready_reg    <= 1'b1;
        end

        // Beat capture (runs in parallel with state machine)
        if (state == ST_WAIT_LINE && ddr_dout_ready) begin
            fifo_wr      <= 1'b1;
            fifo_wr_data <= ddr_dout;
            beat_count   <= beat_count + 8'd1;
            timeout_cnt  <= 20'd0;
        end

        // -- Cart byte collection (unchanged from pre-Step-60) ----
        cart_dl_prev <= ioctl_download;

        if (ioctl_download && !cart_dl_prev) begin
            cart_loading     <= 1'b1;
            cart_byte_cnt    <= 3'd0;
            cart_buf         <= 64'd0;
            cart_total_bytes <= 27'd0;
        end

        if (ioctl_download && ioctl_wr && !cart_write_pending) begin
            cart_total_bytes <= ioctl_addr + 27'd1;

            if (ioctl_addr < 27'h100000) begin    // 1 MB cap matches NV_CART_MAX_SIZE
                case (cart_byte_cnt)
                    3'd0: cart_buf[ 7: 0] <= ioctl_dout;
                    3'd1: cart_buf[15: 8] <= ioctl_dout;
                    3'd2: cart_buf[23:16] <= ioctl_dout;
                    3'd3: cart_buf[31:24] <= ioctl_dout;
                    3'd4: cart_buf[39:32] <= ioctl_dout;
                    3'd5: cart_buf[47:40] <= ioctl_dout;
                    3'd6: cart_buf[55:48] <= ioctl_dout;
                    3'd7: cart_buf[63:56] <= ioctl_dout;
                endcase

                if (cart_byte_cnt == 3'd7) begin
                    cart_write_pending <= 1'b1;
                    cart_write_addr    <= CART_DATA_ADDR + {5'd0, ioctl_addr[26:3]};
                    cart_write_data    <= {ioctl_dout, cart_buf[55:0]};
                    cart_byte_cnt      <= 3'd0;
                end
                else begin
                    cart_byte_cnt <= cart_byte_cnt + 3'd1;
                end
            end
        end

        if (!ioctl_download && cart_dl_prev && cart_loading) begin
            cart_loading      <= 1'b0;
            cart_size_pending <= 1'b1;
            if (cart_byte_cnt != 3'd0 && !cart_write_pending && cart_total_bytes <= 27'h100000) begin
                cart_write_pending <= 1'b1;
                cart_write_addr    <= CART_DATA_ADDR + {5'd0, cart_total_bytes[26:3]};
                cart_write_data    <= cart_buf;
                cart_byte_cnt      <= 3'd0;
            end
        end

        case (state)
            ST_IDLE: begin
                if (enable_ddr && new_frame_pending) begin
                    new_frame_pending <= 1'b0;
                    state <= ST_WRITE_JOY0;
                end
                else if (cart_write_pending)
                    state <= ST_WRITE_CART;
                else if (cart_size_pending)
                    state <= ST_WRITE_CART_SIZE;
                else if (audio_wake)
                    state <= ST_POLL_AUDIO_WR;
                else if (probe_pending)  /* TEMPORARY DIAG: do probe write */
                    state <= ST_WRITE_PROBE;
            end

            ST_WRITE_JOY0: begin
                if (!ddr_busy) begin
                    ddr_addr     <= JOY0_ADDR;
                    ddr_din      <= {32'd0, joystick_0};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_WRITE_JOY1;
                end
            end

            ST_WRITE_JOY1: begin
                if (!ddr_busy) begin
                    ddr_addr     <= JOY1_ADDR;
                    ddr_din      <= {32'd0, joystick_1};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_WRITE_JOY2;
                end
            end

            ST_WRITE_JOY2: begin
                if (!ddr_busy) begin
                    ddr_addr     <= JOY2_ADDR;
                    ddr_din      <= {32'd0, joystick_2};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_WRITE_JOY3;
                end
            end

            ST_WRITE_JOY3: begin
                if (!ddr_busy) begin
                    ddr_addr     <= JOY3_ADDR;
                    ddr_din      <= {32'd0, joystick_3};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_POLL_CTRL;
                end
            end

            ST_WRITE_CART: begin
                if (!ddr_busy) begin
                    ddr_addr           <= cart_write_addr;
                    ddr_din            <= cart_write_data;
                    ddr_burstcnt       <= 8'd1;
                    ddr_we             <= 1'b1;
                    cart_write_pending <= 1'b0;
                    cart_buf           <= 64'd0;
                    if (!cart_loading && cart_size_pending)
                        state <= ST_WRITE_CART_SIZE;
                    else
                        state <= ST_IDLE;
                end
            end

            ST_WRITE_CART_SIZE: begin
                if (!ddr_busy) begin
                    ddr_addr          <= CART_CTRL_ADDR;
                    ddr_din           <= {32'd0, 5'd0, cart_total_bytes};
                    ddr_burstcnt      <= 8'd1;
                    ddr_we            <= 1'b1;
                    cart_size_pending <= 1'b0;
                    state             <= ST_IDLE;
                end
            end

            ST_POLL_CTRL: begin
                // Reads CTRL + DIM in one qword (DIM lives in upper 32 bits
                // of the qword that holds CTRL in lower 32 bits — free
                // 2-field fetch).
                if (!ddr_busy) begin
                    ddr_addr     <= CTRL_ADDR;
                    ddr_burstcnt <= 8'd1;
                    ddr_rd       <= 1'b1;
                    timeout_cnt  <= 20'd0;
                    state        <= ST_WAIT_CTRL;
                end
            end

            ST_WAIT_CTRL: begin
                if (ddr_dout_ready) begin
                    ctrl_word   <= ddr_dout[31:0];
                    // Capture DIM = upper 32 bits of same qword.
                    //  ddr_dout[63:48] = height
                    //  ddr_dout[47:32] = width
                    // Sanitize: clamp width and height to (1 .. max).
                    if (ddr_dout[47:32] == 16'd0 || ddr_dout[47:32] > 16'd1920)
                        src_width <= DEFAULT_WIDTH;
                    else
                        src_width <= ddr_dout[42:32];      // 11 bits = up to 2047
                    if (ddr_dout[63:48] == 16'd0 || ddr_dout[63:48] > 16'd1080)
                        src_height <= DEFAULT_HEIGHT;
                    else
                        src_height <= ddr_dout[58:48];     // 11 bits
                    // qwords_per_line = ceil(width / 4) computed at frame
                    // start in ST_CHECK_CTRL once src_width is latched.
                    timeout_cnt <= 20'd0;
                    state       <= ST_CHECK_CTRL;
                end
                else if (timeout_cnt == TIMEOUT_MAX)
                    state <= ST_IDLE;
                else
                    timeout_cnt <= timeout_cnt + 20'd1;
            end

            ST_CHECK_CTRL: begin
                if (!synced) begin
                    // First read after reset — capture stale DDR3 counter
                    // without displaying. Prevents showing stale game data.
                    prev_frame_counter <= ctrl_word[31:2];
                    synced <= 1'b1;
                    state <= ST_IDLE;
                end
                else if (ctrl_word[31:2] != prev_frame_counter) begin
                    // New frame. Compute per-frame source-loop parameters.
                    prev_frame_counter <= ctrl_word[31:2];
                    active_buffer      <= ctrl_word[0];
                    stale_vblank_count <= 5'd0;
                    buf_base_addr      <= ctrl_word[0] ? BUF1_ADDR : BUF0_ADDR;
                    src_line           <= 11'd0;
                    // qwords_per_line = (src_width + 3) >> 2  (ceil)
                    qwords_per_line    <= (src_width + 11'd3) >> 2;
                    line_qword_offset  <= 29'd0;
                    preloading         <= 1'b1;
                    fifo_aclr_cnt      <= 4'd8;
                    // Snapshot source dims onto export ports so the
                    // downscale module sees a stable value for the frame.
                    src_width_o        <= src_width;
                    src_height_o       <= src_height;
                    src_frame_start_o  <= 1'b1;    // pulses for 1 ddr_clk
                    // Step 60 v2 fix: reset pacing for new frame. src_target
                    // = LOOKAHEAD preloads first 3 lines for V-pass startup.
                    dest_phase_v       <= 32'd0;
                    src_target         <= LOOKAHEAD;
                    /* TEMPORARY DIAG: arm probe write on every frame start */
                    probe_pending      <= 1'b1;
                    probe_idx          <= 3'd0;
                    state              <= ST_READ_LINE;
                end
                else if (first_frame_loaded) begin
                    // Stale frame — re-read previous buffer.
                    if (stale_vblank_count < 5'd30)
                        stale_vblank_count <= stale_vblank_count + 5'd1;
                    if (stale_vblank_count >= 5'd29)
                        frame_ready_reg <= 1'b0;
                    src_line          <= 11'd0;
                    qwords_per_line   <= (src_width + 11'd3) >> 2;
                    line_qword_offset <= 29'd0;
                    preloading        <= 1'b1;
                    fifo_aclr_cnt     <= 4'd8;
                    src_width_o       <= src_width;
                    src_height_o      <= src_height;
                    src_frame_start_o <= 1'b1;
                    state             <= ST_READ_LINE;
                end
                else
                    state <= ST_IDLE;
            end

            ST_READ_LINE: begin
                if (!ddr_busy && !fifo_aclr_ddr_active) begin
                    // line_base_addr is buf_base + (src_line × qwords_per_line).
                    // For a single-burst line, line_qword_offset starts at
                    // 0 and grows by cur_burst per multi-burst chunk.
                    // line_qwords_remaining = qwords_per_line at start of
                    // each new logical line, decremented by cur_burst per
                    // ST_LINE_DONE iteration.
                    //
                    // If this is the first burst for this src_line:
                    //   line_qwords_remaining was 0 (last line drained).
                    //   We compute line_base_addr fresh.
                    // If we're continuing a multi-burst line:
                    //   line_qwords_remaining > 0 — keep going.
                    if (line_qwords_remaining == 10'd0) begin
                        // Start of a fresh source line.
                        line_base_addr        <= buf_base_addr
                                                 + ({18'd0, src_line} * qwords_per_line);
                        line_qwords_remaining <= qwords_per_line;
                        line_qword_offset     <= 29'd0;
                    end
                    // Plan this burst: min(remaining, MAX_BURST_QW).
                    if (line_qwords_remaining > {2'd0, MAX_BURST_QW}) begin
                        cur_burst    <= MAX_BURST_QW;
                        ddr_burstcnt <= MAX_BURST_QW;
                    end
                    else begin
                        cur_burst    <= line_qwords_remaining[7:0];
                        ddr_burstcnt <= line_qwords_remaining[7:0];
                    end
                    ddr_addr    <= (line_qwords_remaining == 10'd0)
                                   ? (buf_base_addr + ({18'd0, src_line} * qwords_per_line))
                                   : (line_base_addr + line_qword_offset);
                    ddr_rd      <= 1'b1;
                    beat_count  <= 8'd0;
                    timeout_cnt <= 20'd0;
                    state       <= ST_WAIT_LINE;
                end
            end

            ST_WAIT_LINE: begin
                if (beat_count == cur_burst)
                    state <= ST_LINE_DONE;
                else if (timeout_cnt == TIMEOUT_MAX)
                    state <= ST_IDLE;
                else if (!ddr_dout_ready)
                    timeout_cnt <= timeout_cnt + 20'd1;
            end

            ST_LINE_DONE: begin
                // Advance within-line counters.
                line_qwords_remaining <= line_qwords_remaining - {2'd0, cur_burst};
                line_qword_offset     <= line_qword_offset + {21'd0, cur_burst};

                if (line_qwords_remaining == {2'd0, cur_burst}) begin
                    // This burst completed the logical source line.
                    src_line_done_o <= 1'b1;
                    src_line        <= src_line + 11'd1;

                    if (src_line == src_height - 11'd1) begin
                        // Last source line of the frame.
                        first_frame_loaded <= 1'b1;
                        frame_ready_reg    <= 1'b1;
                        preloading         <= 1'b0;
                        state              <= ST_IDLE;
                    end
                    else if (preloading && src_line < 11'd1) begin
                        // Preload first 2 source lines back-to-back, then
                        // pace by new_line_ddr. For sources MUCH wider
                        // than display (He-Man 960x480 → 320x224) this
                        // pacing is a conservative throttle — downscale
                        // module is expected to consume ~5 source lines
                        // per display line, so pacing tighter may help.
                        // Keeping the existing line-paced pattern for now;
                        // Phase 4 can re-pace from downscale module's
                        // consumption rate if needed.
                        state <= ST_READ_LINE;
                    end
                    else begin
                        preloading <= 1'b0;
                        state      <= ST_WAIT_DISPLAY;
                    end
                end
                else begin
                    // More qwords remain for this source line — continue
                    // multi-burst without yielding the DDR3 bus.
                    state <= ST_READ_LINE;
                end
            end

            ST_WAIT_DISPLAY: begin
                // Step 60 v2 fix: pace reader by src_target (= D * ratio +
                // LOOKAHEAD), not 1 source line per active display line.
                // This lets reader fetch multiple source lines per display
                // line for downscale ratios > 1 (He-Man 480->224 needs 2.14
                // src lines per dest line), and fewer for very close to 1:1
                // (ATOV 240->224 needs 1.07 src lines per dest line).
                // src_target advances every new_line_ddr && !vblank_ddr in
                // the cycle-level always block above; reader bursts reads
                // here until src_line catches up.
                if (src_line < src_height && src_line < src_target)
                    state <= ST_READ_LINE;
            end

            // -- Audio path (unchanged) -----------------------------
            ST_POLL_AUDIO_WR: begin
                if (!ddr_busy) begin
                    ddr_addr     <= AUDIO_WR_ADDR;
                    ddr_burstcnt <= 8'd1;
                    ddr_rd       <= 1'b1;
                    state        <= ST_WAIT_AUDIO_WR;
                end
            end

            ST_WAIT_AUDIO_WR: begin
                if (ddr_dout_ready) begin
                    audio_wr_ptr <= ddr_dout[31:0];
                    state        <= ST_PLAN_AUDIO;
                end
            end

            ST_PLAN_AUDIO: begin
                if (audio_bytes_avail == 32'd0) begin
                    audio_backoff <= 20'h01000;
                    state         <= ST_IDLE;
                end
                else if (!audio_fifo_low) begin
                    state <= ST_IDLE;
                end
                else if (audio_plan_bytes == 32'd0) begin
                    state <= ST_IDLE;
                end
                else begin
                    audio_burst_bytes <= audio_plan_bytes;
                    audio_burst_rem   <= audio_plan_qwords;
                    state             <= ST_READ_AUDIO_RING;
                end
            end

            ST_READ_AUDIO_RING: begin
                if (!ddr_busy) begin
                    ddr_addr     <= AUDIO_RING_ADDR + audio_rd_ptr[15:3];
                    ddr_burstcnt <= audio_burst_rem;
                    ddr_rd       <= 1'b1;
                    state        <= ST_WAIT_AUDIO_RING;
                end
            end

            ST_WAIT_AUDIO_RING: begin
                if (ddr_dout_ready) begin
                    audio_fifo_wr_data <= ddr_dout;
                    audio_fifo_wr      <= 1'b1;
                    audio_burst_rem    <= audio_burst_rem - 8'd1;
                    if (audio_burst_rem == 8'd1) begin
                        audio_rd_ptr <= (audio_rd_ptr + audio_burst_bytes) & AUDIO_RING_MASK;
                        state        <= ST_WRITE_AUDIO_RD;
                    end
                end
            end

            ST_WRITE_AUDIO_RD: begin
                if (!ddr_busy) begin
                    ddr_addr     <= AUDIO_RD_ADDR;
                    ddr_din      <= {32'd0, audio_rd_ptr};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_IDLE;
                end
            end

            /* TEMPORARY DIAG: probe state writes (v4: qw1 repacked with
             *   snap_count_raw + snap_count_active fields).
             *   qword 0: [63:32]={2'd0, prev_frame_counter}  [31:0]=magic
             *   qword 1: bits [10:0]=src_line, [21:11]=src_target,
             *            [32:22]=snap_count_raw, [43:33]=snap_count_active,
             *            [63:44]=pad (NEW v4 LAYOUT — was 21'd0+v+21'd0+v)
             *   qword 2: [63:32]={21'd0, src_height}         [31:0]={21'd0, src_width}
             *   qword 3: [59:0]=packed slot_src_line[4..0] each as 12 bits
             *   ([11:0]=slot[0], [23:12]=slot[1], [35:24]=slot[2],
             *    [47:36]=slot[3], [59:48]=slot[4])
             * REVERT AFTER MEASURED. */
            ST_WRITE_PROBE: begin
                if (!ddr_busy) begin
                    ddr_addr     <= PROBE_BASE_ADDR + {26'd0, probe_idx};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    case (probe_idx)
                        3'd0: ddr_din <= {2'd0, prev_frame_counter, PROBE_MAGIC};
                        /* qw1 (v4 REVERTED): counters dropped, back to
                         * 11-bit src_target + src_line with padding */
                        3'd1: ddr_din <= {21'd0, src_target, 21'd0, src_line};
                        3'd2: ddr_din <= {21'd0, src_height_o, 21'd0, src_width_o};
                        /* qw3: END-OF-FRAME slot_src_line snapshot */
                        3'd3: ddr_din <= {4'd0,
                                          1'b0, dbg_slot_src_line_packed_i[54:44],
                                          1'b0, dbg_slot_src_line_packed_i[43:33],
                                          1'b0, dbg_slot_src_line_packed_i[32:22],
                                          1'b0, dbg_slot_src_line_packed_i[21:11],
                                          1'b0, dbg_slot_src_line_packed_i[10: 0]};
                        /* qw4 NEW: V-pass MID-FRAME (dest=100) slot_src_line.
                         * bits [54:0] = snap_slot_src_line[0..4] packed 5×11
                         * bits [63:55] = pad
                         * dbg_vpass_snap_dest100_i[54:0] = snap_slot_src_line packed */
                        3'd4: ddr_din <= {9'd0, dbg_vpass_snap_dest100_i[54:0]};
                        /* qw5 (Plan B v1): bits [10:0] = snap_src_line_needed,
                         * bits [13:11] = snap_slot_for_tap_0,
                         * bits [24:14] = snap_dest_line_bin_r (NEW Plan B),
                         * bits [35:25] = snap_src_target_computed (NEW Plan B),
                         * bits [63:36] = pad */
                        3'd5: ddr_din <= {28'd0,
                                          snap_src_target_computed,
                                          snap_dest_line_bin_r,
                                          dbg_vpass_snap_dest100_i[57:55],
                                          dbg_vpass_snap_dest100_i[68:58]};
                    endcase
                    state <= ST_WAIT_PROBE;
                end
            end

            ST_WAIT_PROBE: begin
                if (!ddr_busy && !ddr_we) begin
                    if (probe_idx == 3'd5) begin
                        probe_pending <= 1'b0;
                        state         <= ST_IDLE;
                    end else begin
                        probe_idx <= probe_idx + 3'd1;
                        state     <= ST_WRITE_PROBE;
                    end
                end
            end

            default: state <= ST_IDLE;
        endcase
    end
end

// -- Dual-Clock FIFO --------------------------------------------------
// 64-bit wide, stores raw DDR3 beats (4 RGB565 pixels per entry).
// Depth 1024 (was 256) — variable-res sources can fill more than the
// previous fixed 2-line preload (160 qwords). For 960-wide source we need
// 240 qwords per line; depth 1024 supports ~4 lines of buffering.
wire [63:0] fifo_rd_data;
wire        fifo_empty;
wire        fifo_rd;        // Driven by external downscale via src_fifo_rd_i

dcfifo #(
    .intended_device_family ("Cyclone V"),
    .lpm_numwords           (1024),
    .lpm_showahead          ("ON"),
    .lpm_type               ("dcfifo"),
    .lpm_width              (64),
    .lpm_widthu             (10),
    .overflow_checking      ("ON"),
    .rdsync_delaypipe       (4),
    .underflow_checking     ("ON"),
    .use_eab                ("ON"),
    .wrsync_delaypipe       (4)
) line_fifo (
    .aclr     (fifo_aclr),
    .data     (fifo_wr_data),
    .rdclk    (clk_vid),
    .rdreq    (fifo_rd),
    .wrclk    (ddr_clk),
    .wrreq    (fifo_wr),
    .q        (fifo_rd_data),
    .rdempty  (fifo_empty),
    .wrfull   (fifo_full),
    .eccstatus(),
    .rdfull   (),
    .rdusedw  (),
    .wrempty  (),
    .wrusedw  ()
);

// -- Phase 4: expose line_fifo to downscale module --------------------
//
// The reader's old internal pixel-output block is removed. The FIFO read
// side is now driven externally by the openbor_video_downscale module
// in openbor_video_top.sv. fifo_rd is asserted by the downscale module
// via src_fifo_rd_i; fifo_rd_data flows out via src_fifo_rd_data_o.
//
assign fifo_rd            = src_fifo_rd_i;
assign src_fifo_rd_data_o = fifo_rd_data;
assign src_fifo_empty_o   = fifo_empty;

// -- Audio dual-clock FIFO (unchanged) --------------------------------
wire [63:0] audio_fifo_rd_data;
reg         audio_fifo_rd;

dcfifo #(
    .intended_device_family ("Cyclone V"),
    .lpm_numwords           (1024),
    .lpm_showahead          ("ON"),
    .lpm_type               ("dcfifo"),
    .lpm_width              (64),
    .lpm_widthu             (10),
    .overflow_checking      ("ON"),
    .rdsync_delaypipe       (4),
    .underflow_checking     ("ON"),
    .use_eab                ("ON"),
    .wrsync_delaypipe       (4)
) audio_fifo_inst (
    .aclr     (reset),
    .data     (audio_fifo_wr_data),
    .rdclk    (clk_audio),
    .rdreq    (audio_fifo_rd),
    .wrclk    (ddr_clk),
    .wrreq    (audio_fifo_wr),
    .q        (audio_fifo_rd_data),
    .rdempty  (audio_fifo_empty),
    .wrfull   (),
    .wrusedw  (audio_fifo_wrusedw),
    .eccstatus(),
    .rdfull   (),
    .rdusedw  (),
    .wrempty  ()
);

// -- 48 kHz sample clock (unchanged) ----------------------------------
reg [8:0] aud_div;
reg       aud_tick;
reg [1:0] reset_aud_sync;
always @(posedge clk_audio or posedge reset)
    if (reset) reset_aud_sync <= 2'b11;
    else       reset_aud_sync <= {reset_aud_sync[0], 1'b0};
wire reset_aud = reset_aud_sync[1];

always @(posedge clk_audio) begin
    if (reset_aud) begin
        aud_div  <= 9'd0;
        aud_tick <= 1'b0;
    end
    else begin
        aud_div  <= aud_div + 9'd1;
        aud_tick <= (aud_div == 9'd0);
    end
end

// -- Audio sample output (unchanged) ----------------------------------
reg half_sel;
always @(posedge clk_audio) begin
    if (reset_aud) begin
        audio_l       <= 16'd0;
        audio_r       <= 16'd0;
        audio_fifo_rd <= 1'b0;
        half_sel      <= 1'b0;
    end
    else begin
        audio_fifo_rd <= 1'b0;
        if (aud_tick) begin
            if (!audio_fifo_empty) begin
                if (half_sel == 1'b0) begin
                    audio_l  <= audio_fifo_rd_data[15:0];
                    audio_r  <= audio_fifo_rd_data[31:16];
                    half_sel <= 1'b1;
                end
                else begin
                    audio_l       <= audio_fifo_rd_data[47:32];
                    audio_r       <= audio_fifo_rd_data[63:48];
                    audio_fifo_rd <= 1'b1;
                    half_sel      <= 1'b0;
                end
            end
        end
    end
end

endmodule
