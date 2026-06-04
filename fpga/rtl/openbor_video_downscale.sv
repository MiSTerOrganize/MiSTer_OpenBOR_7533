//============================================================================
//
//  OpenBOR Native Video Downscale  (Step 60 / Option Y -- Phase 4)
//
//  Consumes source pixels from the reader's line FIFO at NATIVE resolution
//  (320..1920 wide x 224..1080 tall) and emits 320x224 (Sega CD V28 NTSC)
//  RGB888 pixels to the display pipeline.
//
//  Algorithm: separable 4x4 Catmull-Rom cubic polyphase + sharp-edge bypass.
//
//   * H pass: per source line, for each dest column, a 4-tap Catmull-Rom
//     blend of 4 consecutive source pixels with phase-dependent coefs.
//     Output: 320 column-reduced 16-bit RGB565 samples per source line.
//
//   * V pass: per dest line, for each dest column, a 4-tap Catmull-Rom
//     blend of 4 consecutive column-reduced lines (held in M10K-backed
//     ring buffer) with phase-dependent coefs. Output: 320 dest pixels.
//
//   * Sharp-edge bypass: per dest pixel, compute max abs gradient across
//     the 4-tap window's R/G/B channels. If gradient > BYPASS_THRESH,
//     output the nearest source pixel instead of the polyphase blend
//     (preserves sprite-edge crispness; polyphase blurs across edges).
//
//  Coefficients (Catmull-Rom, Mitchell B=0 C=0.5, signed Q1.8):
//    For fractional phase t in [0, 1):
//      c[-1] = (-t^3 + 2t^2 - t) / 2
//      c[ 0] = ( 3t^3 - 5t^2 + 2) / 2
//      c[+1] = (-3t^3 + 4t^2 + t) / 2
//      c[+2] = ( t^3 - t^2) / 2
//
//    Sum at any phase = 1.0 (DC preserved).
//    Precomputed for 32 phases in an `initial` block.
//
//  Bresenham pacing:
//    H_STEP_FP = (src_width  << 16) / dest_width
//    V_STEP_FP = (src_height << 16) / dest_height
//
//    Per source pixel: phase_h += H_STEP_FP; when integer part bumps,
//    emit one column-reduced sample using current 4-tap shift-reg.
//    Per source line:  phase_v += V_STEP_FP; track when integer part
//    bumps to know which column-reduced source lines feed which dest line.
//
//  Buffer sizing (Cyclone V M10K = 10240 bits = 256 x 40 or 512 x 20 etc.):
//    Line buffer ring:  5 lines x 320 cols x 16 bits = 25,600 bits ~ 3 M10Ks
//    Coefficient ROM:   32 phases x 4 taps x 9 bits = 1,152 bits  ~ 1 M10K
//
//  Copyright (C) 2026 MiSTer Organize -- GPL-3.0
//
//============================================================================

module openbor_video_downscale (
    input  wire        clk_vid,         // pixel-output clock (53.693 MHz)
    input  wire        clk_sys,         // reader/DDR3 clock (used by FIFO write side)
    input  wire        ce_pix,
    input  wire        reset,

    // Display timing inputs
    input  wire        de,
    input  wire        hblank,
    input  wire        vblank,
    input  wire        new_frame,
    input  wire        new_line,

    // Source-pixel stream from reader's line FIFO (clk_vid domain)
    output reg         src_fifo_rd,
    input  wire [63:0] src_fifo_rd_data, // 4 RGB565 pixels per qword
    input  wire        src_fifo_empty,

    // Source dimensions (latched at src_frame_start)
    input  wire [10:0] src_width,
    input  wire [10:0] src_height,
    input  wire        src_frame_start,
    input  wire        frame_ready,

    // Dest output (clk_vid domain)
    output reg   [7:0] r_out,
    output reg   [7:0] g_out,
    output reg   [7:0] b_out,

    /* TEMPORARY DIAG: expose slot_src_line for reader-side probe. 5 slots
     * × 11 bits each = 55 bits packed. CDC from clk_vid to clk_sys is
     * NOT synchronized — values change slowly (~62us between updates)
     * so occasional bit-flip on read is acceptable for diagnostic.
     * REVERT AFTER MEASURED. */
    output wire [54:0] dbg_slot_src_line_packed,

    /* TEMPORARY DIAG v2: V-pass state snapshot taken at dest_line 100
     * (mid-frame). Tells us WHICH slot V-pass picked at that dest line
     * and WHAT slot_src_line contained at that moment.
     *   bits [54:0]  = snap_slot_src_line packed (5 × 11)
     *   bits [57:55] = snap_slot_for_tap_0 (the SLOT V-pass picked)
     *   bits [68:58] = snap_src_line_for_dest (the LINE V-pass NEEDED)
     * REVERT AFTER MEASURED. */
    output reg  [79:0] dbg_vpass_snap_dest100,

    /* TEMPORARY DIAG v3: src_target from reader (clk_sys domain),
     * CDC'd internally to clk_vid for snap capture. */
    input  wire [10:0] src_target_i,

    /* Phase 5 fix (Bug 2026-06-04): expose dest_line_out as gray-coded
     * value for safe multi-bit CDC into reader (clk_vid -> clk_sys).
     * Reader uses this to compute src_target = f(dest_line) directly,
     * replacing the pulse-counted accumulator that was phase-misaligned
     * with V-pass when ARM frame bumps lag FPGA new_frame. */
    output wire [10:0] dest_line_gray_o
);

// ===================================================================
// Constants
// ===================================================================
localparam [10:0] DEST_WIDTH  = 11'd320;
localparam [10:0] DEST_HEIGHT = 11'd224;

// Sharp-edge bypass threshold (max abs gradient across taps). 24 in
// 8-bit-per-channel space is "edges in any of R/G/B channels span more
// than ~9.4% of full range" - empirically captures sprite outlines
// without false-positive on smooth gradients.
localparam [7:0]  BYPASS_THRESH = 8'd24;

// Phase fixed-point: 16 fractional bits. Phase ranges over [0, 1.0).
// Integer part of accumulator tells "how far the source axis has advanced
// past the dest axis position." When integer increments by 1, we've
// emitted one dest pixel/line.
localparam        FP_BITS = 16;
localparam [31:0] FP_ONE  = 32'h00010000;

// ===================================================================
// Coefficient ROM  (Catmull-Rom, signed Q1.8)
// ===================================================================
//   32 phases x 4 taps. Indexed by phase[14:10] (top 5 bits of fractional
//   part of the per-axis Bresenham accumulator).
//
//   Sum of 4 coefficients per phase = 256 (= 1.0 in Q1.8).
//   Negative outer-tap lobes are 9-bit signed; inner taps are positive.
//
//   Values precomputed below from the Catmull-Rom basis. The unused
//   sign of c[0] and c[1] (always positive) lets us store as 9-bit signed.
//
reg signed [8:0] coef_rom [0:127];  // [phase*4 + tap], tap = 0..3 (-1, 0, +1, +2)

initial begin
    // Generated from formula:
    //   t  = phase / 32
    //   t2 = t * t
    //   t3 = t * t * t
    //   c[-1] = (-t3 + 2*t2 - t) / 2  * 256
    //   c[ 0] = ( 3*t3 - 5*t2 + 2) / 2 * 256
    //   c[+1] = (-3*t3 + 4*t2 + t) / 2 * 256
    //   c[+2] = ( t3 - t2) / 2 * 256
    //
    // 32 phases, rounded to nearest integer.
    //
    // phase=0:    [   0, 256,   0,   0]
    coef_rom[  0] =   0; coef_rom[  1] = 256; coef_rom[  2] =   0; coef_rom[  3] =   0;
    coef_rom[  4] =  -4; coef_rom[  5] = 255; coef_rom[  6] =   5; coef_rom[  7] =   0;
    coef_rom[  8] =  -7; coef_rom[  9] = 254; coef_rom[ 10] =  10; coef_rom[ 11] =  -1;
    coef_rom[ 12] = -10; coef_rom[ 13] = 252; coef_rom[ 14] =  16; coef_rom[ 15] =  -2;
    coef_rom[ 16] = -13; coef_rom[ 17] = 249; coef_rom[ 18] =  23; coef_rom[ 19] =  -3;
    coef_rom[ 20] = -15; coef_rom[ 21] = 245; coef_rom[ 22] =  30; coef_rom[ 23] =  -4;
    coef_rom[ 24] = -17; coef_rom[ 25] = 240; coef_rom[ 26] =  39; coef_rom[ 27] =  -6;
    coef_rom[ 28] = -19; coef_rom[ 29] = 234; coef_rom[ 30] =  48; coef_rom[ 31] =  -7;
    coef_rom[ 32] = -20; coef_rom[ 33] = 227; coef_rom[ 34] =  57; coef_rom[ 35] =  -8;
    coef_rom[ 36] = -22; coef_rom[ 37] = 220; coef_rom[ 38] =  67; coef_rom[ 39] =  -9;
    coef_rom[ 40] = -23; coef_rom[ 41] = 212; coef_rom[ 42] =  78; coef_rom[ 43] = -11;
    coef_rom[ 44] = -23; coef_rom[ 45] = 203; coef_rom[ 46] =  88; coef_rom[ 47] = -12;
    coef_rom[ 48] = -24; coef_rom[ 49] = 194; coef_rom[ 50] =  99; coef_rom[ 51] = -13;
    coef_rom[ 52] = -24; coef_rom[ 53] = 185; coef_rom[ 54] = 110; coef_rom[ 55] = -15;
    coef_rom[ 56] = -24; coef_rom[ 57] = 175; coef_rom[ 58] = 122; coef_rom[ 59] = -17;
    coef_rom[ 60] = -24; coef_rom[ 61] = 165; coef_rom[ 62] = 133; coef_rom[ 63] = -18;
    // phase=0.5:  [ -16, 144, 144, -16]
    coef_rom[ 64] = -24; coef_rom[ 65] = 155; coef_rom[ 66] = 144; coef_rom[ 67] = -19;
    coef_rom[ 68] = -23; coef_rom[ 69] = 145; coef_rom[ 70] = 155; coef_rom[ 71] = -21;
    coef_rom[ 72] = -23; coef_rom[ 73] = 134; coef_rom[ 74] = 166; coef_rom[ 75] = -21;
    coef_rom[ 76] = -22; coef_rom[ 77] = 124; coef_rom[ 78] = 176; coef_rom[ 79] = -22;
    coef_rom[ 80] = -22; coef_rom[ 81] = 113; coef_rom[ 82] = 186; coef_rom[ 83] = -21;
    coef_rom[ 84] = -20; coef_rom[ 85] = 102; coef_rom[ 86] = 196; coef_rom[ 87] = -22;
    coef_rom[ 88] = -19; coef_rom[ 89] =  92; coef_rom[ 90] = 205; coef_rom[ 91] = -22;
    coef_rom[ 92] = -18; coef_rom[ 93] =  82; coef_rom[ 94] = 215; coef_rom[ 95] = -23;
    coef_rom[ 96] = -16; coef_rom[ 97] =  73; coef_rom[ 98] = 222; coef_rom[ 99] = -23;
    coef_rom[100] = -15; coef_rom[101] =  63; coef_rom[102] = 230; coef_rom[103] = -22;
    coef_rom[104] = -13; coef_rom[105] =  54; coef_rom[106] = 237; coef_rom[107] = -22;
    coef_rom[108] = -12; coef_rom[109] =  45; coef_rom[110] = 243; coef_rom[111] = -20;
    coef_rom[112] = -10; coef_rom[113] =  37; coef_rom[114] = 248; coef_rom[115] = -19;
    coef_rom[116] =  -8; coef_rom[117] =  29; coef_rom[118] = 252; coef_rom[119] = -17;
    coef_rom[120] =  -6; coef_rom[121] =  22; coef_rom[122] = 255; coef_rom[123] = -15;
    coef_rom[124] =  -4; coef_rom[125] =  14; coef_rom[126] = 256; coef_rom[127] = -10;
end

// ===================================================================
// CDC + phase computation
// ===================================================================
reg [10:0] src_w_latched, src_h_latched;
reg [31:0] h_step_fp, v_step_fp;
reg        frame_active;
reg [1:0]  src_frame_start_sync;

always @(posedge clk_vid) begin
    if (reset) begin
        src_w_latched <= DEST_WIDTH;
        src_h_latched <= DEST_HEIGHT;
        h_step_fp     <= FP_ONE;        // 1:1 default
        v_step_fp     <= FP_ONE;
        frame_active  <= 1'b0;
        src_frame_start_sync <= 2'b0;
    end else begin
        src_frame_start_sync <= {src_frame_start_sync[0], src_frame_start};
        if (src_frame_start_sync[0] & ~src_frame_start_sync[1]) begin
            // Latch source dims and compute Bresenham step for this frame.
            //
            // Bug X fix 2026-06-03: H-pass step formula was INVERTED.
            //
            //   H-pass iterates SOURCE pixels (src_col increments every
            //   cycle while src_word_valid). For downscale (src > dest)
            //   we want emit RARELY (every Nth source pixel). For Bresenham:
            //       phase += step          per source pixel
            //       emit when phase >= 1.0
            //   means step = dest / source < 1.0 for downscale.
            //
            //   Previous formula h_step_fp = (src_width << 16) / DEST_WIDTH
            //   yielded step = src/dest > 1.0 for downscale -> phase >= 1.0
            //   ALWAYS -> emit EVERY cycle -> dest_col_out advances by
            //   src_width per line (e.g., 960 for He-Man) instead of 320.
            //   Writes overflowed past slot+319 into the NEXT slot in the
            //   ring buffer, corrupting it. V-pass then read partially-
            //   corrupted lines, with which-bytes-corrupted varying per
            //   frame depending on H-pass / V-pass timing race -> visible
            //   as severe flicker on He-Man + lesser flicker on 1:1 ATOV.
            //
            //   Corrected: h_step_fp = DEST_WIDTH / src_width.
            //
            // V-pass step is unchanged. V-pass iterates DEST lines (new_line
            // pulses) and looks up source line index per dest line; step
            // there is src/dest >= 1 for downscale (e.g., 2.14 for He-Man's
            // 480 source lines mapped across 224 dest lines).
            src_w_latched <= src_width;
            src_h_latched <= src_height;
            h_step_fp     <= ({DEST_WIDTH, 16'd0}) / src_width;
            v_step_fp     <= ({src_height, 16'd0}) / DEST_HEIGHT;
            frame_active  <= 1'b1;
        end
    end
end

// ===================================================================
// H pass: stream source pixels from FIFO, produce column-reduced samples
// ===================================================================
// State:
//   - src_pixel_word: 64-bit word from FIFO (4 RGB565 pixels)
//   - src_pixel_sub: which of the 4 pixels in src_pixel_word we are using
//   - shift_reg[3:0]: last 4 source pixels (Catmull-Rom needs 4 taps)
//   - src_col: current source column index (0..src_width-1)
//   - phase_h: 32-bit Bresenham accumulator
//   - dest_col_out: next dest column to write into line buffer
//
//   Algorithm (Bresenham-paced, polyphase):
//     For each incoming source pixel:
//       shift it into shift_reg
//       phase_h += h_step_fp           // accumulate fractional advance
//       when phase_h's integer part crosses 1.0:
//           dest_pixel = polyphase(shift_reg, coef_rom[phase_h[15:11]])
//           write to line_buf[dest_col_out]
//           dest_col_out += 1
//           phase_h -= FP_ONE
//
//   For downscale (src_width > DEST_WIDTH), each source pixel produces
//   <= 1 dest column. For src_width == DEST_WIDTH, every source produces
//   exactly 1 dest column. For src_width < DEST_WIDTH (upscale - rare
//   but we should handle defensively), a source pixel can produce
//   multiple dest columns - loop while phase_h >= FP_ONE.
//

reg [63:0] src_pixel_word;
reg [1:0]  src_pixel_sub;
reg        src_word_valid;

reg [15:0] sh_p0, sh_p1, sh_p2, sh_p3;   // shift reg of last 4 source pixels
reg [10:0] src_col;
reg [31:0] phase_h;
reg [10:0] dest_col_out;
reg [2:0]  src_lines_buffered;            // 0..4 of the 5-line ring
reg [10:0] src_line_in;                   // current source line index being filled

// Currently-arriving source pixel (selected from FIFO word)
wire [15:0] src_pix_cur = src_pixel_word[{src_pixel_sub, 4'b0000} +: 16];

// Coefficient indices for current phase
wire [4:0]  h_phase_idx = phase_h[15:11];  // top 5 bits of fractional part
wire [6:0]  h_coef_base = {h_phase_idx, 2'b00};
wire signed [8:0] h_c0 = coef_rom[h_coef_base + 7'd0];   // tap -1
wire signed [8:0] h_c1 = coef_rom[h_coef_base + 7'd1];   // tap  0
wire signed [8:0] h_c2 = coef_rom[h_coef_base + 7'd2];   // tap +1
wire signed [8:0] h_c3 = coef_rom[h_coef_base + 7'd3];   // tap +2

// ===================================================================
// H-pass polyphase pipeline (2 stages)  [Step 60 timing-closure rework]
// ===================================================================
//
// Original combinational depth from coef_rom + sh_p* to line_buf write
// was ~21 ns, failing setup at clk_pix 18.6 ns period (slack -2.278 ns).
//
// Restructured as a 2-stage pipeline:
//   STAGE 1 (registers hmul_*_s1):  4-tap multiply per RGB channel.
//           Each DSP slice does (signed 9-bit coef) x (signed-extended
//           9-bit pixel channel) = 18-bit signed product, registered at
//           DSP output.
//   STAGE 2 (registers hclip_*_s2): 4-input adder tree per channel,
//           Q1.8 normalize (>> 8), clip to [0, 255], pack to RGB565.
//   WRITE  (line_buf):              Address = slot_base(ws_pipe2) +
//           dc_pipe2; data = edge bypass mux between nearest (near_pipe2)
//           and polyphase blend (hclip_*_s2 packed).
//
// Metadata pipeline (parallel to data pipeline, 2-stage delay):
//   emit_pipe1/2:  was this a "phase crosses" emit event
//   dc_pipe1/2:    dest_col_out at time of emit
//   ws_pipe1/2:    write_slot at time of emit
//   edge_pipe1/2:  edge_sharp() result at time of emit
//   near_pipe1/2:  the source pixel chosen for NN bypass (sh_p2)
//
// Net timing: each stage now has ~5-8 ns combinational depth (well
// under 18.6 ns budget). Adds 2 cycles of latency per source pixel,
// imperceptible at display rate.
//

// Stage 1: multiply outputs (signed Q1.8 x unsigned 8-bit -> signed 17-bit)
reg signed [17:0] hmul_r_0_s1, hmul_r_1_s1, hmul_r_2_s1, hmul_r_3_s1;
reg signed [17:0] hmul_g_0_s1, hmul_g_1_s1, hmul_g_2_s1, hmul_g_3_s1;
reg signed [17:0] hmul_b_0_s1, hmul_b_1_s1, hmul_b_2_s1, hmul_b_3_s1;

// Stage 2: clipped + packed channel results
reg [7:0] hclip_r_s2, hclip_g_s2, hclip_b_s2;

// Metadata pipeline (2 stages deep, matches data pipeline depth)
reg        emit_pipe1, emit_pipe2;
reg [10:0] dc_pipe1, dc_pipe2;
reg [2:0]  ws_pipe1, ws_pipe2;
reg        edge_pipe1, edge_pipe2;
reg [15:0] near_pipe1, near_pipe2;

// Stage 2 adder-tree wires (combinational; result captured into hclip_*_s2).
// Sign-extend each 18-bit multiplier product to 20 bits, sum 4 products
// (worst-case range fits in 20 bits since coefs sum to 256 and pixel < 256),
// then arithmetic-shift right 8 for Q1.8 normalize.
wire signed [19:0] hsum_r_v = {{2{hmul_r_0_s1[17]}}, hmul_r_0_s1}
                            + {{2{hmul_r_1_s1[17]}}, hmul_r_1_s1}
                            + {{2{hmul_r_2_s1[17]}}, hmul_r_2_s1}
                            + {{2{hmul_r_3_s1[17]}}, hmul_r_3_s1};
wire signed [19:0] hsum_g_v = {{2{hmul_g_0_s1[17]}}, hmul_g_0_s1}
                            + {{2{hmul_g_1_s1[17]}}, hmul_g_1_s1}
                            + {{2{hmul_g_2_s1[17]}}, hmul_g_2_s1}
                            + {{2{hmul_g_3_s1[17]}}, hmul_g_3_s1};
wire signed [19:0] hsum_b_v = {{2{hmul_b_0_s1[17]}}, hmul_b_0_s1}
                            + {{2{hmul_b_1_s1[17]}}, hmul_b_1_s1}
                            + {{2{hmul_b_2_s1[17]}}, hmul_b_2_s1}
                            + {{2{hmul_b_3_s1[17]}}, hmul_b_3_s1};
wire signed [19:0] hnorm_r = hsum_r_v >>> 8;
wire signed [19:0] hnorm_g = hsum_g_v >>> 8;
wire signed [19:0] hnorm_b = hsum_b_v >>> 8;

// Decompose shift_reg pixels into R/G/B (RGB565)
function [7:0] r5to8(input [4:0] r5); r5to8 = {r5, r5[4:2]}; endfunction
function [7:0] g6to8(input [5:0] g6); g6to8 = {g6, g6[5:4]}; endfunction
function [7:0] b5to8(input [4:0] b5); b5to8 = {b5, b5[4:2]}; endfunction

// 4-tap dot product for one channel (8-bit channel x 4 coefs).
// Result is up to ~16-bit signed; clip to 8-bit unsigned after >>8 shift.
function [7:0] poly4 (
    input [7:0] s_m1, input [7:0] s_0, input [7:0] s_p1, input [7:0] s_p2,
    input signed [8:0] c_m1, input signed [8:0] c_0,
    input signed [8:0] c_p1, input signed [8:0] c_p2
);
    reg signed [17:0] acc;
    begin
        acc = (c_m1 * $signed({1'b0, s_m1}))
            + (c_0  * $signed({1'b0, s_0}))
            + (c_p1 * $signed({1'b0, s_p1}))
            + (c_p2 * $signed({1'b0, s_p2}));
        // >> 8 to undo Q1.8, clip to 8-bit unsigned.
        acc = acc >>> 8;
        if (acc < 0)
            poly4 = 8'd0;
        else if (acc > 18'sd255)
            poly4 = 8'd255;
        else
            poly4 = acc[7:0];
    end
endfunction

// Sharp-edge gradient detector (returns 1 if any channel's max abs diff
// across the 4 taps exceeds BYPASS_THRESH).
function edge_sharp (
    input [7:0] r0, input [7:0] r1, input [7:0] r2, input [7:0] r3,
    input [7:0] g0, input [7:0] g1, input [7:0] g2, input [7:0] g3,
    input [7:0] b0, input [7:0] b1, input [7:0] b2, input [7:0] b3
);
    reg [7:0] dr01, dr12, dr23;
    reg [7:0] dg01, dg12, dg23;
    reg [7:0] db01, db12, db23;
    reg [7:0] max_r, max_g, max_b, max_grad;
    begin
        // Absolute diff between adjacent taps (only 3 pairs needed)
        dr01 = (r1 > r0) ? (r1 - r0) : (r0 - r1);
        dr12 = (r2 > r1) ? (r2 - r1) : (r1 - r2);
        dr23 = (r3 > r2) ? (r3 - r2) : (r2 - r3);
        dg01 = (g1 > g0) ? (g1 - g0) : (g0 - g1);
        dg12 = (g2 > g1) ? (g2 - g1) : (g1 - g2);
        dg23 = (g3 > g2) ? (g3 - g2) : (g2 - g3);
        db01 = (b1 > b0) ? (b1 - b0) : (b0 - b1);
        db12 = (b2 > b1) ? (b2 - b1) : (b1 - b2);
        db23 = (b3 > b2) ? (b3 - b2) : (b2 - b3);
        max_r = (dr01 > dr12) ? ((dr01 > dr23) ? dr01 : dr23)
                              : ((dr12 > dr23) ? dr12 : dr23);
        max_g = (dg01 > dg12) ? ((dg01 > dg23) ? dg01 : dg23)
                              : ((dg12 > dg23) ? dg12 : dg23);
        max_b = (db01 > db12) ? ((db01 > db23) ? db01 : db23)
                              : ((db12 > db23) ? db12 : db23);
        max_grad = (max_r > max_g) ? ((max_r > max_b) ? max_r : max_b)
                                   : ((max_g > max_b) ? max_g : max_b);
        edge_sharp = (max_grad > BYPASS_THRESH);
    end
endfunction

// ===================================================================
// Line buffer ring -- 5 lines x 320 cols x 16 bits
// ===================================================================
// Single array; Quartus auto-replicates as needed to serve 4 V-pass
// read ports. Phase 5 inferred this as ~12 M10K. Bug 3 (proposed
// 4-bank explicit split) was reverted 2026-06-03 because the explicit
// banks failed M10K inference ("asynchronous read logic"), spilled to
// LAB registers, and overflowed device ALM capacity (130%). The
// original auto-replicated single-bank inference works correctly and
// was not the source of any observed bug.
reg [15:0] line_buf [0:1599];  // 5 x 320 = 1600 entries

// Slot base address LUT - slot i occupies entries [i*320 .. i*320+319].
// Lookup avoids inferring a multiplier for `slot * 320`.
function [10:0] slot_base(input [2:0] s);
    case (s)
        3'd0: slot_base = 11'd0;
        3'd1: slot_base = 11'd320;
        3'd2: slot_base = 11'd640;
        3'd3: slot_base = 11'd960;
        3'd4: slot_base = 11'd1280;
        default: slot_base = 11'd0;
    endcase
endfunction

// Reg of which source-line index each physical buffer slot currently holds.
// All-ones (11'h7FF) = empty / unassigned.
reg [10:0] slot_src_line [0:4];

/* TEMPORARY DIAG: pack slot_src_line into 55-bit wire for reader probe. */
assign dbg_slot_src_line_packed = {
    slot_src_line[4],
    slot_src_line[3],
    slot_src_line[2],
    slot_src_line[1],
    slot_src_line[0]
};

reg [2:0] write_slot;             // 0..4 - slot the H pass is filling
reg       h_pass_active;          // 1 while consuming source pixels

// ===================================================================
// V pass: per dest line, blend 4 consecutive column-reduced source lines
// ===================================================================
reg [10:0] dest_line_out;          // 0..DEST_HEIGHT-1
reg [10:0] dest_col_read;          // column being emitted to pixel output
reg [31:0] phase_v;                // 32-bit V Bresenham accumulator
reg [10:0] src_line_for_dest;      // floor(phase_v) -- "0-tap" line of V kernel

// 4 column-reduced V-tap pixels for current dest column (loaded from line_buf)
reg [15:0] v_p0, v_p1, v_p2, v_p3;

reg [4:0]  v_phase_idx;
reg [6:0]  v_coef_base;
reg signed [8:0] v_c0, v_c1, v_c2, v_c3;

// Slot indices resolved at new_line: which physical slot (0..4) holds
// the source line for each V tap. Updated by find_slot_for_src_line()
// helper which searches slot_src_line[].
reg [2:0] slot_for_tap_m1, slot_for_tap_0, slot_for_tap_p1, slot_for_tap_p2;

// ===================================================================
// V-pass polyphase pipeline (2 stages, mirror of H-pass)
// ===================================================================
// Mirror of the H-pass pipeline. v_p0..v_p3 load on ce_pix; the
// pipeline advances per ce_pix tick (2 cycles latency = 2 dest cols).
// Pipeline registers vmul_*_s1 + vclip_*_s2 break the deep
// combinational chain (mul + add + clip) that previously ran at
// clk_pix rate.
//
// Edge-bypass pipeline also matches H-pass: edge_v_pipe1/2 carry the
// edge_sharp() result through the same 2 stages so the final r/g/b
// output mux selects nearest-neighbor or polyphase blend correctly.

reg signed [17:0] vmul_r_0_s1, vmul_r_1_s1, vmul_r_2_s1, vmul_r_3_s1;
reg signed [17:0] vmul_g_0_s1, vmul_g_1_s1, vmul_g_2_s1, vmul_g_3_s1;
reg signed [17:0] vmul_b_0_s1, vmul_b_1_s1, vmul_b_2_s1, vmul_b_3_s1;

reg [7:0] vclip_r_s2, vclip_g_s2, vclip_b_s2;

reg        edge_v_pipe1, edge_v_pipe2;
reg [15:0] near_v_pipe1, near_v_pipe2;

wire signed [19:0] vsum_r_v = {{2{vmul_r_0_s1[17]}}, vmul_r_0_s1}
                            + {{2{vmul_r_1_s1[17]}}, vmul_r_1_s1}
                            + {{2{vmul_r_2_s1[17]}}, vmul_r_2_s1}
                            + {{2{vmul_r_3_s1[17]}}, vmul_r_3_s1};
wire signed [19:0] vsum_g_v = {{2{vmul_g_0_s1[17]}}, vmul_g_0_s1}
                            + {{2{vmul_g_1_s1[17]}}, vmul_g_1_s1}
                            + {{2{vmul_g_2_s1[17]}}, vmul_g_2_s1}
                            + {{2{vmul_g_3_s1[17]}}, vmul_g_3_s1};
wire signed [19:0] vsum_b_v = {{2{vmul_b_0_s1[17]}}, vmul_b_0_s1}
                            + {{2{vmul_b_1_s1[17]}}, vmul_b_1_s1}
                            + {{2{vmul_b_2_s1[17]}}, vmul_b_2_s1}
                            + {{2{vmul_b_3_s1[17]}}, vmul_b_3_s1};
wire signed [19:0] vnorm_r = vsum_r_v >>> 8;
wire signed [19:0] vnorm_g = vsum_g_v >>> 8;
wire signed [19:0] vnorm_b = vsum_b_v >>> 8;

// Look up which physical slot holds source line N (or N clamped to
// [0, src_height-1]). Returns slot index 0..4, or slot 0 if no match
// (degraded but bounded -- emits last-valid frame data).
function [2:0] find_slot(input [10:0] src_line_idx);
    reg [10:0] target;
    begin
        // Clamp to valid range to avoid out-of-bounds index
        if (src_line_idx[10])                       // signed-like underflow (top bit set)
            target = 11'd0;
        else if (src_line_idx >= src_h_latched)
            target = src_h_latched - 11'd1;
        else
            target = src_line_idx;

        if      (slot_src_line[0] == target) find_slot = 3'd0;
        else if (slot_src_line[1] == target) find_slot = 3'd1;
        else if (slot_src_line[2] == target) find_slot = 3'd2;
        else if (slot_src_line[3] == target) find_slot = 3'd3;
        else if (slot_src_line[4] == target) find_slot = 3'd4;
        else                                  find_slot = 3'd0;
    end
endfunction

// Pipeline registers between V-load and pixel-emit
// (Removed unused pix_r/pix_g/pix_b/pix_valid after V-pass pipeline
//  refactor — V output now flows from vclip_*_s2 / near_v_pipe2 directly.)

// ===================================================================
// FIFO read pacing
// ===================================================================
// Read one qword from line_fifo whenever src_pixel_sub wraps from 3 -> 0
// (i.e., we've consumed the last of the 4 pixels in the current qword).
// Stop reading when we have all source lines for the current frame.
//
// For H pass to run as fast as possible, we want at least 1 qword
// available - the H pass loop just shifts in 1 pixel per cycle when
// src_word_valid is high.

reg [1:0] frame_ready_sync;
always @(posedge clk_vid) begin
    if (reset) frame_ready_sync <= 2'b0;
    else       frame_ready_sync <= {frame_ready_sync[0], frame_ready};
end
wire frame_ready_vid = frame_ready_sync[1];

// ===================================================================
// H pass datapath  (clk_vid domain)
// ===================================================================
always @(posedge clk_vid) begin
    if (reset) begin
        src_fifo_rd        <= 1'b0;
        src_pixel_word     <= 64'd0;
        src_pixel_sub      <= 2'd0;
        src_word_valid     <= 1'b0;
        sh_p0              <= 16'd0;
        sh_p1              <= 16'd0;
        sh_p2              <= 16'd0;
        sh_p3              <= 16'd0;
        src_col            <= 11'd0;
        phase_h            <= 32'd0;
        dest_col_out       <= 11'd0;
        src_line_in        <= 11'd0;
        src_lines_buffered <= 3'd0;
        write_slot         <= 3'd0;
        h_pass_active      <= 1'b0;
        slot_src_line[0]   <= 11'h7FF;
        slot_src_line[1]   <= 11'h7FF;
        slot_src_line[2]   <= 11'h7FF;
        slot_src_line[3]   <= 11'h7FF;
        slot_src_line[4]   <= 11'h7FF;
        // H-pass pipeline regs
        hmul_r_0_s1 <= 18'd0; hmul_r_1_s1 <= 18'd0;
        hmul_r_2_s1 <= 18'd0; hmul_r_3_s1 <= 18'd0;
        hmul_g_0_s1 <= 18'd0; hmul_g_1_s1 <= 18'd0;
        hmul_g_2_s1 <= 18'd0; hmul_g_3_s1 <= 18'd0;
        hmul_b_0_s1 <= 18'd0; hmul_b_1_s1 <= 18'd0;
        hmul_b_2_s1 <= 18'd0; hmul_b_3_s1 <= 18'd0;
        hclip_r_s2 <= 8'd0;
        hclip_g_s2 <= 8'd0;
        hclip_b_s2 <= 8'd0;
        emit_pipe1 <= 1'b0; emit_pipe2 <= 1'b0;
        dc_pipe1   <= 11'd0; dc_pipe2   <= 11'd0;
        ws_pipe1   <= 3'd0;  ws_pipe2   <= 3'd0;
        edge_pipe1 <= 1'b0;  edge_pipe2 <= 1'b0;
        near_pipe1 <= 16'd0; near_pipe2 <= 16'd0;
    end
    else begin
        src_fifo_rd <= 1'b0;

        // Start H pass on frame start.
        if (src_frame_start_sync[0] & ~src_frame_start_sync[1]) begin
            src_col            <= 11'd0;
            phase_h            <= 32'd0;
            dest_col_out       <= 11'd0;
            src_line_in        <= 11'd0;
            src_lines_buffered <= 3'd0;
            write_slot         <= 3'd0;
            src_pixel_sub      <= 2'd0;
            src_word_valid     <= 1'b0;
            sh_p0 <= 16'd0; sh_p1 <= 16'd0; sh_p2 <= 16'd0; sh_p3 <= 16'd0;
            h_pass_active      <= 1'b1;
        end

        if (h_pass_active) begin
            // Fetch a new FIFO qword when our current one is drained.
            if (!src_word_valid && !src_fifo_empty && !src_fifo_rd) begin
                src_pixel_word <= src_fifo_rd_data;
                src_pixel_sub  <= 2'd0;
                src_word_valid <= 1'b1;
                src_fifo_rd    <= 1'b1;
            end

            // Process one source pixel per cycle while valid.
            if (src_word_valid) begin
                // Shift register update
                sh_p0 <= sh_p1;
                sh_p1 <= sh_p2;
                sh_p2 <= sh_p3;
                sh_p3 <= src_pix_cur;

                // Advance phase + drive pipeline Stage-0 metadata.
                // The Stage-1 mul registers, Stage-2 clip registers, and
                // metadata pipeline are updated unconditionally each
                // cycle BELOW (outside this src_word_valid branch).
                // Here we only update the rolling state (phase_h,
                // dest_col_out).
                if (phase_h + h_step_fp >= FP_ONE) begin
                    dest_col_out <= dest_col_out + 11'd1;
                    phase_h <= phase_h + h_step_fp - FP_ONE;
                end
                else begin
                    phase_h <= phase_h + h_step_fp;
                end

                // Advance to next source pixel
                if (src_pixel_sub == 2'd3) begin
                    src_word_valid <= 1'b0;
                end
                else begin
                    src_pixel_sub <= src_pixel_sub + 2'd1;
                end

                src_col <= src_col + 11'd1;

                // End of source line
                if (src_col == src_w_latched - 11'd1) begin
                    src_col      <= 11'd0;
                    phase_h      <= 32'd0;
                    dest_col_out <= 11'd0;
                    slot_src_line[write_slot] <= src_line_in;
                    write_slot   <= (write_slot == 3'd4) ? 3'd0 : write_slot + 3'd1;
                    src_line_in  <= src_line_in + 11'd1;
                    if (src_lines_buffered != 3'd5)
                        src_lines_buffered <= src_lines_buffered + 3'd1;
                    sh_p0 <= 16'd0; sh_p1 <= 16'd0; sh_p2 <= 16'd0; sh_p3 <= 16'd0;

                    // End of frame
                    if (src_line_in == src_h_latched - 11'd1)
                        h_pass_active <= 1'b0;
                end
            end

            // ---------------- H-pass 2-stage pipeline ---------------
            // Runs every clk_vid cycle while H pass is active. The
            // multiply stage (Stage 1) always computes; the line_buf
            // write only fires when emit_pipe2 indicates this stage-2
            // result corresponds to an actual emit event.
            //
            // Stage 1: capture 4-tap multiplies for R/G/B channels.
            // Inputs are current sh_p* shift-register values + h_c*
            // coef wires. Each multiply lands in a DSP slice's output
            // register (Quartus DSP packing).
            hmul_r_0_s1 <= h_c0 * $signed({1'b0, {sh_p0[15:11], sh_p0[15:13]}});
            hmul_r_1_s1 <= h_c1 * $signed({1'b0, {sh_p1[15:11], sh_p1[15:13]}});
            hmul_r_2_s1 <= h_c2 * $signed({1'b0, {sh_p2[15:11], sh_p2[15:13]}});
            hmul_r_3_s1 <= h_c3 * $signed({1'b0, {sh_p3[15:11], sh_p3[15:13]}});
            hmul_g_0_s1 <= h_c0 * $signed({1'b0, {sh_p0[10: 5], sh_p0[10: 9]}});
            hmul_g_1_s1 <= h_c1 * $signed({1'b0, {sh_p1[10: 5], sh_p1[10: 9]}});
            hmul_g_2_s1 <= h_c2 * $signed({1'b0, {sh_p2[10: 5], sh_p2[10: 9]}});
            hmul_g_3_s1 <= h_c3 * $signed({1'b0, {sh_p3[10: 5], sh_p3[10: 9]}});
            hmul_b_0_s1 <= h_c0 * $signed({1'b0, {sh_p0[ 4: 0], sh_p0[ 4: 2]}});
            hmul_b_1_s1 <= h_c1 * $signed({1'b0, {sh_p1[ 4: 0], sh_p1[ 4: 2]}});
            hmul_b_2_s1 <= h_c2 * $signed({1'b0, {sh_p2[ 4: 0], sh_p2[ 4: 2]}});
            hmul_b_3_s1 <= h_c3 * $signed({1'b0, {sh_p3[ 4: 0], sh_p3[ 4: 2]}});

            // Stage 1 metadata: pipelined alongside the data so the
            // line_buf write 2 cycles later uses the dest_col/write_slot
            // values from the emit cycle, not the (advanced) current values.
            emit_pipe1 <= src_word_valid && ((phase_h + h_step_fp) >= FP_ONE);
            dc_pipe1   <= dest_col_out;
            ws_pipe1   <= write_slot;
            near_pipe1 <= sh_p2;
            edge_pipe1 <= edge_sharp(
                {sh_p0[15:11], sh_p0[15:13]}, {sh_p1[15:11], sh_p1[15:13]},
                {sh_p2[15:11], sh_p2[15:13]}, {sh_p3[15:11], sh_p3[15:13]},
                {sh_p0[10: 5], sh_p0[10: 9]}, {sh_p1[10: 5], sh_p1[10: 9]},
                {sh_p2[10: 5], sh_p2[10: 9]}, {sh_p3[10: 5], sh_p3[10: 9]},
                {sh_p0[ 4: 0], sh_p0[ 4: 2]}, {sh_p1[ 4: 0], sh_p1[ 4: 2]},
                {sh_p2[ 4: 0], sh_p2[ 4: 2]}, {sh_p3[ 4: 0], sh_p3[ 4: 2]});

            // Stage 2: 4-input adder tree + Q1.8 normalize + clip to 8-bit
            // unsigned. Driven combinationally from hnorm_*_v wires above;
            // the clip is registered into hclip_*_s2.
            if (hnorm_r[19])               hclip_r_s2 <= 8'd0;
            else if (hnorm_r > 20'sd255)   hclip_r_s2 <= 8'd255;
            else                            hclip_r_s2 <= hnorm_r[7:0];
            if (hnorm_g[19])               hclip_g_s2 <= 8'd0;
            else if (hnorm_g > 20'sd255)   hclip_g_s2 <= 8'd255;
            else                            hclip_g_s2 <= hnorm_g[7:0];
            if (hnorm_b[19])               hclip_b_s2 <= 8'd0;
            else if (hnorm_b > 20'sd255)   hclip_b_s2 <= 8'd255;
            else                            hclip_b_s2 <= hnorm_b[7:0];

            // Stage 2 metadata: shift from Stage 1
            emit_pipe2 <= emit_pipe1;
            dc_pipe2   <= dc_pipe1;
            ws_pipe2   <= ws_pipe1;
            edge_pipe2 <= edge_pipe1;
            near_pipe2 <= near_pipe1;

            // Stage 3 (write): fire line_buf write when emit_pipe2 is set.
            // Mux between nearest-neighbor passthrough (sharp edge) and
            // polyphase blend (smooth region).
            if (emit_pipe2) begin
                line_buf[slot_base(ws_pipe2) + dc_pipe2] <=
                    edge_pipe2 ? near_pipe2
                               : {hclip_r_s2[7:3], hclip_g_s2[7:2], hclip_b_s2[7:3]};
            end
        end
    end
end

// ===================================================================
// V pass datapath  (clk_vid domain, driven by display timing)
// ===================================================================
//
// Bug 1 fix 2026-06-03: combinational POST-update phase_v computed
// here for use by the new_line procedural block below. Using a wire
// (continuous assign) instead of a local reg with blocking assignment
// inside the always block avoids SystemVerilog synthesis ambiguity
// around static-vs-automatic local variables. The new_line block
// reads phase_v_next_w so that coefs+slot lookups derive from the
// NEW phase (the one corresponding to the line we're about to
// output), not the OLD phase (which corresponded to the line we
// just finished).
wire [31:0] phase_v_next_w = phase_v + v_step_fp;

//   The V pass is invoked per display line by `new_line` pulse. It
//   walks 320 dest columns, reading 4 column-reduced samples from
//   line_buf (one per V-tap line), blending them with phase-dependent
//   coefs, and emitting RGB888 pixels via (r_out, g_out, b_out).
//
//   For Phase 4 first cut: emit pixels with 1-cycle pipeline latency.
//   V-tap loading and polyphase happen on consecutive ce_pix ticks.
//
//   The mapping from dest_line -> source_line indices is computed via
//   Bresenham at new_line: phase_v += v_step_fp; integer part of phase_v
//   is the index of the V-tap "0" line. The 4 taps are phase_v_int-1,
//   phase_v_int, phase_v_int+1, phase_v_int+2.
//
//   For each dest column, we need the column-reduced value from 4 ring
//   slots. We compute the slot index for each tap line by searching
//   slot_src_line[i] for a match. (For Phase 4 first cut, this loop is
//   unrolled; future optimization: maintain a 4-deep "current V taps"
//   register that advances by 1 slot per dest line.)
//

// Pixel position counter within a dest line
reg [10:0] hpos;
reg        line_active;

// Bug Z refactor 2026-06-03: register `new_line && !vblank` into a
// single FF (new_line_active) instead of using the bare expression
// inline in the V-pass always block. Original inline use caused the
// pll_hdmi placement to fail across SEEDs 10/11/12 (slacks -0.4/-1.3/
// -1.5 ns), likely because the AND output fanned out to MANY mux
// selects (every NBA register in the new_line branch), making routing
// pressure tight in our already-54%-ALM-dense design.
//
// Registered intermediate trades 1 clk_vid cycle of latency (~18.6 ns,
// negligible vs 62.5 us scanline period) for: (1) reduced fanout from
// a single FF Q output, (2) cleaner placement (FF can place near its
// consumers), (3) potentially less CDC analysis pressure (the AND of
// two cross-domain signals now feeds 1 FF instead of N muxes).
reg new_line_active;
always @(posedge clk_vid) begin
    new_line_active <= new_line && !vblank;
end

/* Phase 6 fix (2026-06-04): raw `new_line` from openbor_video_timing stays
 * HIGH for an entire ce_pix gap (~8-10 clk_vid cycles) per scanline because
 * timing module only updates new_line on ce_pix ticks. Without edge detect,
 * V-pass's `else if (new_line_active)` block runs 8-10x per scanline,
 * incrementing dest_line_out and advancing phase_v that many times. This
 * also produced rapid gray-code transitions across multiple bits that
 * reader's CDC couldn't reliably decode (intermediate values seen → ~4-line
 * lag in observed snap_tgt). Fix: edge-detect new_line_active to a single-
 * clk_vid pulse. */
reg new_line_active_prev;
always @(posedge clk_vid) begin
    new_line_active_prev <= new_line_active;
end
wire new_line_active_pulse = new_line_active && !new_line_active_prev;

/* Phase 5 fix: gray-code encode dest_line_out for safe multi-bit CDC.
 * Gray-coded value has only 1 bit transitioning per increment of
 * dest_line_out, so async sampling can never read an intermediate
 * spurious value. Reader 2-FF syncs + decodes back to binary. */
assign dest_line_gray_o = dest_line_out ^ (dest_line_out >> 1);

/* TEMPORARY DIAG v3: CDC src_target from reader (clk_sys) into clk_vid.
 * 2-FF synchronizer. Bit-incoherence is acceptable here — src_target
 * advances by ~+1 per active scanline (~15700 Hz), much slower than
 * clk_vid (53.7 MHz). At worst we read an off-by-1 value at snap time,
 * which is fine for the diagnostic question "is src_target near 107 at
 * dest=100, or stuck/way-off?". REVERT AFTER MEASURED. */
reg [10:0] src_target_s1, src_target_s2;
always @(posedge clk_vid) begin
    src_target_s1 <= src_target_i;
    src_target_s2 <= src_target_s1;
end

always @(posedge clk_vid) begin
    if (reset) begin
        r_out         <= 8'd0;
        g_out         <= 8'd0;
        b_out         <= 8'd0;
        hpos          <= 11'd0;
        dest_line_out <= 11'd0;
        phase_v       <= 32'd0;
        line_active   <= 1'b0;
        v_p0 <= 16'd0; v_p1 <= 16'd0; v_p2 <= 16'd0; v_p3 <= 16'd0;
        v_c0 <= 9'd0;  v_c1 <= 9'd0;  v_c2 <= 9'd0;  v_c3 <= 9'd0;
        v_phase_idx <= 5'd0;
        src_line_for_dest <= 11'd0;
        slot_for_tap_m1 <= 3'd0;
        slot_for_tap_0  <= 3'd0;
        slot_for_tap_p1 <= 3'd0;
        slot_for_tap_p2 <= 3'd0;
        // pix_r/pix_g/pix_b/pix_valid removed (dead code, see decl)
        // V-pass pipeline regs
        vmul_r_0_s1 <= 18'd0; vmul_r_1_s1 <= 18'd0;
        vmul_r_2_s1 <= 18'd0; vmul_r_3_s1 <= 18'd0;
        vmul_g_0_s1 <= 18'd0; vmul_g_1_s1 <= 18'd0;
        vmul_g_2_s1 <= 18'd0; vmul_g_3_s1 <= 18'd0;
        vmul_b_0_s1 <= 18'd0; vmul_b_1_s1 <= 18'd0;
        vmul_b_2_s1 <= 18'd0; vmul_b_3_s1 <= 18'd0;
        vclip_r_s2 <= 8'd0;
        vclip_g_s2 <= 8'd0;
        vclip_b_s2 <= 8'd0;
        edge_v_pipe1 <= 1'b0; edge_v_pipe2 <= 1'b0;
        near_v_pipe1 <= 16'd0; near_v_pipe2 <= 16'd0;
        dbg_vpass_snap_dest100 <= 80'd0;  /* TEMPORARY DIAG v2/v3 */
    end
    else begin
        if (new_frame) begin
            // Bug 1 fix 2026-06-03: also initialize coefs+slots for the
            // FIRST dest line of the frame (phase_v = 0 = integer src
            // line 0, phase 0 = coef ROM index 0). Without this, line 0
            // is rendered with whatever values are in coef regs from the
            // previous frame (or reset zeros at boot -> output is black).
            dest_line_out     <= 11'd0;
            phase_v           <= 32'd0;
            src_line_for_dest <= 11'd0;
            v_phase_idx       <= 5'd0;
            v_c0 <= coef_rom[7'd0];
            v_c1 <= coef_rom[7'd1];
            v_c2 <= coef_rom[7'd2];
            v_c3 <= coef_rom[7'd3];
            slot_for_tap_m1 <= find_slot(11'd0);
            slot_for_tap_0  <= find_slot(11'd0);
            slot_for_tap_p1 <= find_slot(11'd1);
            slot_for_tap_p2 <= find_slot(11'd2);
        end
        else if (new_line_active_pulse) begin  /* Phase 6: single-cycle edge */
            // Bug Z fix 2026-06-03: gate phase_v update on !vblank.
            //
            // Refactored to use new_line_active (registered combo of
            // new_line && !vblank). See declaration above for placement
            // rationale. Original `else if (new_line && !vblank)` failed
            // pll_hdmi timing across SEEDs 10/11/12.
            //
            // new_line pulses for EVERY scanline including VBLANK (262
            // total for Sega CD: 224 active + 38 VBLANK). Without the
            // vblank gate, phase_v advanced by 38 * step_v per frame
            // BEFORE the first active dest line. For ATOV (step=1.07):
            // ~40 phantom advances. For He-Man (step=2.14): ~81 phantom
            // advances. V-pass at the first active dest line was
            // looking up source lines that didn't exist yet in the
            // ring buffer (find_slot defaulted to slot 0 = stale
            // data), so the visible output was uniform color from
            // whatever was last in slot 0. Matches the symptom of "all
            // yellow/red bars" in test pattern photos: V-pass thought
            // it was at the END of the frame, looking for high-N
            // source lines (= bright R in test pattern).
            //
            // Reader's src_target advance is ALREADY gated by
            // !vblank_ddr (matching pacing). V-pass must match.
            //
            // Bug 1 fix 2026-06-03: derive coefs + slot_for_tap_* from
            // POST-update phase_v (= phase_v_next_w wire computed above)
            // so they reflect the line we're about to output. The
            // previous code read phase_v[*] as RHS in the same NBA
            // assignment block that scheduled phase_v <= phase_v +
            // v_step_fp; NBA semantics gave the OLD phase value to the
            // lookups, applying line N's coefs to line N+1's output
            // (1-line vertical off-by-one).
            phase_v <= phase_v_next_w;
            src_line_for_dest <= phase_v_next_w[26:16];
            v_phase_idx <= phase_v_next_w[15:11];
            v_c0 <= coef_rom[{phase_v_next_w[15:11], 2'b00} + 7'd0];
            v_c1 <= coef_rom[{phase_v_next_w[15:11], 2'b00} + 7'd1];
            v_c2 <= coef_rom[{phase_v_next_w[15:11], 2'b00} + 7'd2];
            v_c3 <= coef_rom[{phase_v_next_w[15:11], 2'b00} + 7'd3];

            // Resolve V-tap slots from POST-update phase. At
            // end-of-frame (src_line_for_dest+2 >= src_h) taps clamp to
            // last available source line.
            slot_for_tap_m1 <= find_slot(phase_v_next_w[26:16] == 11'd0
                                         ? 11'd0 : phase_v_next_w[26:16] - 11'd1);
            slot_for_tap_0  <= find_slot(phase_v_next_w[26:16]);
            slot_for_tap_p1 <= find_slot(phase_v_next_w[26:16] + 11'd1);
            slot_for_tap_p2 <= find_slot(phase_v_next_w[26:16] + 11'd2);

            if (dest_line_out != DEST_HEIGHT - 11'd1)
                dest_line_out <= dest_line_out + 11'd1;

            /* TEMPORARY DIAG v2: snapshot V-pass state at dest_line 100
             * (mid-frame). Captures:
             *   - which slot V-pass picked (slot_for_tap_0)
             *   - which line V-pass thought it needed (src_line_for_dest
             *     = phase_v_next_w[26:16])
             *   - what slot_src_line[] contained at that moment
             * Reader includes this in next probe write. */
            if (dest_line_out == 11'd99) begin  /* fires at transition to 100 */
                dbg_vpass_snap_dest100 <= {
                    src_target_s2,          /* [79:69] snap_src_target (v3) */
                    phase_v_next_w[26:16],  /* [68:58] src_line_needed */
                    find_slot(phase_v_next_w[26:16]),  /* [57:55] slot_picked */
                    slot_src_line[4],       /* [54:44] */
                    slot_src_line[3],       /* [43:33] */
                    slot_src_line[2],       /* [32:22] */
                    slot_src_line[1],       /* [21:11] */
                    slot_src_line[0]        /* [10:0]  */
                };
            end
        end

        if (ce_pix) begin
            if (de && frame_ready_vid) begin
                line_active <= 1'b1;
                if (!line_active) hpos <= 11'd0;
                else              hpos <= hpos + 11'd1;

                // V-tap pixel load:
                // For dest column `hpos`, read 4 column-reduced pixels
                // from line_buf at the slots holding src_lines:
                //   src_line_for_dest - 1, +0, + 1, + 2 (the 4-tap V kernel).
                //
                // The 4 slot indices were resolved at new_line via the
                // slot_for_tap_* registers (set in the new_line block
                // above using POST-update phase_v per Bug 1 fix). Here
                // we just read from those slots. Quartus auto-replicates
                // line_buf to serve 4 simultaneous read ports.
                v_p0 <= line_buf[slot_base(slot_for_tap_m1) + hpos];
                v_p1 <= line_buf[slot_base(slot_for_tap_0)  + hpos];
                v_p2 <= line_buf[slot_base(slot_for_tap_p1) + hpos];
                v_p3 <= line_buf[slot_base(slot_for_tap_p2) + hpos];

                // Stage 1 (latched, per ce_pix tick):
                //   4 muls per channel from CURRENT v_p* (registered)
                //   x v_c* (registered) -> DSP slice output regs
                vmul_r_0_s1 <= v_c0 * $signed({1'b0, {v_p0[15:11], v_p0[15:13]}});
                vmul_r_1_s1 <= v_c1 * $signed({1'b0, {v_p1[15:11], v_p1[15:13]}});
                vmul_r_2_s1 <= v_c2 * $signed({1'b0, {v_p2[15:11], v_p2[15:13]}});
                vmul_r_3_s1 <= v_c3 * $signed({1'b0, {v_p3[15:11], v_p3[15:13]}});
                vmul_g_0_s1 <= v_c0 * $signed({1'b0, {v_p0[10: 5], v_p0[10: 9]}});
                vmul_g_1_s1 <= v_c1 * $signed({1'b0, {v_p1[10: 5], v_p1[10: 9]}});
                vmul_g_2_s1 <= v_c2 * $signed({1'b0, {v_p2[10: 5], v_p2[10: 9]}});
                vmul_g_3_s1 <= v_c3 * $signed({1'b0, {v_p3[10: 5], v_p3[10: 9]}});
                vmul_b_0_s1 <= v_c0 * $signed({1'b0, {v_p0[ 4: 0], v_p0[ 4: 2]}});
                vmul_b_1_s1 <= v_c1 * $signed({1'b0, {v_p1[ 4: 0], v_p1[ 4: 2]}});
                vmul_b_2_s1 <= v_c2 * $signed({1'b0, {v_p2[ 4: 0], v_p2[ 4: 2]}});
                vmul_b_3_s1 <= v_c3 * $signed({1'b0, {v_p3[ 4: 0], v_p3[ 4: 2]}});

                // Stage 1 metadata: edge detect + nearest pixel (v_p1
                // = tap 0 = closest src line to the dest line we're
                // emitting) for the bypass mux.
                edge_v_pipe1 <= edge_sharp(
                    {v_p0[15:11], v_p0[15:13]}, {v_p1[15:11], v_p1[15:13]},
                    {v_p2[15:11], v_p2[15:13]}, {v_p3[15:11], v_p3[15:13]},
                    {v_p0[10: 5], v_p0[10: 9]}, {v_p1[10: 5], v_p1[10: 9]},
                    {v_p2[10: 5], v_p2[10: 9]}, {v_p3[10: 5], v_p3[10: 9]},
                    {v_p0[ 4: 0], v_p0[ 4: 2]}, {v_p1[ 4: 0], v_p1[ 4: 2]},
                    {v_p2[ 4: 0], v_p2[ 4: 2]}, {v_p3[ 4: 0], v_p3[ 4: 2]});
                near_v_pipe1 <= v_p1;

                // Stage 2: 4-input add + Q1.8 norm + clip into vclip_*_s2
                if (vnorm_r[19])             vclip_r_s2 <= 8'd0;
                else if (vnorm_r > 20'sd255) vclip_r_s2 <= 8'd255;
                else                          vclip_r_s2 <= vnorm_r[7:0];
                if (vnorm_g[19])             vclip_g_s2 <= 8'd0;
                else if (vnorm_g > 20'sd255) vclip_g_s2 <= 8'd255;
                else                          vclip_g_s2 <= vnorm_g[7:0];
                if (vnorm_b[19])             vclip_b_s2 <= 8'd0;
                else if (vnorm_b > 20'sd255) vclip_b_s2 <= 8'd255;
                else                          vclip_b_s2 <= vnorm_b[7:0];

                // Stage 2 metadata: shift from Stage 1
                edge_v_pipe2 <= edge_v_pipe1;
                near_v_pipe2 <= near_v_pipe1;

                // Final output mux: edge bypass between nearest (decoded
                // from near_v_pipe2 RGB565) and polyphase blend
                // (vclip_*_s2). Output captures 2 ce_pix ticks after
                // the line_buf read -- 2-column visual latency.
                if (edge_v_pipe2) begin
                    r_out <= {near_v_pipe2[15:11], near_v_pipe2[15:13]};
                    g_out <= {near_v_pipe2[10: 5], near_v_pipe2[10: 9]};
                    b_out <= {near_v_pipe2[ 4: 0], near_v_pipe2[ 4: 2]};
                end
                else begin
                    r_out <= vclip_r_s2;
                    g_out <= vclip_g_s2;
                    b_out <= vclip_b_s2;
                end
            end
            else begin
                r_out       <= 8'd0;
                g_out       <= 8'd0;
                b_out       <= 8'd0;
                line_active <= 1'b0;
                hpos        <= 11'd0;
            end
        end
    end
end

endmodule
