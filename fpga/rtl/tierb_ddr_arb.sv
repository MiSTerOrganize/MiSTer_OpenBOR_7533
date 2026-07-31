// ===========================================================================
// tierb_ddr_arb -- minimal strict-priority DDR3 arbiter (video reader wins)
//
// TEMPORARY DIAG -- Tier-B phase 0 f2h bandwidth probe harness.
// REVERT AFTER MEASURED.  Not a shipping module.
//
// Two Avalon-MM masters share the framework's single f2h port:
//
//   port A ("rdr") -- openbor_video_top.  ABSOLUTE PRIORITY.  Never masked
//                     while it is requesting, so it can never be starved.
//   port B ("prb") -- tierb_bw_probe.  Fills every cycle the reader leaves.
//
// The reader's issue points are all of the form
//
//     if (!ddr_busy) begin ... ddr_rd <= 1'b1; end          (issue)
//     if (!ddr_busy) ddr_rd <= 1'b0;                        (hold-only clear)
//
// so a request is *accepted* on the cycle where rd && !busy.  Round 5's
// finding C6 applies: masking a master's request does nothing, because the
// master only raises the request when it already sees !busy.  The only
// correct back-pressure is through `busy` itself, which is what this does.
//
// Reader's busy = DDRAM_BUSY | prb_owns.  It is therefore blocked ONLY while
// the probe genuinely holds an outstanding burst -- at most PRB_MAX_BURST
// beats (128 -> 1.3 us at 98.4375 MHz), against a 63.7 us scanline in which
// the reader needs 0.813 us of bus.  No starvation.
//
// Both masters are strictly single-outstanding for reads, so exactly one
// burst is ever in flight and DDRAM_DOUT_READY routes unambiguously to the
// recorded owner.  Writes are single-beat (burstcnt == 1) on both ports.
// ===========================================================================

module tierb_ddr_arb
(
	input             clk,
	input             reset,

	// ---- downstream: the framework's f2h port ----------------------------
	input             ddr_busy,
	output      [7:0] ddr_burstcnt,
	output     [28:0] ddr_addr,
	input      [63:0] ddr_dout,
	input             ddr_dout_ready,
	output            ddr_rd,
	output     [63:0] ddr_din,
	output      [7:0] ddr_be,
	output            ddr_we,

	// ---- port A: video reader (absolute priority) ------------------------
	output            a_busy,
	input       [7:0] a_burstcnt,
	input      [28:0] a_addr,
	output     [63:0] a_dout,
	output            a_dout_ready,
	input             a_rd,
	input      [63:0] a_din,
	input       [7:0] a_be,
	input             a_we,

	// ---- port B: bandwidth probe -----------------------------------------
	output            b_busy,
	input       [7:0] b_burstcnt,
	input      [28:0] b_addr,
	output     [63:0] b_dout,
	output            b_dout_ready,
	input             b_rd,
	input      [63:0] b_din,
	input       [7:0] b_be,
	input             b_we,

	// ---- observation (for the probe's counters) --------------------------
	output            o_bus_idle,
	output            o_a_active
);

// ---------------------------------------------------------------------------
// Ownership of the single in-flight read burst
// ---------------------------------------------------------------------------
reg        a_owns    = 1'b0;
reg        b_owns    = 1'b0;
reg  [8:0] beats_left = 9'd0;

wire bus_idle   = ~a_owns & ~b_owns;
wire a_active   = a_rd | a_we;
wire b_active   = b_rd | b_we;

// Reader wins any tie.  The probe may only take the bus when it is idle AND
// the reader is silent this cycle.
wire a_go = a_active & bus_idle;
wire b_go = b_active & bus_idle & ~a_active;

// ---------------------------------------------------------------------------
// Downstream mux
// ---------------------------------------------------------------------------
assign ddr_rd       = a_go ? a_rd : (b_go ? b_rd : 1'b0);
assign ddr_we       = a_go ? a_we : (b_go ? b_we : 1'b0);
assign ddr_addr     = b_go ? b_addr     : a_addr;
assign ddr_burstcnt = b_go ? b_burstcnt : a_burstcnt;
assign ddr_din      = b_go ? b_din      : a_din;
assign ddr_be       = b_go ? b_be       : a_be;

// ---------------------------------------------------------------------------
// Back-pressure
//
// The reader sees busy only while the probe actually holds the bus.  The
// probe sees busy whenever it is not selected -- which, with hold-only clear
// semantics on its own side, means it simply retries until granted.
// ---------------------------------------------------------------------------
assign a_busy = ddr_busy | b_owns;

// NOTE: b_busy deliberately does NOT include the probe's own ownership.  Both
// masters use hold-only request clears (`if (!busy) rd <= 0`), so a master that
// saw busy for the whole duration of its own burst could never clear its
// request -- and would re-issue the stale address the instant the burst
// retired.  The reader is safe from this only because a_busy excludes a_owns;
// port B must match.  Re-issue is prevented structurally by b_go's `bus_idle`
// term, not by back-pressure.
assign b_busy = ddr_busy | a_active | a_owns;

// ---------------------------------------------------------------------------
// Read-beat accounting.  Acceptance is (rd && !busy) on the selected port.
// Ownership is asserted combinationally on the acceptance cycle as well as
// registered, so a (physically impossible) zero-latency return still routes.
// ---------------------------------------------------------------------------
wire acc_a_rd = a_go & a_rd & ~ddr_busy;
wire acc_b_rd = b_go & b_rd & ~ddr_busy;

wire a_owns_now = a_owns | acc_a_rd;
wire b_owns_now = b_owns | acc_b_rd;

assign a_dout       = ddr_dout;
assign b_dout       = ddr_dout;
assign a_dout_ready = ddr_dout_ready & a_owns_now;
assign b_dout_ready = ddr_dout_ready & b_owns_now;

always @(posedge clk) begin
	if (reset) begin
		a_owns     <= 1'b0;
		b_owns     <= 1'b0;
		beats_left <= 9'd0;
	end
	else if (acc_a_rd) begin
		a_owns     <= 1'b1;
		b_owns     <= 1'b0;
		beats_left <= {1'b0, a_burstcnt};
	end
	else if (acc_b_rd) begin
		a_owns     <= 1'b0;
		b_owns     <= 1'b1;
		beats_left <= {1'b0, b_burstcnt};
	end
	else if (ddr_dout_ready && beats_left != 9'd0) begin
		beats_left <= beats_left - 9'd1;
		if (beats_left == 9'd1) begin
			a_owns <= 1'b0;
			b_owns <= 1'b0;
		end
	end
end

assign o_bus_idle = bus_idle;
assign o_a_active = a_active;

endmodule
