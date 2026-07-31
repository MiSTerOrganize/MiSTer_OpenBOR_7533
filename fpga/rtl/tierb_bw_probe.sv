// ===========================================================================
// tierb_bw_probe -- f2h DDR3 read-bandwidth probe (Tier-B phase 0 GO/NO-GO)
//
// TEMPORARY DIAG -- measurement harness only.  REVERT AFTER MEASURED.
// Not a shipping module; must never appear in a released RBF.
//
// WHY THIS EXISTS
// ---------------
// The Tier-B architecture document gates the whole project on a "433 MB/s
// conservative ceiling" for the FPGA->DDR3 path.  An exhaustive search of the
// workspace and git history found no derivation of that number anywhere: it
// is a bare parenthetical, and it is section 15's only hard bandwidth gate.
// OPTION4_FPGA_BLEND_OFFLOAD_SCOPE.md section 4 scoped this probe as the
// go/no-go gate and it was never run.  This module runs it.
//
// WHAT IT MEASURES
// ----------------
// Achieved read throughput on the framework's single f2h port, *under live
// contention* with the video reader (absolute priority, via tierb_ddr_arb)
// and with the A9 hitting the same SDRAM controller through its own port.
//
// It sweeps 16 steps -- eight burst lengths {1,2,4,8,16,32,64,128 beats}
// crossed with two address patterns -- because the answer is a curve, not a
// scalar.  The compositor's sprite fetch is short scattered row reads
// (~2-8 beats at pseudo-random addresses); the background fetch is long
// sequential runs.  A single "peak sequential" figure would flatter the
// short-scattered case, which is the one that actually binds.
//
// Strictly single-outstanding: issue a burst, drain every beat, issue the
// next.  That is the protocol openbor_video_reader.sv already proves works on
// this bridge.  If single-outstanding at the useful burst lengths clears the
// requirement, no pipelining is needed.  If it does not, "you need multiple
// outstanding reads" is itself the finding.
//
// READ-ONLY traffic.  The read side is what binds (section 14.4.5 puts sprite
// + background reads at 100-360 MB/s against 8.59 MB/s of scanout writes), and
// read-only means the probe cannot corrupt anything.  The only writes it makes
// are the result words in its own private region.
//
// RESULT BLOCK -- 49 qwords at PROBE_BASE (0x3A0F0000), little-endian:
//
//   +0x000  {sweep_count[31:0], 32'h54425731}          magic "1WBT"
//   +0x008  step 0  A = {bursts[31:0],  beats[31:0]}
//   +0x010  step 0  B = {stall[31:0],   ddrbusy[31:0]}
//   +0x018  step 0  C = {32'd0, swallow[15:0], pattern[3:0], log2burst[3:0],
//                        step[7:0]}
//   +0x020  step 1  A ... and so on, 3 qwords per step, 16 steps.
//
//   beats    -- DDRAM_DOUT_READY cycles credited to this port in the window
//   bursts   -- read commands accepted
//   ddrbusy  -- cycles the framework asserted DDRAM_BUSY
//   stall    -- cycles the probe wanted the bus and could not have it
//   swallow  -- bursts abandoned on beat timeout (expect 0)
//
//   MB/s = beats * 8 * (CLK_HZ / WINDOW_CYCLES) / 1e6
//
// Each window is followed by an equal idle gap so the machine stays usable
// while the sweep runs; the in-window figure is unaffected by the gap.
// ===========================================================================

module tierb_bw_probe
(
	input             clk,
	input             reset,

	// Avalon-MM master (arbiter port B)
	input             ddr_busy,
	output reg  [7:0] ddr_burstcnt,
	output reg [28:0] ddr_addr,
	input      [63:0] ddr_dout,
	input             ddr_dout_ready,
	output reg        ddr_rd,
	output reg [63:0] ddr_din,
	output reg  [7:0] ddr_be,
	output reg        ddr_we,

	// observation from the arbiter
	input             o_bus_idle,
	input             o_a_active,

	// raw framework busy, for the contention counter
	input             raw_ddr_busy
);

// ---------------------------------------------------------------------------
// Geometry
// ---------------------------------------------------------------------------
// Traffic region: the low 512 KiB of the core's DDR3 window.  Reads only, so
// overlapping the framebuffers / cart data / audio ring is harmless, and it
// is realistic: this is exactly the memory the compositor would be fetching.
localparam [28:0] TRAFFIC_BASE   = 29'h07400000;   // 0x3A000000 >> 3
localparam [16:0] TRAFFIC_QWORDS = 17'h10000;      // 65536 qwords = 512 KiB

// Private result block, 64 KiB clear of the audio ring end (0x3A0E0000) and
// inside the 1 MiB core window.
localparam [28:0] PROBE_BASE     = 29'h0741E000;   // 0x3A0F0000 >> 3

// 2^24 cycles at 98.4375 MHz = 170.4 ms per window.
localparam [24:0] WINDOW_CYCLES  = 25'h1000000;
// 2^27 cycles = 1.36 s of settle before the first sweep, so the engine is up.
localparam [27:0] SETTLE_CYCLES  = 28'h8000000;
// Abandon a burst whose beats stop arriving for 2^16 cycles (0.67 ms).
localparam [15:0] BEAT_TIMEOUT   = 16'hFFFF;

localparam [3:0] N_STEPS_M1 = 4'd15;

// ---------------------------------------------------------------------------
// FSM
// ---------------------------------------------------------------------------
localparam [2:0] ST_SETTLE = 3'd0;
localparam [2:0] ST_HDR    = 3'd1;
localparam [2:0] ST_ISSUE  = 3'd2;
localparam [2:0] ST_DRAIN  = 3'd3;
localparam [2:0] ST_EMIT   = 3'd4;
localparam [2:0] ST_GAP    = 3'd5;

reg  [2:0] state = ST_SETTLE;

reg [27:0] settle_cnt = 28'd0;
reg [24:0] win_cnt    = 25'd0;
reg  [3:0] step       = 4'd0;
reg [31:0] sweep_cnt  = 32'd0;

// per-window counters
reg [31:0] c_beats   = 32'd0;
reg [31:0] c_bursts  = 32'd0;
reg [31:0] c_busy    = 32'd0;
reg [31:0] c_stall   = 32'd0;
reg [15:0] c_swallow = 16'd0;

// in-flight burst bookkeeping
reg  [8:0] beats_todo = 9'd0;
reg [15:0] beat_wdog  = 16'd0;

// address generation
reg [16:0] seq_idx = 17'd0;
reg [31:0] lfsr    = 32'hACE1_2345;

wire        pattern    = step[3];              // 0 = sequential, 1 = scattered
wire  [2:0] burst_log2 = step[2:0];
wire  [7:0] burst_len  = 8'd1 << burst_log2;

// Scattered: 64 B aligned inside the low 512 KiB, capped so the longest burst
// (128 qwords) cannot run past the region.
wire [16:0] scat_idx = {4'd0, lfsr[15:3], 3'd0};
wire [16:0] next_idx = pattern ? scat_idx : seq_idx;

wire [31:0] lfsr_next = {lfsr[30:0],
                         lfsr[31] ^ lfsr[21] ^ lfsr[1] ^ lfsr[0]};

// result emit sub-counter: 0 = header, else 3 words per step
reg  [1:0] emit_sel = 2'd0;

// ---------------------------------------------------------------------------
// Result word assembly
// ---------------------------------------------------------------------------
wire [63:0] res_a = {c_bursts, c_beats};
wire [63:0] res_b = {c_stall,  c_busy};
wire [63:0] res_c = {32'd0, c_swallow,
                     3'd0, pattern, 1'b0, burst_log2, 4'd0, step};

// The probe wanted the bus this cycle but did not get it.
wire want_bus = (state == ST_ISSUE);
wire blocked  = want_bus & (raw_ddr_busy | o_a_active | ~o_bus_idle);

always @(posedge clk) begin
	if (reset) begin
		state        <= ST_SETTLE;
		settle_cnt   <= 28'd0;
		win_cnt      <= 25'd0;
		step         <= 4'd0;
		sweep_cnt    <= 32'd0;
		ddr_rd       <= 1'b0;
		ddr_we       <= 1'b0;
		ddr_be       <= 8'hFF;
		ddr_burstcnt <= 8'd1;
		beats_todo   <= 9'd0;
		seq_idx      <= 17'd0;
		lfsr         <= 32'hACE1_2345;
		emit_sel     <= 2'd0;
		c_beats      <= 32'd0;
		c_bursts     <= 32'd0;
		c_busy       <= 32'd0;
		c_stall      <= 32'd0;
		c_swallow    <= 16'd0;
	end
	else begin
		// Hold-only clear, exactly like openbor_video_reader.sv:338-339.
		if (!ddr_busy) ddr_rd <= 1'b0;
		if (!ddr_busy) ddr_we <= 1'b0;

		// Window-scoped contention counters run in both measured states.
		if (state == ST_ISSUE || state == ST_DRAIN) begin
			if (raw_ddr_busy) c_busy  <= c_busy  + 32'd1;
			if (blocked)      c_stall <= c_stall + 32'd1;
		end

		case (state)

		// -------------------------------------------------------------
		ST_SETTLE: begin
			if (settle_cnt == SETTLE_CYCLES) state <= ST_HDR;
			else settle_cnt <= settle_cnt + 28'd1;
		end

		// -------------------------------------------------------------
		// Publish the header, then start step 0.
		//
		// Every transaction below is present-then-confirm.  A request is
		// ACCEPTED on the cycle where (req && !busy) -- the same cycle the
		// hold-only clear fires.  Advancing on presentation instead would
		// overwrite ddr_addr/ddr_din while a request was still pending
		// (the reader wins any tie, so pending is normal), which on a read
		// would strand the burst until the watchdog and count a phantom
		// swallow, and on a write would publish the wrong word.
		ST_HDR: begin
			if (ddr_we && !ddr_busy) begin
				step      <= 4'd0;
				win_cnt   <= 25'd0;
				c_beats   <= 32'd0;
				c_bursts  <= 32'd0;
				c_busy    <= 32'd0;
				c_stall   <= 32'd0;
				c_swallow <= 16'd0;
				seq_idx   <= 17'd0;
				state     <= ST_ISSUE;
			end
			else if (!ddr_we && !ddr_busy) begin
				ddr_addr     <= PROBE_BASE;
				ddr_din      <= {sweep_cnt, 32'h54425731};
				ddr_be       <= 8'hFF;
				ddr_burstcnt <= 8'd1;
				ddr_we       <= 1'b1;
			end
		end

		// -------------------------------------------------------------
		// Measured window: issue a burst whenever the bus is ours.
		ST_ISSUE: begin
			if (win_cnt != WINDOW_CYCLES) win_cnt <= win_cnt + 25'd1;

			if (ddr_rd && !ddr_busy) begin
				// accepted
				beats_todo <= {1'b0, burst_len};
				beat_wdog  <= 16'd0;
				c_bursts   <= c_bursts + 32'd1;

				// advance both generators so the two patterns are
				// independent of each other's history
				lfsr    <= lfsr_next;
				seq_idx <= seq_idx + {9'd0, burst_len};

				state <= ST_DRAIN;
			end
			else if (win_cnt == WINDOW_CYCLES && !ddr_rd) begin
				// window over and nothing in flight -- publish the step
				emit_sel <= 2'd0;
				state    <= ST_EMIT;
			end
			else if (!ddr_rd && !ddr_busy) begin
				// present the next burst
				ddr_addr     <= TRAFFIC_BASE + {12'd0, next_idx};
				ddr_burstcnt <= burst_len;
				ddr_rd       <= 1'b1;
			end
		end

		// -------------------------------------------------------------
		// Drain every beat of the outstanding burst before issuing again.
		ST_DRAIN: begin
			if (win_cnt != WINDOW_CYCLES) win_cnt <= win_cnt + 25'd1;

			if (ddr_dout_ready) begin
				c_beats   <= c_beats + 32'd1;
				beat_wdog <= 16'd0;
				if (beats_todo <= 9'd1) begin
					beats_todo <= 9'd0;
					state      <= ST_ISSUE;
				end
				else
					beats_todo <= beats_todo - 9'd1;
			end
			else if (beat_wdog == BEAT_TIMEOUT) begin
				// The bridge swallowed the burst.  Give up on it, note it,
				// and carry on -- a wedge must not hang the sweep.
				c_swallow  <= c_swallow + 16'd1;
				beats_todo <= 9'd0;
				beat_wdog  <= 16'd0;
				state      <= ST_ISSUE;
			end
			else
				beat_wdog <= beat_wdog + 16'd1;
		end

		// -------------------------------------------------------------
		// Three result words for the step just finished.
		ST_EMIT: begin
			if (ddr_we && !ddr_busy) begin
				// accepted
				if (emit_sel == 2'd2) begin
					win_cnt <= 25'd0;
					state   <= ST_GAP;
				end
				else
					emit_sel <= emit_sel + 2'd1;
			end
			else if (!ddr_we && !ddr_busy) begin
				ddr_addr     <= PROBE_BASE + 29'd1
				                + ({25'd0, step} * 29'd3)
				                + {27'd0, emit_sel};
				ddr_din      <= (emit_sel == 2'd0) ? res_a
				              : (emit_sel == 2'd1) ? res_b : res_c;
				ddr_be       <= 8'hFF;
				ddr_burstcnt <= 8'd1;
				ddr_we       <= 1'b1;
			end
		end

		// -------------------------------------------------------------
		// Idle gap: keeps the machine usable between windows.
		ST_GAP: begin
			if (win_cnt == WINDOW_CYCLES) begin
				win_cnt   <= 25'd0;
				c_beats   <= 32'd0;
				c_bursts  <= 32'd0;
				c_busy    <= 32'd0;
				c_stall   <= 32'd0;
				c_swallow <= 16'd0;
				seq_idx   <= 17'd0;

				if (step == N_STEPS_M1) begin
					sweep_cnt <= sweep_cnt + 32'd1;
					state     <= ST_HDR;
				end
				else begin
					step  <= step + 4'd1;
					state <= ST_ISSUE;
				end
			end
			else
				win_cnt <= win_cnt + 25'd1;
		end

		default: state <= ST_SETTLE;

		endcase
	end
end

endmodule
