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
// H-pass and V-pass datapaths — Phase 4b/4c
// ===================================================================
// Phase 4a scaffolding: drive output to BLACK for now so the module
// compiles end-to-end and we can verify scaffolding via Quartus syntax
// + timing. H-pass + slot ring + V-pass datapaths land in 4b/4c on top
// of this skeleton.

always @(posedge clk_vid) begin
    if (reset) begin
        r_out <= 8'd0;
        g_out <= 8'd0;
        b_out <= 8'd0;
        src_fifo_rd <= 1'b0;
    end
    else if (ce_pix) begin
        // Phase 4a: black output everywhere. Phase 4b replaces this with
        // the H-pass edge-aware pipeline. Phase 4c adds V-pass + slot ring.
        r_out <= 8'd0;
        g_out <= 8'd0;
        b_out <= 8'd0;
        src_fifo_rd <= 1'b0;
    end
end

endmodule
