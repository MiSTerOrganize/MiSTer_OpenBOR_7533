//============================================================================
//
//  OpenBOR Video Downscale — Option Y Phase 4
//
//  Edge-aware NN/bilinear hybrid downscale: source W×H (up to 1920×1080) →
//  320×224 destination (Sega CD NTSC active area).
//
//  Replaces the failed Step 60 polyphase implementation (archived in branch
//  polyphase-archive-20260605). See docs/dev/option_y_phase1_architecture.md
//  §8 for the algorithm spec.
//
//  Algorithm
//  ---------
//  For each dest pixel, sample a 2×2 source neighborhood. Compute luma
//  contrast (max - min). High contrast (text/sprite edge) → output =
//  nearest source pixel (preserves hard edges). Low contrast (gradient) →
//  output = bilinear blend (smooth). Threshold tunable; default 24.
//
//  Lessons from polyphase (DO NOT REPEAT)
//  --------------------------------------
//    1. ONE registered frame-start fanout to both H-pass and V-pass — no
//       parallel CDC of the same logical event (Step 60 had separate CDC
//       paths for `new_frame` and `src_frame_start_sync`; 36% of frames
//       had them out of sync at the d=100 V-pass snap).
//    2. 2-slot ring with EXPLICIT producer-consumer handshake. H-pass
//       STALLS before overwriting a slot V-pass still uses; V-pass STALLS
//       dest_line advance when needed slot not ready. No more race class.
//    3. Match engine character: NN on hard edges, bilinear on smooth.
//       Polyphase blurred text and was the wrong filter for OpenBOR's
//       hard-edge pixel-art rendering.
//
//  Resource estimate (per design doc §9)
//  -------------------------------------
//    Line buffers:  2 × 320 × 16 = ~5 M10K  (vs polyphase 5 × 320 = ~12)
//    DSP elements:  6 (2-tap V × 3 channels)  (vs polyphase 12)
//    ALMs:          ~2000  (vs polyphase ~3500)
//
//  Copyright (C) 2026 MiSTer Organize — GPL-3.0
//
//============================================================================

module openbor_video_downscale (
    // Clocks + reset
    input  wire        clk_vid,
    input  wire        clk_sys,
    input  wire        ce_pix,
    input  wire        reset,

    // Display timing (clk_vid)
    input  wire        de,
    input  wire        hblank,
    input  wire        vblank,
    input  wire        new_frame,
    input  wire        new_line,

    // Source pixel stream from reader's line_fifo (clk_vid domain)
    output reg         src_fifo_rd,
    input  wire [63:0] src_fifo_rd_data,    // 4 RGB565 pixels per qword
    input  wire        src_fifo_empty,

    // Source dimensions (latched at frame start)
    input  wire [10:0] src_width,           // 1..1920 from DIM
    input  wire [10:0] src_height,          // 1..1080 from DIM
    input  wire        src_frame_start,     // single-cycle pulse @ ddr_clk side
    input  wire        frame_ready,         // CDC sys→vid handshake from reader

    // Dest pixel output (clk_vid)
    output reg   [7:0] r_out,
    output reg   [7:0] g_out,
    output reg   [7:0] b_out,

    // Edge threshold (tunable; default 24)
    input  wire  [7:0] edge_threshold
);

// ===================================================================
// Constants
// ===================================================================
localparam [10:0] DEST_WIDTH  = 11'd320;
localparam [10:0] DEST_HEIGHT = 11'd224;

// Bresenham fixed-point: 16 fractional bits.
localparam        FP_BITS = 16;
localparam [31:0] FP_ONE  = 32'h00010000;

// ===================================================================
// CDC + frame-start fanout (the CDC RACE FIX)
// ===================================================================
//
// THE LESSON FROM POLYPHASE: Step 60 had `new_frame` (timing module) and
// `src_frame_start_sync` (reader → 2-FF sync in downscale) as SEPARATE CDC
// chains. 36% of frames had them out of sync at the d=100 V-pass snap.
// Phase 12 probe DEFINITIVELY confirmed this.
//
// THIS DESIGN: source-side `src_frame_start` is the ONE authoritative
// frame-start signal. 2-FF sync into clk_vid produces `frame_start_pulse`,
// a single-cycle rising-edge pulse. BOTH H-pass and V-pass use this
// same pulse. No parallel CDC, no race possible.
// ===================================================================

reg [1:0] frame_start_sync;
always @(posedge clk_vid) begin
    if (reset) frame_start_sync <= 2'b0;
    else       frame_start_sync <= {frame_start_sync[0], src_frame_start};
end
wire frame_start_pulse = frame_start_sync[0] & ~frame_start_sync[1];

// Latch source dims at frame start (CDC: src_width / src_height are
// stable across frames once written by ARM's atomic DIM word). Async
// sampling acceptable because dims change rarely (per-PAK, not per-frame).
reg [10:0] src_w_latched, src_h_latched;
always @(posedge clk_vid) begin
    if (reset) begin
        src_w_latched <= DEST_WIDTH;
        src_h_latched <= DEST_HEIGHT;
    end
    else if (frame_start_pulse) begin
        src_w_latched <= src_width;
        src_h_latched <= src_height;
    end
end

// Bresenham step factors. Computed at frame start when dims are latched.
//
//   H-pass step (per source pixel): step_h = DEST_WIDTH / src_width
//     When phase_h crosses 1.0, emit one dest column.
//     For downscale (src > dest) step_h < 1.0; emit rarely.
//
//   V-pass step (per dest line): step_v = src_height / DEST_HEIGHT
//     phase_v accumulates step_v per dest line. integer part of phase_v
//     is the source line index for that dest row.
reg [31:0] h_step_fp;
reg [31:0] v_step_fp;
always @(posedge clk_vid) begin
    if (reset) begin
        h_step_fp <= FP_ONE;     // 1:1 default
        v_step_fp <= FP_ONE;
    end
    else if (frame_start_pulse) begin
        h_step_fp <= ({DEST_WIDTH, 16'd0}) / src_width;
        v_step_fp <= ({src_height, 16'd0}) / DEST_HEIGHT;
    end
end

// ===================================================================
// frame_ready CDC (clk_sys → clk_vid)
// ===================================================================
// Reader asserts frame_ready when at least one full frame's worth of data
// is in the line_fifo. We gate V-pass output on this.
reg [1:0] frame_ready_sync;
always @(posedge clk_vid) begin
    if (reset) frame_ready_sync <= 2'b0;
    else       frame_ready_sync <= {frame_ready_sync[0], frame_ready};
end
wire frame_ready_vid = frame_ready_sync[1];

// ===================================================================
// Edge-aware decision helpers
// ===================================================================
//
// Luma approximation: Y ≈ (R + 2G + B) / 4
//   RGB565 components extracted to 8-bit equivalents first (replicate
//   high bits to fill low bits — standard RGB565→RGB888 promotion).
//
// Edge detection: compute max-min across N luma samples. If > threshold,
// the neighborhood crosses an edge → output NN. Else → blend.
//
// Inputs are 16-bit RGB565 pixels.

function automatic [7:0] luma_of_pix;
    input [15:0] p;
    reg   [7:0] r, g, b;
    begin
        r = {p[15:11], p[15:13]};       // R5 → R8
        g = {p[10:5],  p[10:9]};        // G6 → G8
        b = {p[4:0],   p[4:2]};         // B5 → B8
        // (r + 2g + b) >> 2; 8-bit truncation OK for diff comparison.
        luma_of_pix = (r + {g[6:0], 1'b0} + b) >> 2;
    end
endfunction

function automatic edge_sharp_2;
    input [7:0] a, b;
    input [7:0] thresh;
    reg   [7:0] hi, lo;
    begin
        hi = (a > b) ? a : b;
        lo = (a > b) ? b : a;
        edge_sharp_2 = (hi - lo) > thresh;
    end
endfunction

function automatic edge_sharp_4;
    input [7:0] a, b, c, d;
    input [7:0] thresh;
    reg   [7:0] hi, lo, t1, t2;
    begin
        t1 = (a > b) ? a : b;
        t2 = (c > d) ? c : d;
        hi = (t1 > t2) ? t1 : t2;
        t1 = (a > b) ? b : a;
        t2 = (c > d) ? d : c;
        lo = (t1 > t2) ? t2 : t1;
        edge_sharp_4 = (hi - lo) > thresh;
    end
endfunction

// ===================================================================
// Slot ring (Phase 4b/4c)
// ===================================================================
// Two slots × 320 entries × 16-bit = 640 entries total ≈ 5 M10K blocks
// (vs polyphase's 5 slots × 320 = 12 M10K). Each slot holds one
// X-downscaled source line (already reduced to 320 pixels via H-pass).
// V-pass (Phase 4c) reads 2 slots per dest line for 2-tap Y blend.
//
// Producer-consumer handshake (Phase 4c adds the consumer side):
//   - slot_valid[N] = 1 means slot N has current-frame data
//   - slot_src_line[N] = which source line that slot holds
//   - At frame_start_pulse: both slots invalidated (slot_valid <= 2'b00)
//   - H-pass writes line K to slot S → slot_src_line[S]=K, slot_valid[S]=1
//   - H-pass STALLS before overwriting a slot V-pass still needs
//     (Phase 4c uses v_pass_src_line_top / _bot to gate this)

reg [15:0] line_buf [0:639];                 // 2 × 320 = 640 entries
reg [10:0] slot_src_line [0:1];              // src line each slot holds (0x7FF = invalid)
reg [1:0]  slot_valid;                       // bit N: slot N has current-frame data

function automatic [9:0] slot_base(input s);
    slot_base = s ? 10'd320 : 10'd0;
endfunction

// ===================================================================
// V-pass state declarations (Phase 4c)
// ===================================================================
// Declared here so H-pass can read v_pass_needed_top / v_pass_needed_bot
// for its backpressure check (Phase 4c). Sequential logic in V-pass
// always block below.

reg [10:0] dest_line_out;       // 0..223
reg [31:0] phase_v;
reg [10:0] hpos;
reg        line_active;

// needed_top / needed_bot are COMBINATIONAL from phase_v / dest_line_out.
// They update every clk_vid cycle. V-pass advance is GATED on slots
// being ready; H-pass backpressure is GATED on these lines being held.
wire [10:0] needed_top_raw = phase_v[26:16];
wire [10:0] needed_top = (needed_top_raw >= src_h_latched)
                        ? (src_h_latched - 11'd1)
                        : needed_top_raw;
wire [11:0] needed_bot_raw = {1'b0, needed_top} + 12'd1;
wire [10:0] needed_bot = (needed_bot_raw >= {1'b0, src_h_latched})
                        ? (src_h_latched - 11'd1)
                        : needed_bot_raw[10:0];

// Slot lookup — combinational find. Returns whether each slot holds
// the needed line. With only 2 slots, find is a 2-way comparison.
wire slot0_matches_top = slot_valid[0] && (slot_src_line[0] == needed_top);
wire slot1_matches_top = slot_valid[1] && (slot_src_line[1] == needed_top);
wire slot0_matches_bot = slot_valid[0] && (slot_src_line[0] == needed_bot);
wire slot1_matches_bot = slot_valid[1] && (slot_src_line[1] == needed_bot);

wire slot_top_ready    = slot0_matches_top | slot1_matches_top;
wire slot_top_idx      = slot1_matches_top;   // 0 if slot0 matches; 1 if slot1
wire slot_bot_ready    = slot0_matches_bot | slot1_matches_bot;
wire slot_bot_idx      = slot1_matches_bot;

wire v_pass_stall      = ~slot_top_ready | ~slot_bot_ready;

// ===================================================================
// H-pass datapath — Phase 4b (clk_vid domain)
// ===================================================================
// Reads source pixels from line_fifo, downscales X with edge-aware
// NN/bilinear hybrid, writes 320-pixel rows into line_buf.
//
// Per clk_vid cycle when h_pass_active:
//   1. If we have no current FIFO word and one is available, latch it
//      and assert src_fifo_rd (consume).
//   2. Extract source pixel from word at src_pixel_sub index.
//   3. Shift register: sh_p0 <= sh_p1; sh_p1 <= current.
//   4. Advance phase_h += h_step_fp. If phase_h crosses FP_ONE:
//      - frac = phase_h_next[15:0]
//      - if edge_sharp(sh_p0, sh_p1) → emit sh_p1 (nearest)
//      - else → emit bilinear(sh_p0, sh_p1, frac)
//      - line_buf[slot_base(write_slot) + dest_col_out] <= emit
//      - dest_col_out <= dest_col_out + 1
//   5. Advance src_col, src_pixel_sub.
//   6. End of source line (src_col == src_width-1):
//      - slot_src_line[write_slot] <= src_line_in
//      - slot_valid[write_slot] <= 1
//      - write_slot <= ~write_slot
//      - src_line_in <= src_line_in + 1
//      - reset src_col, phase_h, dest_col_out, shift register
//      - if src_line_in == src_height - 1: h_pass_active <= 0 (frame done)

reg [63:0] src_pixel_word;
reg [1:0]  src_pixel_sub;
reg        src_word_valid;
reg [10:0] src_col;
reg [10:0] src_line_in;
reg [15:0] sh_p0, sh_p1;
reg [31:0] phase_h;
reg [10:0] dest_col_out;
reg        h_pass_active;
reg        write_slot;
reg        h_eol_pending;    // Phase 4c backpressure: end-of-line pending advance

// H-pass backpressure check: if NEXT slot to be written holds a line
// V-pass is currently using (needed_top or needed_bot), stall.
wire        h_next_slot         = ~write_slot;
wire        h_next_slot_v_needed =
    slot_valid[h_next_slot] &&
    ((slot_src_line[h_next_slot] == needed_top) ||
     (slot_src_line[h_next_slot] == needed_bot));

// Current source pixel: 16-bit slice from src_pixel_word at sub*16.
wire [15:0] src_pix_cur = src_pixel_word[{src_pixel_sub, 4'b0000} +: 16];

// Bresenham emit check (combinational).
wire [32:0] phase_h_next = {1'b0, phase_h} + {1'b0, h_step_fp};
wire        emit_pix     = phase_h_next >= {1'b0, FP_ONE};
wire [31:0] phase_h_post = emit_pix ? phase_h_next[31:0] - FP_ONE : phase_h_next[31:0];

// Edge-aware decision on the 2-pixel X neighborhood.
wire [7:0]  hl_p0 = luma_of_pix(sh_p0);
wire [7:0]  hl_p1 = luma_of_pix(sh_p1);
wire        h_edge_sharp = edge_sharp_2(hl_p0, hl_p1, edge_threshold);

// Bilinear blend between sh_p0 and sh_p1 with weight = phase_h_next[15:0].
// frac=0 → sh_p0; frac=FFFF → sh_p1. RGB565 fields blended independently.
//
// Extract per-channel as 8-bit (replicate top bits to fill low bits — standard
// RGB565→RGB888 conversion). Multiply by 16-bit weight, sum, shift back.
wire [15:0] frac_h     = phase_h_next[15:0];
wire [15:0] inv_frac_h = 16'hFFFF - frac_h;
wire [7:0]  p0_r = {sh_p0[15:11], sh_p0[15:13]};
wire [7:0]  p0_g = {sh_p0[10:5],  sh_p0[10:9]};
wire [7:0]  p0_b = {sh_p0[ 4:0],  sh_p0[ 4:2]};
wire [7:0]  p1_r = {sh_p1[15:11], sh_p1[15:13]};
wire [7:0]  p1_g = {sh_p1[10:5],  sh_p1[10:9]};
wire [7:0]  p1_b = {sh_p1[ 4:0],  sh_p1[ 4:2]};

// Blend each channel: (a*inv + b*frac) >> 16. The 8x16 multiplies map to
// DSP slices (Cyclone V 18x19 multipliers — 8x16 fits trivially).
wire [23:0] blend_r24 = p0_r * inv_frac_h + p1_r * frac_h;
wire [23:0] blend_g24 = p0_g * inv_frac_h + p1_g * frac_h;
wire [23:0] blend_b24 = p0_b * inv_frac_h + p1_b * frac_h;
wire [7:0]  blend_r   = blend_r24[23:16];
wire [7:0]  blend_g   = blend_g24[23:16];
wire [7:0]  blend_b   = blend_b24[23:16];

// Final H-pass pixel: NN (sh_p1) on edge, bilinear-repacked on smooth.
wire [15:0] h_emit_nn    = sh_p1;
wire [15:0] h_emit_blend = {blend_r[7:3], blend_g[7:2], blend_b[7:3]};
wire [15:0] h_emit_pix   = h_edge_sharp ? h_emit_nn : h_emit_blend;

// ===================================================================
// H-pass sequential logic
// ===================================================================
always @(posedge clk_vid) begin
    if (reset) begin
        src_fifo_rd       <= 1'b0;
        src_pixel_word    <= 64'd0;
        src_pixel_sub     <= 2'd0;
        src_word_valid    <= 1'b0;
        src_col           <= 11'd0;
        src_line_in       <= 11'd0;
        sh_p0             <= 16'd0;
        sh_p1             <= 16'd0;
        phase_h           <= 32'd0;
        dest_col_out      <= 11'd0;
        h_pass_active     <= 1'b0;
        write_slot        <= 1'b0;
        h_eol_pending     <= 1'b0;
        slot_src_line[0]  <= 11'h7FF;
        slot_src_line[1]  <= 11'h7FF;
        slot_valid        <= 2'b00;
    end
    else begin
        src_fifo_rd <= 1'b0;

        // Start H-pass on the frame_start_pulse fanout (CDC race fix —
        // SAME pulse drives V-pass start in Phase 4c).
        if (frame_start_pulse) begin
            src_pixel_sub  <= 2'd0;
            src_word_valid <= 1'b0;
            src_col        <= 11'd0;
            src_line_in    <= 11'd0;
            sh_p0          <= 16'd0;
            sh_p1          <= 16'd0;
            phase_h        <= 32'd0;
            dest_col_out   <= 11'd0;
            h_pass_active  <= 1'b1;
            write_slot     <= 1'b0;
            h_eol_pending  <= 1'b0;
            slot_src_line[0] <= 11'h7FF;
            slot_src_line[1] <= 11'h7FF;
            slot_valid     <= 2'b00;
        end
        else if (h_pass_active) begin
            // Phase 4c backpressure: at end of line, before rotating
            // write_slot, check if the next slot still holds a line
            // V-pass needs. If yes, STALL (don't process pixels) until
            // V-pass advances past those lines. If no, do the normal
            // end-of-line advance.
            if (h_eol_pending) begin
                if (!h_next_slot_v_needed) begin
                    // Safe to advance: latch slot metadata + rotate.
                    slot_src_line[write_slot] <= src_line_in;
                    slot_valid[write_slot]     <= 1'b1;
                    write_slot    <= ~write_slot;
                    src_line_in   <= src_line_in + 11'd1;
                    src_col       <= 11'd0;
                    phase_h       <= 32'd0;
                    dest_col_out  <= 11'd0;
                    sh_p0         <= 16'd0;
                    sh_p1         <= 16'd0;
                    src_word_valid <= 1'b0;        /* force fresh FIFO read */
                    h_eol_pending <= 1'b0;
                    if (src_line_in == src_h_latched - 11'd1)
                        h_pass_active <= 1'b0;
                end
                // else: STALL — wait for V-pass to advance past needed lines
            end
            else begin
                // Normal pixel processing.
                if (!src_word_valid && !src_fifo_empty && !src_fifo_rd) begin
                    src_pixel_word <= src_fifo_rd_data;
                    src_pixel_sub  <= 2'd0;
                    src_word_valid <= 1'b1;
                    src_fifo_rd    <= 1'b1;
                end

                if (src_word_valid) begin
                    sh_p0 <= sh_p1;
                    sh_p1 <= src_pix_cur;
                    phase_h <= phase_h_post;

                    if (emit_pix) begin
                        line_buf[{1'b0, slot_base(write_slot)} +
                                 {1'b0, dest_col_out[8:0]}] <= h_emit_pix;
                        dest_col_out <= dest_col_out + 11'd1;
                    end

                    if (src_pixel_sub == 2'd3) begin
                        src_word_valid <= 1'b0;
                    end
                    else begin
                        src_pixel_sub <= src_pixel_sub + 2'd1;
                    end

                    if (src_col == src_w_latched - 11'd1) begin
                        // Reached end of source line — defer slot advance
                        // to next cycle's backpressure check.
                        h_eol_pending <= 1'b1;
                    end
                    else begin
                        src_col <= src_col + 11'd1;
                    end
                end
            end
        end
    end
end

// ===================================================================
// V-pass datapath — Phase 4c (clk_vid domain)
// ===================================================================
// For each dest scanline:
//   1. phase_v_next computes src_line_for_dest = phase_v / FP_ONE
//   2. needed_top, needed_bot derived combinationally (declared above)
//   3. find_slot looks up which physical slot holds each needed line
//   4. If either slot not ready (v_pass_stall) → don't advance dest_line
//   5. Per dest pixel (ce_pix && de):
//      - pix_top = line_buf[slot_base(slot_top_idx) + hpos]
//      - pix_bot = line_buf[slot_base(slot_bot_idx) + hpos]
//      - luma_top, luma_bot computed
//      - v_edge = edge_sharp_2(luma_top, luma_bot, edge_threshold)
//      - frac_v = phase_v[15:0] (fractional Y position between top and bot)
//      - NN choice: closer source line (frac_v < 0x8000 → top)
//      - Bilinear: per-channel blend
//      - Final RGB output: NN on edge, bilinear on smooth

// Edge-detect + emit logic (combinational, read at every ce_pix)
wire [10:0] v_hpos = hpos;
wire [15:0] pix_top = line_buf[{1'b0, slot_base(slot_top_idx)} + {1'b0, v_hpos[8:0]}];
wire [15:0] pix_bot = line_buf[{1'b0, slot_base(slot_bot_idx)} + {1'b0, v_hpos[8:0]}];

wire [7:0]  vl_top = luma_of_pix(pix_top);
wire [7:0]  vl_bot = luma_of_pix(pix_bot);
wire        v_edge_sharp = edge_sharp_2(vl_top, vl_bot, edge_threshold);

wire [15:0] frac_v       = phase_v[15:0];
wire [15:0] inv_frac_v   = 16'hFFFF - frac_v;
wire [7:0]  pt_r = {pix_top[15:11], pix_top[15:13]};
wire [7:0]  pt_g = {pix_top[10:5],  pix_top[10:9]};
wire [7:0]  pt_b = {pix_top[ 4:0],  pix_top[ 4:2]};
wire [7:0]  pb_r = {pix_bot[15:11], pix_bot[15:13]};
wire [7:0]  pb_g = {pix_bot[10:5],  pix_bot[10:9]};
wire [7:0]  pb_b = {pix_bot[ 4:0],  pix_bot[ 4:2]};

wire [23:0] v_blend_r24 = pt_r * inv_frac_v + pb_r * frac_v;
wire [23:0] v_blend_g24 = pt_g * inv_frac_v + pb_g * frac_v;
wire [23:0] v_blend_b24 = pt_b * inv_frac_v + pb_b * frac_v;
wire [7:0]  v_blend_r   = v_blend_r24[23:16];
wire [7:0]  v_blend_g   = v_blend_g24[23:16];
wire [7:0]  v_blend_b   = v_blend_b24[23:16];

// NN choice: which of top/bot is "nearer" to the current dest line in
// fractional space. frac_v < 0x8000 → top is nearer; else bot.
wire [15:0] v_nn_pix = frac_v[15] ? pix_bot : pix_top;
wire [7:0]  v_nn_r   = {v_nn_pix[15:11], v_nn_pix[15:13]};
wire [7:0]  v_nn_g   = {v_nn_pix[10:5],  v_nn_pix[10:9]};
wire [7:0]  v_nn_b   = {v_nn_pix[ 4:0],  v_nn_pix[ 4:2]};

// new_line edge detect (gated on !vblank)
reg new_line_active_d;
wire new_line_active = new_line & ~vblank;
wire new_line_active_pulse = new_line_active & ~new_line_active_d;

always @(posedge clk_vid) begin
    if (reset) begin
        dest_line_out      <= 11'd0;
        phase_v            <= 32'd0;
        hpos               <= 11'd0;
        line_active        <= 1'b0;
        new_line_active_d  <= 1'b0;
        r_out              <= 8'd0;
        g_out              <= 8'd0;
        b_out              <= 8'd0;
    end
    else begin
        new_line_active_d <= new_line_active;

        // Frame start — V-pass uses the SAME frame_start_pulse as H-pass.
        // No parallel CDC = no race possible (the polyphase fix).
        if (frame_start_pulse) begin
            dest_line_out <= 11'd0;
            phase_v       <= 32'd0;
        end
        else if (new_line_active_pulse && !v_pass_stall) begin
            // Advance phase_v + dest_line when slots are ready.
            // If stalled, dest_line holds — HDMI scanout repeats the
            // last-emitted line (brief artifact, ≤1-2 scanlines during
            // H-pass warm-up).
            if (dest_line_out != DEST_HEIGHT - 11'd1)
                dest_line_out <= dest_line_out + 11'd1;
            phase_v <= phase_v + v_step_fp;
        end

        // Per-pixel output during active display.
        if (ce_pix) begin
            if (de && frame_ready_vid && !v_pass_stall) begin
                line_active <= 1'b1;
                if (!line_active) hpos <= 11'd0;
                else              hpos <= hpos + 11'd1;

                // Edge-aware select: NN on sharp, bilinear on smooth.
                if (v_edge_sharp) begin
                    r_out <= v_nn_r;
                    g_out <= v_nn_g;
                    b_out <= v_nn_b;
                end
                else begin
                    r_out <= v_blend_r;
                    g_out <= v_blend_g;
                    b_out <= v_blend_b;
                end
            end
            else begin
                line_active <= 1'b0;
                hpos        <= 11'd0;
                r_out       <= 8'd0;
                g_out       <= 8'd0;
                b_out       <= 8'd0;
            end
        end
    end
end

endmodule
