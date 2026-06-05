# Option Y — Phase 1: Architecture Design

> **Status**: Phase 1 deliverable. Awaiting user review before Phase 2 implementation starts.
>
> **Author**: MiSTerOrganize
> **Date**: 2026-06-05
> **Supersedes**: Step 60 polyphase implementation (commit `2ce015b` and following, archived in branch `polyphase-archive-20260605`)

---

## 1. Goals

Variable-resolution native-source-capture FPGA downscale pipeline for OpenBOR PAKs ranging from 320×240 (ATOV) to 1920×1080 (Lust Rush and any future HD-authored PAK). Produces 320×224 dest for Sega CD NTSC timing match.

**Specific deliverables**:
1. ARM-side `WriteFrame` writes source-native pixels to DDR3, **NO** software squish. Saves ~3ms ARM time per frame.
2. FPGA-side variable-res reader configurable per-frame via DDR3 ctrl word.
3. FPGA-side **edge-aware NN/bilinear hybrid** downscale module (replaces failed polyphase from Step 60).
4. Sister-core mirror to MiSTer_OpenBOR_4086.

## 2. Non-goals

- **fps boost for engine-bound PAKs (He-Man heavy scenes, etc.)** — this is engine-level work, a separate workstream. Option Y is a render-stage redesign only.
- **Engine code changes** — none. Pure ARM-wrapper + FPGA-side changes.
- **CRT scanline overlays** — future enhancement, deferred.
- **Per-PAK aspect-ratio modes** (Original / Full Screen / ARC1 / ARC2) — framework features for the future; Option Y just preserves source dims through to FPGA scaler so those modes are POSSIBLE later.

## 3. Critical lessons from polyphase (DO NOT REPEAT)

| Lesson | Where it bit us | Fix in Option Y |
|---|---|---|
| **"Match engine character" > "best filter on the audio ladder"** | I chose polyphase because it sounded like "best quality." It blurred OpenBOR's hard-edged sprite + text content. | Edge-aware NN/bilinear hybrid: NN on high-contrast (preserves edges), bilinear on smooth (no jaggies). Matches engine character. |
| **Multi-pipeline FPGA designs with parallel CDC of frame-start signals create race classes** | Step 60 had V-pass `new_frame` (from timing module) and H-pass `src_frame_start_sync` (from reader). They CDC'd separately. 36% of frames had them out of sync at the d=100 V-pass snap. | **Single frame-start fanout**: ONE register sources `new_frame` for both H-pass and V-pass, propagated within the same clock cycle. NO parallel CDC of the same logical event. |
| **Producer-consumer slot rings without explicit handshake race** | 5-slot ring with H-pass writing freely and V-pass reading freely → H-pass overruns when reader produces ahead, or V-pass picks empty slots when H-pass underruns. | **Explicit producer-consumer handshake**: 2-slot ring (down from 5), V-pass STALLS when needed slot not ready, H-pass CAN'T overwrite a slot V-pass is still using. Ping-pong protocol with `slot_valid_for_frame[N]` flags. |
| **Polyphase's filter complexity requires a deep slot ring** | 4-tap V kernel = needs 4 source lines simultaneously = 5-slot ring with race class. | 2-tap edge-aware = needs 2 source lines = 2-slot ring (50% less M10K, much smaller race surface). |
| **Diagnostic-driven debug always trumps speculation-driven** | Phases 5-9 were guess-fix cycles. Phase 12 certainty probe finally pinned the bug. | Build DIAG instrumentation IN FROM THE START — slot-valid + reader-aligned counters + CDC frame-count comparison fields in dbg_ output. CI gate auto-skips diag binaries per `[[no-diagnostic-binaries-in-db]]`. |

## 4. Architecture Overview

```
   ARM (clk_sys)                    FPGA (clk_sys + clk_vid)
  +-----------------+              +--------------------------+
  | OpenBOR engine  |              |                          |
  |   renders WxH   |              |  reader.sv (clk_sys):    |
  |   native        |              |    reads CTRL + DIM word |
  |       |        |              |    paces by src_target    |
  |       v        |              |    streams source pixels  |
  | WriteFrame --->| DDR3 native  |    into line_fifo (CDC)   |
  |  (no squish)   |  buffer       |                          |
  +-----------------+   WxH x 16bpp |  line_fifo (CDC: sys→vid)|
                                   |                          |
                                   |  downscale.sv (clk_vid):  |
                                   |    H-pass: src→320 cols  |
                                   |    V-pass: 2-tap edge-   |
                                   |      aware to 224 rows   |
                                   |    sync_unit: single    |
                                   |      frame-start fanout |
                                   |    slot ring: 2 slots + |
                                   |      explicit handshake |
                                   |        |                 |
                                   |        v                 |
                                   | 320x224 RGB888 → HDMI    |
                                   +--------------------------+
```

**Pipeline stages**:

1. **ARM `WriteFrame`** — writes source-native pixels to DDR3 buffer 0 or 1 (toggles each frame). Also writes atomic CTRL+DIM 64-bit pair to signal "new frame ready, dims W×H".
2. **Reader (clk_sys)** — detects CTRL frame_counter increment, latches DIM. Reads source pixels from DDR3 paced by `src_target = dest_line * src_h / dest_h`. Writes pixels into line_fifo (clk_sys → clk_vid CDC FIFO).
3. **Downscale H-pass (clk_vid)** — reads line_fifo, applies edge-aware X-downscale (similar 2×2 neighborhood logic but 1D in X), produces 320-wide rows into line buffer.
4. **Downscale V-pass (clk_vid)** — reads 2 line buffers (current + next source line), applies edge-aware Y-downscale per dest pixel, emits 320×224 RGB888 to HDMI pipeline.
5. **Sync unit (clk_vid)** — single registered fanout of frame-start, drives both H-pass and V-pass start. Producer-consumer handshake for slot writes.

## 5. DDR3 Memory Map

**Current layout** (pre-polyphase, fixed 320×224):
```
0x3A000000 + 0x00000 : CTRL (32-bit)
0x3A000000 + 0x00008 : Joystick P1
0x3A000000 + 0x00010 : Cart control
0x3A000000 + 0x00018 : Joystick P2
0x3A000000 + 0x00020 : Joystick P3
0x3A000000 + 0x00028 : Joystick P4
0x3A000000 + 0x00030 : Audio ring write ptr
0x3A000000 + 0x00038 : Audio ring read ptr
0x3A000000 + 0x00040 : Buffer 0 (320×224×16bpp = 143360 bytes)
0x3A040040           : Buffer 1
0x3A080000           : Cart data (PAK)
0x3A0D0000           : Audio ring buffer (64 KiB)
```

**Option Y layout** (variable-res, up to 1920×1080×16bpp):
```
0x3A000000 + 0x00000 : CTRL (32-bit, [0:1]=active_buf, [2:31]=frame_counter)
0x3A000000 + 0x00004 : DIM  (32-bit, [10:0]=width, [21:11]=height, [31:22]=reserved)
0x3A000000 + 0x00008 : Joystick P1
0x3A000000 + 0x00010 : Cart control
0x3A000000 + 0x00018 : Joystick P2
0x3A000000 + 0x00020 : Joystick P3
0x3A000000 + 0x00028 : Joystick P4
0x3A000000 + 0x00030 : Audio ring write ptr
0x3A000000 + 0x00038 : Audio ring read ptr
0x3A000000 + 0x00040 : Buffer 0 base
0x3A400000           : Buffer 1 base    (4MB aligned for clean addressing)
0x3A800000           : Cart data (PAK)
0x3A880000           : Audio ring buffer (64 KiB)
```

**Each buffer**:
- Max size: 1920 × 1080 × 16bpp = 4,147,200 bytes ≈ **4 MB per buffer**
- Aligned to 4MB boundary for clean 22-bit qword addressing
- Source pixels stored row-major: row 0 = first 1920×2 = 3840 bytes, row 1 starts at offset 3840, etc.
- For source W < 1920, **only the first W pixels of each row are valid**; tail unused
- For source H < 1080, **only the first H rows are valid**; tail unused
- ARM writes contiguous WxH region; reader knows W,H from DIM word

**Atomic CTRL+DIM write** (lesson from Step 60 Phase 5 Bug B):
- ARM uses `*(volatile uint64_t*)(ddr_base) = (DIM << 32) | CTRL;` — single 64-bit store
- Cortex-A9 + NEON guarantees this is atomic at the bus level
- Reader's CDC syncs both halves together (same 64-bit fetch)
- **No more split CTRL writes followed by separate DIM** (caused race in Step 60)

## 6. CTRL + DIM Word Encoding

**CTRL (32 bits at offset 0x00)**:
| Bits | Field | Purpose |
|---|---|---|
| `[1:0]` | `active_buf` | 0 or 1 — which buffer ARM just wrote |
| `[31:2]` | `frame_counter[29:0]` | Monotonic frame index; FPGA detects increment as "new frame ready" |

**DIM (32 bits at offset 0x04)**:
| Bits | Field | Purpose |
|---|---|---|
| `[10:0]` | `src_width` | 1..1920 (PAK's native X dimension) |
| `[21:11]` | `src_height` | 1..1080 (PAK's native Y dimension) |
| `[31:22]` | reserved (0) | Future: aspect-ratio mode, pixel-perfect flag, etc. |

**ARM writes per-frame**:
```c
uint32_t ctrl = (frame_counter << 2) | active_buf;
uint32_t dim  = (src_height << 11) | src_width;
*(volatile uint64_t*)(ddr_base) = ((uint64_t)dim << 32) | ctrl;
__sync_synchronize();  /* DSB SY — ensure all buffer writes precede ctrl */
```

**FPGA reader reads atomically** (single 64-bit DDR3 fetch into ctrl_dim_reg).

## 7. Reader Interface (Phase 3)

`openbor_video_reader.sv` (clk_sys domain):

```verilog
module openbor_video_reader (
    input  wire        ddr_clk,
    /* ... DDR3 master interface ... */

    /* Variable-res outputs to downscale */
    output reg  [10:0] src_width_o,        // 1..1920 from DIM
    output reg  [10:0] src_height_o,       // 1..1080 from DIM
    output reg         src_frame_start_o,  // pulses 1 cycle at start of frame
    output reg         src_line_done_o,    // pulses 1 cycle when each src line completes
    output reg [63:0]  src_pixel_word_o,   // 4 RGB565 pixels per qword
    output reg         src_pixel_valid_o,
    input  wire        src_fifo_ready_i,   // backpressure from downscale's line_fifo

    /* Pacing input from downscale V-pass */
    input  wire [10:0] dest_line_gray_i    // V-pass dest_line gray-coded (CDC sys←vid)
);
```

**Reader state machine**:
1. `IDLE` — wait for CTRL frame_counter increment
2. `LATCH_DIM` — read DIM word, latch src_width and src_height
3. `READ_LINE` — DDR3 burst-read N qwords (where N = ceil(src_width / 4)) for one source row
4. `WAIT_PACE` — hold next line until V-pass advances dest_line s.t. src_target > current_src_line
5. Loop to `READ_LINE` until src_height lines read
6. Back to `IDLE`

**Pacing formula**: `src_target = (dest_line * src_height) / DEST_HEIGHT` where DEST_HEIGHT=224. Reader holds `READ_LINE` advance until `current_src_line <= src_target + LOOKAHEAD` (where LOOKAHEAD=2 to give H-pass headroom).

## 8. Downscale Module (Phase 4) — THE EDGE-AWARE NN/BILINEAR HYBRID

`openbor_video_downscale.sv` (clk_vid domain):

```verilog
module openbor_video_downscale (
    input  wire        clk_vid,
    input  wire        clk_sys,
    input  wire        reset,

    /* Timing */
    input  wire        de, hblank, vblank,
    input  wire        new_frame, new_line,

    /* Source pixel stream from reader */
    input  wire [63:0] src_pixel_word_i,
    input  wire        src_pixel_valid_i,
    output reg         src_fifo_ready_o,

    /* Source dims (latched at src_frame_start) */
    input  wire [10:0] src_width_i,
    input  wire [10:0] src_height_i,
    input  wire        src_frame_start_i,

    /* Edge threshold config (default 24) */
    input  wire  [7:0] edge_threshold_i,

    /* Dest pixel output */
    output reg   [7:0] r_out, g_out, b_out
);
```

### 8.1 H-pass — X-axis downscale

Same Bresenham-paced approach as Step 60, BUT with edge-aware sample selection:

For each source pixel processed:
1. Accumulate phase_h += h_step_fp (`= DEST_WIDTH << 16 / src_width`).
2. When phase_h crosses FP_ONE, emit one dest column.
3. **Emit decision**: sample 2 adjacent source pixels (current `src_col` and `src_col+1`). Compute luma contrast.
4. If contrast > threshold → dest pixel = nearest source pixel (NN).
5. Else → dest pixel = linear blend weighted by phase_h fractional.
6. Write dest pixel into line buffer at `dest_col_out`.

H-pass produces 320-pixel wide rows into the line buffer.

### 8.2 V-pass — Y-axis downscale (edge-aware 2-tap)

For each dest line K:
1. `src_line_top = floor(K * src_height / DEST_HEIGHT)`
2. `src_line_bot = src_line_top + 1` (clamped to src_height-1 at end)
3. Find slot holding `src_line_top` and slot holding `src_line_bot` in the 2-slot ring.
4. **If either slot not ready → STALL (don't advance dest_line, hold last output)**. This is the producer-consumer handshake.
5. For each dest pixel (hpos 0..319):
   - Load 2 samples: `pix_top = line_buf[top_slot][hpos]`, `pix_bot = line_buf[bot_slot][hpos]`
   - Compute luma: `luma_top = (R+2G+B)>>2`, same for bot
   - Contrast: `|luma_top - luma_bot|`
   - If contrast > threshold → output = nearer source pixel (depends on phase_v frac)
   - Else → output = linear blend weighted by phase_v frac

### 8.3 Edge detection — 2-sample contrast

```verilog
function edge_sharp(input [7:0] luma_top, luma_bot, input [7:0] threshold);
    edge_sharp = (luma_top > luma_bot)
                ? ((luma_top - luma_bot) > threshold)
                : ((luma_bot - luma_top) > threshold);
endfunction
```

**Luma approximation**: `(R*2 + G*5 + B*1) >> 3` (Rec.601 weights, fixed-point Q1.7). Simpler: `(R + 2G + B) >> 2`. Both fit in a single MAC.

### 8.4 Slot ring — 2 slots with handshake

```verilog
reg [15:0] line_buf [0:639];     // 2 slots × 320 entries
reg [10:0] slot_src_line [0:1];  // which source line each slot holds (or 11'h7FF = empty)
reg [1:0]  slot_valid;            // bit N = slot N has current-frame data
reg        write_slot;             // 0 or 1 — which slot H-pass is filling NEXT
```

**Handshake protocol**:
- At `src_frame_start_sync` rising:
  - `slot_valid <= 2'b00` (both slots invalid)
  - `slot_src_line[0] <= 11'h7FF`, `[1] <= 11'h7FF`
  - `write_slot <= 0`
- H-pass writes line N to slot S:
  - When line complete: `slot_src_line[S] <= N`; `slot_valid[S] <= 1`
  - Toggle: `write_slot <= ~write_slot`
- H-pass STALLS before writing to slot S when:
  - `slot_valid[S] == 1` AND `slot_src_line[S]` is NEEDED by current V-pass dest_line
  - V-pass exports its current `src_line_top` and `src_line_bot`; H-pass compares
- V-pass STALLS dest_line advance when:
  - `slot_src_line[0] != needed_top` AND `slot_src_line[1] != needed_top` (top slot not ready)
  - OR same for bot
- V-pass advances when both top and bot slots are ready

This is the **explicit producer-consumer handshake** — no CDC race, no stale slots.

### 8.5 Frame-start fanout (CDC race elimination)

```verilog
// Single registered fanout — both H-pass and V-pass see frame start
// in the SAME clk_vid cycle.
reg src_frame_start_sync_q;
always @(posedge clk_vid) begin
    src_frame_start_sync_q <= src_frame_start_i;
end
wire frame_start_pulse = src_frame_start_i & ~src_frame_start_sync_q;

// H-pass uses: frame_start_pulse
// V-pass uses: frame_start_pulse
// Both fire on the SAME rising edge with SAME latency. No race.
```

NOTE: this REPLACES Step 60's pattern of `tim_new_frame` (V-pass) and `src_frame_start_o` (H-pass) being separately CDC'd. ONE signal, ONE fanout.

## 9. Resource Estimate

| Resource | Step 60 polyphase | Option Y edge-aware | Delta |
|---|---|---|---|
| Line buffer (M10K) | 5 × 320 × 16 = ~12 M10K | 2 × 320 × 16 = ~5 M10K | **-58%** |
| Polyphase coef ROM | 32 phases × 4 taps × 9b = 128 entries | none | **-100%** |
| V-pass multipliers (DSP) | 4 muls per channel × 3 channels = 12 DSP | 2 muls per channel × 3 channels = 6 DSP | **-50%** |
| Edge-detect comparators | 12 (max-min on 4 taps × 3 channels) | 2 (one per axis) | **-83%** |
| Total ALMs (estimate) | ~3500 | ~2000 | **-43%** |
| Total RTL LOC | ~1050 (downscale.sv) | ~600 | **-43%** |

Edge-aware is **smaller** in every dimension than polyphase. Better timing closure expected.

## 10. Diagnostic instrumentation (TEMPORARY DIAG, from day one)

Per `[[no-false-found-it]]` + `[[signaltap-ring-buffer-via-ddr3-for-mister-debug]]`: build diagnostic probes IN FROM THE START, gated by TEMPORARY DIAG markers (CI gate auto-skips binary commit-back per `[[no-diagnostic-binaries-in-db]]`).

**Required probe fields** (in dbg_state register, captured at every dest_line=99→100 transition):
- `slot_valid[1:0]` — current slot-ring validity state
- `slot_src_line[0]`, `slot_src_line[1]` — what source lines slots hold
- `current_v_pass_src_line_top`, `_bot` — what V-pass needs
- `current_h_pass_src_line_in` — H-pass's source line position
- `reader_src_line` (CDC'd from clk_sys) — reader's position
- `h_pass_active`, `v_pass_active` — pipeline state flags
- `frame_start_count`, `vpass_new_frame_count` — sync verification

DDR3 ring-buffer probe ALSO included from day one — same pattern as Phase 10 (256 samples × 64-bit, sampled every 4096 clk_sys cycles, captures pacing trajectory).

## 11. Implementation phase summary

| Phase | Owner | Deliverable | Effort |
|---|---|---|---|
| 1 | This doc | Architecture design (THIS) | ✅ done |
| 2 | ARM-side | `WriteFrame` native-res, atomic CTRL+DIM, bigger mmap | ~3-4 days |
| 3 | FPGA | Variable-res `reader.sv` | ~3 days |
| 4 | FPGA | New `downscale.sv` edge-aware NN/bilinear | ~5-7 days |
| 5 | FPGA | Timing closure (SEED tune if needed) | ~1-2 days |
| 6 | Hardware | Per-PAK regression (ATOV, TMNT-RP, Avengers, He-Man, PDC2, Cap) + v3.10 palette pipeline check | ~3 days |
| 7 | Mirror | Apply to OpenBOR_4086 | ~2 days |
| **Total** | | | **~3-4 weeks** |

## 12. Success criteria

- ATOV, PDC2, Avengers, He-Man, Cap, TMNT-RP all render correctly
- **Text on He-Man is crisp** (no polyphase blur on text/sprite outlines)
- **No flicker** on any PAK (no slot-race; explicit handshake)
- fps unchanged from current (filter is fps-neutral; engine bottlenecks unchanged)
- v3.10 palette pipeline regression-free (NEVER MODIFY the 12 locked-in patches per top-of-CLAUDE.md banner)
- 4086 + 7533 sister parity
- pll_hdmi slack ≥ +0.3 ns (ship band)

---

## Approval needed before Phase 2

**User review checkpoints** before implementation begins:

1. **DDR3 layout** — is the 4MB-aligned buffer size acceptable? Total DDR3 usage: ~9MB (was ~1MB pre-polyphase). MiSTer has 1GB so trivially fits.
2. **CTRL+DIM atomic write** — agreed on using 64-bit qword for atomicity?
3. **Edge threshold** — start with `24` (Step 60's `BYPASS_THRESH`)?
4. **2-slot ring + handshake** — agreed this is the right approach vs alternatives?
5. **fps expectations** — confirmed Option Y delivers crisp text + no flicker but NOT fps boost (fps work is separate)?

Once approved, Phase 2 starts. Estimated 3-4 days for ARM-side WriteFrame + atomic CTRL+DIM + bigger mmap region.
