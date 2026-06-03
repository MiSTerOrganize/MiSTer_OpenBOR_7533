//============================================================================
//
//  OpenBOR Native Video Top-Level Wrapper
//
//  Instantiates the timing generator and DDR3 reader, providing a clean
//  interface for integration into OpenBOR.sv.
//
//  Runs on CLK_VIDEO (53.693 MHz) with variable CE_PIXEL for exact
//  Sega CD timing — 47.68 µs active, 15,700 Hz H rate.
//
//  Differences from pico8_video_top:
//    - 320x224 (Sega CD V28 NTSC) instead of 256x256
//    - 1:1 pixel mapping instead of 2x doubling
//
//  Step 60 / Option Y (2026-06-01): ARM writes native source-res (up to
//  1920x1080) to DDR3 with a DIM ctrl word. The reader emits source
//  pixels at native rate into line_fifo and exposes src_width/src_height
//  and per-frame/per-line pacing signals for the future downscale module
//  (Phase 4 — openbor_video_downscale.sv). Until Phase 4 lands, the
//  reader's internal pixel-output block crops sources to top-left 320x224.
//
//  Cart loading via ioctl is preserved exactly as in PICO-8.
//
//  Adapted from MiSTer_PICO-8 by MiSTer Organize
//  Copyright (C) 2026 MiSTer Organize -- GPL-3.0
//
//============================================================================

module openbor_video_top (
    input  wire        clk_sys,       // system clock for DDR3
    input  wire        clk_vid,       // video clock (53.693 MHz, CLK_VIDEO)
    input  wire        ce_pix,        // pixel enable (variable rate — exact MegaCD)
    input  wire        reset,

    // DDR3 Avalon-MM master
    input  wire        ddr_busy,
    output wire  [7:0] ddr_burstcnt,
    output wire [28:0] ddr_addr,
    input  wire [63:0] ddr_dout,
    input  wire        ddr_dout_ready,
    output wire        ddr_rd,
    output wire [63:0] ddr_din,
    output wire  [7:0] ddr_be,
    output wire        ddr_we,

    // Video output (clk_vid domain)
    output wire  [7:0] vga_r,
    output wire  [7:0] vga_g,
    output wire  [7:0] vga_b,
    output wire        vga_hs,
    output wire        vga_vs,
    output wire        vga_de,

    // Control
    input  wire        enable,        // from ARM: activate native video
    output wire        active,        // module is outputting valid video
    output wire        vsync_out,     // active-low vsync for frame sync

    // CRT position adjustment (0..6 from OSD)
    input  wire  [2:0] h_offset,
    input  wire  [2:0] v_offset,

    // Joystick (from hps_io, written to DDR3 for ARM)
    input  wire [31:0] joystick_0,
    input  wire [31:0] joystick_1,
    input  wire [31:0] joystick_2,
    input  wire [31:0] joystick_3,
    input  wire [15:0] joystick_l_analog_0,

    // Cart loading via ioctl
    input  wire        ioctl_download,
    input  wire        ioctl_wr,
    input  wire [26:0] ioctl_addr,
    input  wire  [7:0] ioctl_dout,
    output wire        ioctl_wait,

    // Audio output (clk_audio domain)
    input  wire        clk_audio,
    output wire [15:0] audio_l,
    output wire [15:0] audio_r
);

// -- Timing Generator --------------------------------------------------
wire        tim_hsync, tim_vsync;
wire        tim_hblank, tim_vblank;
wire        tim_de;
wire [9:0]  tim_hcount;
wire [8:0]  tim_vcount;
wire        tim_new_frame, tim_new_line;

// Convert OSD 3-bit (0..6) to signed adjustment
wire signed [4:0] h_adj = (h_offset == 3'd0) ?  5'sd0 :
                          (h_offset == 3'd1) ?  5'sd4 :
                          (h_offset == 3'd2) ?  5'sd8 :
                          (h_offset == 3'd3) ?  5'sd12 :
                          (h_offset == 3'd4) ? -5'sd12 :
                          (h_offset == 3'd5) ? -5'sd8 :
                                               -5'sd4;
wire signed [3:0] v_adj = (v_offset == 3'd0) ?  4'sd0 :
                          (v_offset == 3'd1) ?  4'sd1 :
                          (v_offset == 3'd2) ?  4'sd2 :
                          (v_offset == 3'd3) ?  4'sd3 :
                          (v_offset == 3'd4) ? -4'sd3 :
                          (v_offset == 3'd5) ? -4'sd2 :
                                               -4'sd1;

openbor_video_timing timing (
    .clk       (clk_vid),
    .ce_pix    (ce_pix),
    .reset     (reset),
    .h_adj     (h_adj),
    .v_adj     (v_adj),
    .hsync     (tim_hsync),
    .vsync     (tim_vsync),
    .hblank    (tim_hblank),
    .vblank    (tim_vblank),
    .de        (tim_de),
    .hcount    (tim_hcount),
    .vcount    (tim_vcount),
    .new_frame (tim_new_frame),
    .new_line  (tim_new_line)
);

// -- DDR3 Pixel Reader -------------------------------------------------
wire        reader_frame_ready;

// Step 60 / Option Y source-dim + pacing signals + FIFO read interface,
// consumed by openbor_video_downscale (instantiated below).
wire [10:0] reader_src_width;
wire [10:0] reader_src_height;
wire        reader_src_frame_start;
wire        reader_src_line_done;

// FIFO interface (reader -> downscale, all clk_vid domain)
wire        reader_src_fifo_rd;       // downscale asserts to pop
wire [63:0] reader_src_fifo_rd_data;  // 4 RGB565 source pixels per qword
wire        reader_src_fifo_empty;

openbor_video_reader reader (
    .ddr_clk        (clk_sys),
    .ddr_busy       (ddr_busy),
    .ddr_burstcnt   (ddr_burstcnt),
    .ddr_addr       (ddr_addr),
    .ddr_dout       (ddr_dout),
    .ddr_dout_ready (ddr_dout_ready),
    .ddr_rd         (ddr_rd),
    .ddr_din        (ddr_din),
    .ddr_be         (ddr_be),
    .ddr_we         (ddr_we),

    .clk_vid        (clk_vid),
    .ce_pix         (ce_pix),
    .reset          (reset),

    .de             (tim_de),
    .hblank         (tim_hblank),
    .vblank         (tim_vblank),
    .new_frame      (tim_new_frame),
    .new_line       (tim_new_line),
    .vcount         (tim_vcount),

    // Phase 4: source-pixel FIFO interface (consumed by downscale below)
    .src_fifo_rd_i        (reader_src_fifo_rd),
    .src_fifo_rd_data_o   (reader_src_fifo_rd_data),
    .src_fifo_empty_o     (reader_src_fifo_empty),

    .enable         (enable),
    .frame_ready    (reader_frame_ready),

    .joystick_0     (joystick_0),
    .joystick_1     (joystick_1),
    .joystick_2     (joystick_2),
    .joystick_3     (joystick_3),
    .joystick_l_analog_0 (joystick_l_analog_0),

    .ioctl_download (ioctl_download),
    .ioctl_wr       (ioctl_wr),
    .ioctl_addr     (ioctl_addr),
    .ioctl_dout     (ioctl_dout),
    .ioctl_wait     (ioctl_wait),

    .clk_audio      (clk_audio),
    .audio_l        (audio_l),
    .audio_r        (audio_r),

    // Step 60 / Option Y exports (consumed by openbor_video_downscale in Phase 4)
    .src_width_o          (reader_src_width),
    .src_height_o         (reader_src_height),
    .src_frame_start_o    (reader_src_frame_start),
    .src_line_done_o      (reader_src_line_done)
);

// -- Downscale (Phase 4): variable-res source -> 320x224 dest ----------
// Separable 4x4 Catmull-Rom polyphase + sharp-edge bypass. Consumes
// source pixels from the reader's line FIFO; produces RGB888 dest
// pixels timed to the display pipeline.
wire [7:0] dn_r, dn_g, dn_b;

openbor_video_downscale downscale (
    .clk_vid          (clk_vid),
    .clk_sys          (clk_sys),
    .ce_pix           (ce_pix),
    .reset            (reset),

    .de               (tim_de),
    .hblank           (tim_hblank),
    .vblank           (tim_vblank),
    .new_frame        (tim_new_frame),
    .new_line         (tim_new_line),

    .src_fifo_rd      (reader_src_fifo_rd),
    .src_fifo_rd_data (reader_src_fifo_rd_data),
    .src_fifo_empty   (reader_src_fifo_empty),

    .src_width        (reader_src_width),
    .src_height       (reader_src_height),
    .src_frame_start  (reader_src_frame_start),
    .frame_ready      (reader_frame_ready),

    .r_out            (dn_r),
    .g_out            (dn_g),
    .b_out            (dn_b)
);

// -- Output assignments ------------------------------------------------
assign vga_r     = dn_r;
assign vga_g     = dn_g;
assign vga_b     = dn_b;
assign vga_hs    = tim_hsync;
assign vga_vs    = tim_vsync;
assign vga_de    = tim_de;
assign active    = enable & reader_frame_ready;
assign vsync_out = tim_vsync;

endmodule
