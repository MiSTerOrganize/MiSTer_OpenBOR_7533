//============================================================================
//
//  OpenBOR Native Video DDR3 Reader (Option Y Phase 3+)
//
//  Reads variable-resolution RGB565 frames from DDR3 (source W×H up to
//  1920×1080) and streams them into line_fifo for the downstream
//  openbor_video_downscale module. Per-frame source dimensions come
//  from the DIM word at 0x3A000004 (atomic read with CTRL).
//
//  Pre-polyphase note: this module USED to output pixels 1:1 to vga
//  directly (assumed fixed 320×224 from a software-squished ARM frame).
//  Option Y Phase 4 routes line_fifo through openbor_video_downscale.sv
//  which does the W×H → 320×224 edge-aware NN/bilinear hybrid downscale.
//  The legacy 1:1 pixel output block is preserved but its r/g/b outputs
//  are unwired in openbor_video_top.sv (downscale's outputs drive vga).
//
//  Cart loading via ioctl is PRESERVED from the PICO-8 design — PAKs are
//  loaded via the MiSTer OSD file browser exactly the way PICO-8 cartridges
//  are. Same ioctl byte collection, same flow control via ioctl_wait, same
//  state machine integration with the video reader.
//
//  DDR3 Memory Map (physical addresses, post Option Y Phase 4):
//    0x3A000000 + 0x000     : CTRL  (32-bit, [0:1]=active_buf, [2:31]=frame_counter)
//    0x3A000000 + 0x004     : DIM   (32-bit, [10:0]=width, [21:11]=height)
//                             (CTRL+DIM read atomically as one 64-bit qword)
//    0x3A000000 + 0x008     : Joystick P1
//    0x3A000000 + 0x010     : Cart control (file_size, ARM polls)
//    0x3A000000 + 0x018     : Joystick P2
//    0x3A000000 + 0x020     : Joystick P3
//    0x3A000000 + 0x028     : Joystick P4
//    0x3A000000 + 0x030     : Audio ring write pointer (ARM writes)
//    0x3A000000 + 0x038     : Audio ring read pointer  (FPGA writes)
//    0x3A000000 + 0x040     : Buffer 0 (variable W×H up to 1920×1080 ≈ 4 MB)
//    0x3A400000             : Buffer 1 (4MB-aligned)
//    0x3A800000             : Cart data buffer (PAK file from OSD)
//    0x3A880000             : Audio ring buffer (64 KiB, 16,384 stereo S16 frames)
//
//  Bandwidth: 153,600 bytes x 60fps = 9.2 MB/s (DDR3 can do >1000)
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

    // Pixel output (LEGACY — Phase 4d wires downscale's r/g/b to vga
    // instead; this output stays until Phase 4d swap is complete).
    output reg   [7:0] r_out,
    output reg   [7:0] g_out,
    output reg   [7:0] b_out,

    // Audio output (clk_audio domain)
    input  wire        clk_audio,       // 24.576 MHz
    output reg  [15:0] audio_l,
    output reg  [15:0] audio_r,

    // Option Y Phase 3/4 source-pixel interface for downscale module.
    // Reader writes source pixels into line_fifo at clk_sys; downscale
    // consumes at clk_vid (line_fifo handles CDC).
    input  wire        src_fifo_rd_i,         // downscale's fifo_rd
    output wire [63:0] src_fifo_rd_data_o,    // line_fifo's q (showahead)
    output wire        src_fifo_empty_o,      // line_fifo's rdempty
    output wire [10:0] src_width_o,           // src_width (CDC handled at downscale)
    output wire [10:0] src_height_o,          // src_height
    output wire        src_frame_start_o,     // pulses 1 ddr_clk per frame start

    // Control
    input  wire        enable,
    output wire        frame_ready
);

// DDR3 byte enable (always all bytes)
assign ddr_be  = 8'hFF;

// -- DDR3 Address Constants -- Option Y Phase 3 (2026-06-05) ---------
// 29-bit qword addresses = physical >> 3
//
// Per docs/dev/option_y_phase1_architecture.md §5: max source 1920×1080
// at 16 bpp = ~4 MB per buffer, with each buffer at a 4MB boundary.
//
//   Physical          Qword (>>3)        Purpose
//   0x3A000000        29'h07400000       CTRL+DIM (atomic 64-bit pair)
//   0x3A000008        29'h07400001       Joystick P1
//   0x3A000010        29'h07400002       Cart control
//   0x3A000018        29'h07400003       Joystick P2
//   0x3A000020        29'h07400004       Joystick P3
//   0x3A000028        29'h07400005       Joystick P4
//   0x3A000030        29'h07400006       Audio ring wr ptr
//   0x3A000038        29'h07400007       Audio ring rd ptr
//   0x3A000040        29'h07400008       Buffer 0 base (up to 4 MB)
//   0x3A400000        29'h07480000       Buffer 1 base (4MB aligned)
//   0x3A800000        29'h07500000       Cart data
//   0x3A880000        29'h07510000       Audio ring (64 KiB)
//
// 4 MB = 0x400000 bytes = 0x80000 qwords. BUF1 = BUF0 + 0x80000.
localparam [28:0] CTRL_ADDR      = 29'h07400000;  // CTRL + DIM (single qword)
localparam [28:0] JOY0_ADDR      = 29'h07400001;  // 0x3A000008 >> 3
localparam [28:0] CART_CTRL_ADDR = 29'h07400002;  // 0x3A000010 >> 3
localparam [28:0] JOY1_ADDR      = 29'h07400003;  // 0x3A000018 >> 3
localparam [28:0] JOY2_ADDR      = 29'h07400004;  // 0x3A000020 >> 3
localparam [28:0] JOY3_ADDR      = 29'h07400005;  // 0x3A000028 >> 3
localparam [28:0] AUDIO_WR_ADDR   = 29'h07400006; // 0x3A000030 >> 3
localparam [28:0] AUDIO_RD_ADDR   = 29'h07400007; // 0x3A000038 >> 3
localparam [28:0] BUF0_ADDR      = 29'h07400008;  // 0x3A000040 >> 3
localparam [28:0] BUF1_ADDR      = 29'h07480000;  // 0x3A400000 >> 3 (4 MB aligned)
localparam [28:0] CART_DATA_ADDR = 29'h07500000;  // 0x3A800000 >> 3
localparam [28:0] AUDIO_RING_ADDR = 29'h07510000; // 0x3A880000 >> 3
localparam [31:0] AUDIO_RING_BYTES = 32'h00010000; // 64 KiB
localparam [31:0] AUDIO_RING_MASK  = 32'h0000FFFF;

// Audio refill threshold: trigger a fetch when FIFO has < this qwords used.
// FIFO is 512 entries deep; 384 leaves 128 qwords (~5.3 ms) headroom.
localparam [9:0]  AUDIO_REFILL_THRESHOLD = 10'd384;

// Option Y: variable-res constants. Source dims latched at frame start
// from the DIM word. qwords_per_line = ceil(src_width / 4). Max 256
// qwords/burst (8-bit burstcnt) covers source widths up to 1024 px in
// ONE burst. Wider sources (1920) require multi-burst — deferred to
// Phase 4 if any HD PAK actually needs it.
localparam [10:0] MAX_SRC_WIDTH  = 11'd1920;
localparam [10:0] MAX_SRC_HEIGHT = 11'd1080;

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

// Latch new_frame so it can't be missed during cart writes
reg new_frame_pending;
reg synced;  // Set after first ctrl read -- prevents displaying stale DDR3 data

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
// Audio-path states -- interleaved into idle windows of the video flow.
localparam [4:0] ST_POLL_AUDIO_WR   = 5'd14;
localparam [4:0] ST_WAIT_AUDIO_WR   = 5'd15;
localparam [4:0] ST_PLAN_AUDIO      = 5'd16;
localparam [4:0] ST_READ_AUDIO_RING = 5'd17;
localparam [4:0] ST_WAIT_AUDIO_RING = 5'd18;
localparam [4:0] ST_WRITE_AUDIO_RD  = 5'd19;
// Phase 5 task #19: multi-burst source-line reads for width > 1024.
// Each source line splits into ceil(qwords_per_line / 128) bursts.
// ST_LINE_BEGIN sets up addr+remaining; ST_BURST_DONE loops back to
// ST_READ_LINE if more bursts needed, else advances to ST_LINE_DONE.
localparam [4:0] ST_LINE_BEGIN      = 5'd20;
localparam [4:0] ST_BURST_DONE      = 5'd21;

reg  [4:0]  state;
reg  [31:0] ctrl_word;
reg  [31:0] dim_word;          // Option Y: source W/H latched atomically with CTRL
reg  [29:0] prev_frame_counter;
reg         active_buffer;
reg  [28:0] buf_base_addr;

// Option Y: variable-res state. src_line is the SOURCE line being read
// (0..src_height-1, can be up to 1079). src_width and src_height come from
// the DIM word; qwords_per_line = ceil(src_width / 4).
reg  [10:0] src_line;          // 0..src_height-1 (Option Y was display_line/9b)
reg  [10:0] src_width;          // 1..1920
reg  [10:0] src_height;         // 1..1080
reg  [9:0]  qwords_per_line;    // ceil(src_width / 4)  (1..480 for max 1920)
reg  [28:0] line_base_addr;     // buf_base_addr + (src_line * qwords_per_line)
reg  [8:0]  beat_count;
// Phase 5 task #19: multi-burst per source line.
reg  [9:0]  qwords_remaining;   // qwords left in current source line
localparam [9:0] MAX_BURST_QW = 10'd128;  // Max qwords per Avalon-MM burst
reg         first_frame_loaded;
reg  [4:0]  stale_vblank_count;
reg         preloading;
reg  [19:0] timeout_cnt;

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
wire [7:0]  fifo_wrusedw;       /* Option Y Phase 4 hotfix: 256-deep FIFO → 8-bit used count */

// -- Audio FIFO write signals -----------------------------------------
reg         audio_fifo_wr;
reg  [63:0] audio_fifo_wr_data;
wire        audio_fifo_empty;
wire [9:0]  audio_fifo_wrusedw;
wire        audio_fifo_low = (audio_fifo_wrusedw < AUDIO_REFILL_THRESHOLD);

// Audio fetch eligibility (combinational)
wire [31:0] audio_bytes_avail = (audio_wr_ptr - audio_rd_ptr) & AUDIO_RING_MASK;
wire        audio_wake        = enable_ddr && audio_fifo_low && (audio_backoff == 20'd0);

// Burst planning (combinational, used in ST_PLAN_AUDIO).
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
        dim_word           <= 32'd0;
        prev_frame_counter <= 30'd0;
        active_buffer      <= 1'b0;
        buf_base_addr      <= 29'd0;
        src_line           <= 11'd0;
        src_width          <= 11'd320;   /* default until DIM read */
        src_height         <= 11'd224;
        qwords_per_line    <= 10'd80;    /* ceil(320/4) */
        line_base_addr     <= 29'd0;
        beat_count         <= 9'd0;
        qwords_remaining   <= 10'd0;     /* Phase 5 task #19 */
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
        src_frame_start_r  <= 1'b0;
    end
    else begin
        fifo_wr           <= 1'b0;
        audio_fifo_wr     <= 1'b0;
        src_frame_start_r <= 1'b0;   /* default: clear pulse each cycle */
        if (audio_backoff != 20'd0) audio_backoff <= audio_backoff - 20'd1;
        if (fifo_aclr_cnt != 4'd0) fifo_aclr_cnt <= fifo_aclr_cnt - 4'd1;
        if (!ddr_busy) ddr_rd <= 1'b0;
        if (!ddr_busy) ddr_we <= 1'b0;

        // Latch new_frame pulse so cart writes can't cause it to be missed
        if (new_frame_ddr) new_frame_pending <= 1'b1;

        // Beat capture (runs in parallel with state machine)
        if (state == ST_WAIT_LINE && ddr_dout_ready) begin
            fifo_wr      <= 1'b1;
            fifo_wr_data <= ddr_dout;
            beat_count   <= beat_count + 9'd1;
            timeout_cnt  <= 20'd0;
        end

        // -- Cart byte collection (runs in parallel) --------------
        cart_dl_prev <= ioctl_download;

        // Download start
        if (ioctl_download && !cart_dl_prev) begin
            cart_loading     <= 1'b1;
            cart_byte_cnt    <= 3'd0;
            cart_buf         <= 64'd0;
            cart_total_bytes <= 27'd0;
        end

        // Collect bytes — cap DDR3 writes at 256KB to prevent overflow
        // (SC0 mount — ARM reads PAK path from .s0, loads from SD directly)
        if (ioctl_download && ioctl_wr && !cart_write_pending) begin
            cart_total_bytes <= ioctl_addr + 27'd1;

            if (ioctl_addr < 27'h40000) begin
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
                    cart_write_addr    <= CART_DATA_ADDR + {2'd0, ioctl_addr[26:3]};
                    cart_write_data    <= {ioctl_dout, cart_buf[55:0]};
                    cart_byte_cnt      <= 3'd0;
                end
                else begin
                    cart_byte_cnt <= cart_byte_cnt + 3'd1;
                end
            end
        end

        // Download end -- flush partial + write size
        if (!ioctl_download && cart_dl_prev && cart_loading) begin
            cart_loading      <= 1'b0;
            cart_size_pending <= 1'b1;
            if (cart_byte_cnt != 3'd0 && !cart_write_pending && cart_total_bytes <= 27'h40000) begin
                cart_write_pending <= 1'b1;
                cart_write_addr    <= CART_DATA_ADDR + {2'd0, cart_total_bytes[26:3]};
                cart_write_data    <= cart_buf;
                cart_byte_cnt      <= 3'd0;
            end
        end

        case (state)
            ST_IDLE: begin
                // Frame reads always get priority -- video must never be starved.
                // Cart writes happen between frame reads.
                // Audio fetches happen last, only when nothing else is pending.
                // new_frame_pending is latched so it can't be missed.
                if (enable_ddr && new_frame_pending) begin
                    new_frame_pending <= 1'b0;  // consumed
                    state <= ST_WRITE_JOY0;
                end
                else if (cart_write_pending)
                    state <= ST_WRITE_CART;
                else if (cart_size_pending)
                    state <= ST_WRITE_CART_SIZE;
                else if (audio_wake)
                    state <= ST_POLL_AUDIO_WR;
            end

            ST_WRITE_JOY0: begin
                // Write joystick_0 (P1) to DDR3 so ARM can read it
                if (!ddr_busy) begin
                    ddr_addr     <= JOY0_ADDR;
                    ddr_din      <= {32'd0, joystick_0};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_WRITE_JOY1;
                end
            end

            ST_WRITE_JOY1: begin
                // Write joystick_1 (P2) to DDR3
                if (!ddr_busy) begin
                    ddr_addr     <= JOY1_ADDR;
                    ddr_din      <= {32'd0, joystick_1};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_WRITE_JOY2;
                end
            end

            ST_WRITE_JOY2: begin
                // Write joystick_2 (P3) to DDR3
                if (!ddr_busy) begin
                    ddr_addr     <= JOY2_ADDR;
                    ddr_din      <= {32'd0, joystick_2};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_WRITE_JOY3;
                end
            end

            ST_WRITE_JOY3: begin
                // Write joystick_3 (P4) to DDR3, then poll control
                if (!ddr_busy) begin
                    ddr_addr     <= JOY3_ADDR;
                    ddr_din      <= {32'd0, joystick_3};
                    ddr_burstcnt <= 8'd1;
                    ddr_we       <= 1'b1;
                    state        <= ST_POLL_CTRL;
                end
            end

            ST_WRITE_CART: begin
                // Write 8 bytes of cart data to DDR3
                if (!ddr_busy) begin
                    ddr_addr           <= cart_write_addr;
                    ddr_din            <= cart_write_data;
                    ddr_burstcnt       <= 8'd1;
                    ddr_we             <= 1'b1;
                    cart_write_pending <= 1'b0;
                    cart_buf           <= 64'd0;
                    // If download ended and this was the flush, write size next
                    if (!cart_loading && cart_size_pending)
                        state <= ST_WRITE_CART_SIZE;
                    else
                        state <= ST_IDLE;
                end
            end

            ST_WRITE_CART_SIZE: begin
                // Write file size to cart control address
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
                    /* Option Y Phase 3 (2026-06-05): atomic CTRL+DIM read.
                     * Single 64-bit DDR3 fetch returns CTRL in low 32 bits
                     * and DIM in high 32 bits — guaranteed coherent because
                     * ARM wrote them atomically as a 64-bit qword (see
                     * docs/dev/option_y_phase1_architecture.md §5-6). */
                    ctrl_word   <= ddr_dout[31:0];
                    dim_word    <= ddr_dout[63:32];
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
                    // First read after reset -- capture stale DDR3 counter
                    // without displaying anything. Prevents showing old game
                    // data that persists in DDR3 across reboots.
                    prev_frame_counter <= ctrl_word[31:2];
                    synced <= 1'b1;
                    state <= ST_IDLE;
                end
                else if (ctrl_word[31:2] != prev_frame_counter) begin
                    // New frame available -- latch CTRL + DIM together.
                    prev_frame_counter <= ctrl_word[31:2];
                    active_buffer      <= ctrl_word[0];
                    stale_vblank_count <= 5'd0;
                    buf_base_addr      <= ctrl_word[0] ? BUF1_ADDR : BUF0_ADDR;
                    /* Option Y Phase 3: latch source dims from DIM word.
                     * DIM[10:0] = width (1..1920), DIM[21:11] = height (1..1080).
                     * qwords_per_line = ceil(width / 4) — width is multiple of 4
                     * for every real PAK (16-byte alignment from SDL surface
                     * malloc), so just width[10:2]. For non-mod-4 widths the
                     * +3 rounds up. */
                    src_width          <= dim_word[10:0];
                    src_height         <= dim_word[21:11];
                    qwords_per_line    <= (dim_word[10:0] + 11'd3) >> 2;
                    src_line           <= 11'd0;
                    preloading         <= 1'b1;
                    fifo_aclr_cnt      <= 4'd8;
                    src_frame_start_r  <= 1'b1;  /* Option Y Phase 4: pulse to downscale */
                    state              <= ST_LINE_BEGIN;   /* Phase 5 task #19 */
                end
                else if (first_frame_loaded) begin
                    // Stale frame -- re-read previous buffer
                    if (stale_vblank_count < 5'd30)
                        stale_vblank_count <= stale_vblank_count + 5'd1;
                    if (stale_vblank_count >= 5'd29)
                        frame_ready_reg <= 1'b0;
                    src_line      <= 11'd0;
                    preloading    <= 1'b1;
                    fifo_aclr_cnt <= 4'd8;
                    state         <= ST_LINE_BEGIN;        /* Phase 5 task #19 */
                end
                else
                    state <= ST_IDLE;
            end

            ST_LINE_BEGIN: begin
                /* Phase 5 task #19: set up addr + remaining for a new
                 * source line. ST_READ_LINE then issues bursts of up to
                 * MAX_BURST_QW until qwords_remaining hits 0.
                 *
                 * For 320 wide (80 qw): 1 burst. For 960 wide (240 qw):
                 * 2 bursts (128+112). For 1600 wide Lust Rush (401 qw):
                 * 4 bursts (128+128+128+17). */
                ddr_addr         <= buf_base_addr +
                                    ({19'd0, src_line} * {19'd0, qwords_per_line});
                qwords_remaining <= qwords_per_line;
                state            <= ST_READ_LINE;
            end

            ST_READ_LINE: begin
                /* Issue one burst (size = min(remaining, MAX_BURST_QW)).
                 * Gated on FIFO having room for one max burst (128 qw).
                 * If qwords_remaining > 128, burst at full MAX; else
                 * burst exactly qwords_remaining for the tail. */
                if (!ddr_busy && !fifo_aclr_ddr_active &&
                    (({2'd0, fifo_wrusedw} + {2'd0, MAX_BURST_QW[7:0]}) <= 10'd256)) begin
                    ddr_burstcnt <= (qwords_remaining > MAX_BURST_QW) ?
                                        MAX_BURST_QW[7:0] : qwords_remaining[7:0];
                    ddr_rd       <= 1'b1;
                    beat_count   <= 9'd0;
                    timeout_cnt  <= 20'd0;
                    state        <= ST_WAIT_LINE;
                end
            end

            ST_WAIT_LINE: begin
                /* Phase 5 task #19: compare against THIS BURST's count,
                 * not whole-line qwords_per_line. ddr_burstcnt was set
                 * to min(remaining, MAX_BURST_QW) by ST_READ_LINE. */
                if (beat_count == {1'b0, ddr_burstcnt})
                    state <= ST_BURST_DONE;
                else if (timeout_cnt == TIMEOUT_MAX)
                    state <= ST_IDLE;
                else if (!ddr_dout_ready)
                    timeout_cnt <= timeout_cnt + 20'd1;
            end

            ST_BURST_DONE: begin
                /* Phase 5 task #19: decrement remaining by this burst's
                 * count; advance addr by same. Loop to ST_READ_LINE for
                 * next burst if more remain, else ST_LINE_DONE.
                 *
                 * Note: ddr_addr advances by qword count (Avalon-MM
                 * address is qword-indexed in this design). */
                qwords_remaining <= qwords_remaining - {2'd0, ddr_burstcnt};
                ddr_addr         <= ddr_addr + {21'd0, ddr_burstcnt};

                if (qwords_remaining > {2'd0, ddr_burstcnt})
                    state <= ST_READ_LINE;       /* more bursts for this line */
                else
                    state <= ST_LINE_DONE;       /* line complete */
            end

            ST_LINE_DONE: begin
                src_line <= src_line + 11'd1;

                if (src_line == src_height - 11'd1) begin
                    first_frame_loaded <= 1'b1;
                    frame_ready_reg    <= 1'b1;
                    preloading         <= 1'b0;
                    state              <= ST_IDLE;
                end
                else if (preloading && src_line < 11'd1)
                    state <= ST_LINE_BEGIN;       /* Phase 5 task #19 */
                else begin
                    preloading <= 1'b0;
                    state      <= ST_WAIT_DISPLAY;
                end
            end

            ST_WAIT_DISPLAY: begin
                /* Phase 5 task #19: pace by FIFO room for one max-size
                 * burst (128 qw). Multi-burst lines reissue ST_READ_LINE
                 * which has its own per-burst FIFO check, so this state
                 * just gates the START of a new source line. */
                if (src_line < src_height &&
                    (({2'd0, fifo_wrusedw} + {2'd0, MAX_BURST_QW[7:0]}) <= 10'd256))
                    state <= ST_LINE_BEGIN;       /* Phase 5 task #19 */
            end

            // -- Audio path: poll wr_ptr, read ring, write rd_ptr ---
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
                // Now audio_bytes_avail is valid (audio_wr_ptr just latched).
                if (audio_bytes_avail == 32'd0) begin
                    // Ring empty -- back off briefly to avoid DDR3 spam.
                    audio_backoff <= 20'h01000;  // ~42 us at 98.44 MHz clk_sys
                    state         <= ST_IDLE;
                end
                else if (!audio_fifo_low) begin
                    // FIFO filled up while we were polling; don't fetch.
                    state <= ST_IDLE;
                end
                else if (audio_plan_bytes == 32'd0) begin
                    state <= ST_IDLE;
                end
                else begin
                    // Plan a burst: min(bytes_avail, 256, ring_wrap_room),
                    // floored to a multiple of 8 bytes (one qword).
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

            default: state <= ST_IDLE;
        endcase
    end
end

// -- Dual-Clock FIFO --------------------------------------------------
// 64-bit wide, stores raw DDR3 beats (4 RGB565 pixels per entry).
// Depth 256 to hold 2 preloaded scanlines (80 beats each = 160 total).
//
// Option Y Phase 4: line_fifo now has TWO potential consumers — the
// legacy pixel-output block (`fifo_rd`) AND the downscale module
// (`src_fifo_rd_i`). Reader OR's both signals on rdreq. Top.sv ensures
// only one consumer is active at a time (Phase 4d swap removes the
// legacy block's `fifo_rd` consumption).
wire [63:0] fifo_rd_data;
wire        fifo_empty;
reg         fifo_rd;

// Option Y Phase 4: expose line_fifo signals + source dims as outputs
// for the downscale module (instantiated in top.sv).
assign src_fifo_rd_data_o = fifo_rd_data;
assign src_fifo_empty_o   = fifo_empty;
assign src_width_o        = src_width;
assign src_height_o       = src_height;
// src_frame_start_o pulses 1 ddr_clk when ST_CHECK_CTRL transitions to
// ST_LINE_BEGIN on a new-frame detection (src_line set to 0). Phase 5
// task #21 downscale aligns this to vblank in clk_vid domain.
reg src_frame_start_r;
assign src_frame_start_o  = src_frame_start_r;

dcfifo #(
    .intended_device_family ("Cyclone V"),
    .lpm_numwords           (256),
    .lpm_showahead          ("ON"),
    .lpm_type               ("dcfifo"),
    .lpm_width              (64),
    .lpm_widthu             (8),
    .overflow_checking      ("ON"),
    .rdsync_delaypipe       (4),
    .underflow_checking     ("ON"),
    .use_eab                ("ON"),
    .wrsync_delaypipe       (4)
) line_fifo (
    .aclr     (fifo_aclr),
    .data     (fifo_wr_data),
    .rdclk    (clk_vid),
    .rdreq    (fifo_rd | src_fifo_rd_i),
    .wrclk    (ddr_clk),
    .wrreq    (fifo_wr),
    .q        (fifo_rd_data),
    .rdempty  (fifo_empty),
    .wrfull   (fifo_full),
    .eccstatus(),
    .rdfull   (),
    .rdusedw  (),
    .wrempty  (),
    .wrusedw  (fifo_wrusedw)        /* Option Y Phase 4 hotfix: expose for pacing */
);

// -- Pixel Output (1:1, no doubling) ----------------------------------
//
// Each 64-bit FIFO word = 4 source pixels (RGB565).
// No horizontal doubling -- one source pixel = one display pixel.
// Each FIFO word produces exactly 4 display pixels.
//
// pixel_sub[1:0] selects which of the 4 source pixels (0..3)
//
reg  [63:0] pixel_word;
reg  [1:0]  pixel_sub;
reg         pixel_word_valid;

// RGB565 decode from current sub-pixel
wire [15:0] cur_pix = pixel_word[{pixel_sub, 4'b0000} +: 16];
wire  [7:0] dec_r = {cur_pix[15:11], cur_pix[15:13]};
wire  [7:0] dec_g = {cur_pix[10:5],  cur_pix[10:9]};
wire  [7:0] dec_b = {cur_pix[4:0],   cur_pix[4:2]};

always @(posedge clk_vid) begin
    if (reset_vid) begin
        fifo_rd          <= 1'b0;
        r_out            <= 8'd0;
        g_out            <= 8'd0;
        b_out            <= 8'd0;
        pixel_word       <= 64'd0;
        pixel_sub        <= 2'd0;
        pixel_word_valid <= 1'b0;
    end
    else begin
        fifo_rd <= 1'b0;

        if (ce_pix) begin
            if (de && frame_ready_vid) begin
                if (pixel_word_valid) begin
                    // Output current pixel
                    r_out <= dec_r;
                    g_out <= dec_g;
                    b_out <= dec_b;

                    if (pixel_sub == 2'd3) begin
                        // Word exhausted -- load next from FIFO
                        pixel_word_valid <= 1'b0;
                        if (!fifo_empty) begin
                            pixel_word       <= fifo_rd_data;
                            pixel_word_valid <= 1'b1;
                            pixel_sub        <= 2'd0;
                            fifo_rd          <= 1'b1;
                        end
                    end
                    else begin
                        pixel_sub <= pixel_sub + 2'd1;
                    end
                end
                else if (!fifo_empty) begin
                    // Load first word from FIFO (show-ahead)
                    pixel_word       <= fifo_rd_data;
                    pixel_word_valid <= 1'b1;
                    pixel_sub        <= 2'd0;
                    fifo_rd          <= 1'b1;
                    // Output first pixel immediately
                    r_out <= {fifo_rd_data[15:11], fifo_rd_data[15:13]};
                    g_out <= {fifo_rd_data[10:5],  fifo_rd_data[10:9]};
                    b_out <= {fifo_rd_data[4:0],   fifo_rd_data[4:2]};
                end
                else begin
                    r_out <= 8'd0;
                    g_out <= 8'd0;
                    b_out <= 8'd0;
                end
            end
            else begin
                // Outside active display
                r_out            <= 8'd0;
                g_out            <= 8'd0;
                b_out            <= 8'd0;
                pixel_sub        <= 2'd0;
                pixel_word_valid <= 1'b0;
            end
        end
    end
end

// -- Audio dual-clock FIFO (ddr_clk write, clk_audio read) -----------
// 64-bit wide (= 2 stereo frames per entry), 1024 deep. Read side
// alternates halves, popping every other sample.
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

// -- 48 kHz sample clock (clk_audio / 512 = 48 kHz exactly) ----------
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

// -- Audio sample output (clk_audio domain) --------------------------
// Each FIFO entry carries two stereo frames:
//   [15:0]   L0
//   [31:16]  R0
//   [47:32]  L1
//   [63:48]  R1
// Alternate halves, pop every other tick.
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
                    audio_fifo_rd <= 1'b1;       // advance to next qword
                    half_sel      <= 1'b0;
                end
            end
            // else: underflow, hold previous sample
        end
    end
end

endmodule
