# Tier-B — Phase 1: Architecture Design (FPGA compositing offload)

Status: **DESIGN — no RTL written, no engine change, nothing shipped.**
Gates passed: Phase 0 (CPU side) and Phase 0b (FPGA side), both 2026-07-29.
Baseline tag: **`pre-tierb`** (+ branch `pre-tierb-stable`) = shipped ARM binary
`e5e6a8219f3994d7be6743d83c0c5999`, RBF `OpenBOR_7533_20260726.rbf`
(`28a9d368a7454f7dcc891c477a770d29`). `git checkout pre-tierb` restores everything.

This is a MAJOR architectural change (new RTL pipeline, new FPGA-ARM bridge, new memory
map, new CDC paths). It therefore runs under the **iterative-audit-until-zero-concerns
loop** — audits repeat until a full cycle reports zero bugs AND zero concerns across all
19 verification domains, and only then does hardware verification begin.

---

## 1. Goals

1. Move per-sprite compositing off the Cortex-A9 so heavy PAKs stop being
   compositing-bound. Target: **He-Man locked at 59.92 Hz** (today 34-56 fps).
2. Preserve **native-resolution rendering** (960x480 for He-Man) and therefore the
   render-high-then-area-average text-clarity path. No drop to 320x224 compositing.
3. **Byte-identical output** to the shipped path wherever the FPGA handles a frame —
   same box-downscale arithmetic, same palette lookup, same blend results.
4. **Transparent fallback**: any PAK, sprite or blend mode the FPGA cannot handle falls
   back to the existing CPU path with no visual difference and no user-visible switch.

## 2. Non-goals

- Not a general-purpose 2D GPU. It executes exactly the blit shapes the engine's
  `putsprite_x8p16` fast path already produces.
- Not a replacement for `gfx_draw_scale`. Scaled/rotated/water/flipy sprites (28.8% of
  putsprite time) stay on the CPU permanently.
- Not applied to OpenBOR_4086 (archival) or PICO-8 (vsync-locked, not compositing-bound).
- No change to the audio path, the input path, `.s0`/`.s1` handling, or the recorder.

## 3. Critical lessons carried in from prior arcs (DO NOT REPEAT)

| Lesson | Source | Applied here |
|---|---|---|
| A single audit pass is insufficient; cycle N+1 always finds new things | Option Y 6-cycle arc | iterative-audit loop is mandatory, section 13 |
| CDC pulse width must be >= 2x the destination clock period | Option Y concern F (1-cycle pulse at 98 MHz crossing to 53 MHz was missed by a 2-FF sync) | every new fast->slow pulse widened, section 11 |
| Reader pacing assumptions break on non-default geometry | Option Y hotfix #4 (1-line-per-scanline broke H>224) | band drain is decoupled from scanout by the existing double buffer |
| Patch content must be ASCII-only | apply-patches encoding safety | all `sp_new` strings ASCII |
| "All patches applied" does not mean the patch is in the binary | the dropped `write(ob_path, ob)` regression | new patches added to the post-apply integrity gate's signature list |
| The documented optimisation target is often STALE | collision grid + putscreen, both closed this week | every number in this doc is measured on the current shipped engine |
| Price a design against the real DISTRIBUTION, not one example | the 450-PAK census broke this doc's first band rule (section 8.1) | band geometry verified against all 20 distinct resolutions |
| Never take a perf decision from QEMU timings | collision bench `tight` variant | all timing decisions from the A9 or from RTL analysis |

## 4. Architecture overview

Today the ARM composites into a native-res `vscreen`, downscales it with NEON, and writes
320x224 RGB565 into a DDR3 double buffer that the FPGA scans out.

Tier-B inverts the ownership of the framebuffer:

```
  ARM                                     FPGA
  ---                                     ----
  engine logic / entity ticks
  spriteq_draw:
    fast-path sprite  -> emit command  ---> display-list ring (DDR3)
    fallback sprite   -> rasterise to scratch, emit LINEAR command
    background        -> emit LINEAR command
  per-band binning (ARM, cheap)
  publish frame header  ------------------> [ band walker ]
                                                 |
                                            [ RLE fetch engine ] <-- sprite arena (DDR3)
                                                 |
                                            [ palette LUT + blend unit ]
                                                 |
                                            [ BAND BUFFER  (on-chip M10K, 2x) ]
                                                 |
                                            [ box downscaler (hcnt x vcnt) ]
                                                 |
                                            output framebuffer (DDR3, 320x224) --> scanout
```

Key properties:

- **The band buffer is on-chip.** This is what makes the bandwidth budget close
  (133 MB/s vs 369 MB/s for a DDR3-resident framebuffer). See Phase 0b.
- **The ARM never reads back composited pixels.** It writes a display list and forgets.
- **Z-order is exact** because fallbacks are rasterised to a scratch buffer and submitted
  as ordinary `LINEAR` commands in their correct slot — the FPGA executes one strictly
  ordered list per band.
- **The ARM does all the addressing arithmetic.** It pre-seeks every sprite's RLE row
  pointer per band, so the FPGA never walks `linetab` and never does random access.

## 5. The RLE format (verified against pristine v7533 `sprite.c::encodesprite`)

This is the load-bearing fact that shapes the fetch engine. Do not re-derive it from
memory; it was read from upstream source.

```
s_sprite
  header: magic, centerx, centery, width, height, pixelformat, mask, palette
  data[]:
    linetab[h]   int32 x h        linetab[y] = (addr of row y data) - (addr of &linetab[y])
                                  ^ SELF-RELATIVE, not relative to the sprite base
    rowdata      rows stored CONTIGUOUSLY, in increasing y, immediately after linetab
```

Per-row byte stream:

```
  repeat:
    clearcount : 1 byte
        0xFF        -> END OF ROW (next byte begins row y+1)
        0x00..0xFE  -> skip that many destination pixels, then:
            viscount : 1 byte
                0x00       -> nothing visible here, continue the loop
                0x01..0xFF -> that many raw 8-bit palette indices follow, copy them
```

Three consequences:

1. **Rows are contiguous and ordered**, so within a band the fetch engine is a pure
   sequential byte stream. It needs only the address of the band's FIRST row.
2. `linetab` is **self-relative** — the ARM computes
   `row_addr = (uintptr_t)(linetab + y) + linetab[y]` once per (sprite, band) and puts the
   absolute address in the command. **The FPGA never touches `linetab`.**
3. Because a band-spanning sprite is seeked directly to its own rows, the total pixel
   bytes fetched across all its bands equal fetching it once. **Banding costs ~0 extra
   bandwidth.** (Third independent reason to keep the RLE format.)

Pixels are 8-bit palette indices; index 0 is `TRANSPARENT_IDX`. On the 16-bit ship build
`PAL_BYTES = 512`, i.e. a 256-entry RGB565 palette per sprite.

## 6. DDR3 memory map

The core's existing window is at `0x3A000000` (control word, joysticks, cart control,
audio ring pointers, BUF0/BUF1, cart data, audio ring). Tier-B adds three regions.

> **VERIFY BEFORE ALLOCATING (open item, section 14).** The reserved core region appears
> to be `0x30000000..0x40000000` (256 MB), consistent with `rtl/ddram.sv`'s "256MB at the
> end of 1GB" comment and with the existing `0x3A000000` window sitting inside it. This
> must be confirmed against the live kernel reservation before any allocation, not
> assumed. All addresses below are proposals contingent on that check.

| Region | Proposed base | Size | Written by | Read by |
|---|---|---|---|---|
| Sprite arena (RLE + palettes) | `0x30000000` | 64 MB | ARM (at PAK load) | FPGA |
| CPU-fallback scratch (double) | `0x34000000` | 2 MB | ARM (per frame) | FPGA |
| Display-list ring (double) | `0x34200000` | 128 KB | ARM (per frame) | FPGA |
| Existing window | `0x3A000000` | unchanged | both | both |
| Output framebuffer BUF0/BUF1 | `0x3A000040` / `0x3A040040` | unchanged | **FPGA (was ARM)** | FPGA scanout |

The sprite arena is a **relocation, not a duplication** — the 46,001 KB working set is
already heap-resident today, so total RAM use is unchanged (Phase 0 finding 2). The ARM
reaches it by `mmap`ing `/dev/mem` exactly as `native_video_writer.c` already does for
`0x3A000000`, and runs a bump allocator inside it. If the pool is exhausted or the mapping
fails, sprite allocation falls back to `malloc` and those sprites are marked CPU-only —
a defense-in-depth gate, not an error path.

## 7. Frame header and display-list encoding

### 7.1 Frame header (one per frame, in the ring)

```
  qword 0: [63:32] magic 'TB01'          [31:16] n_bands      [15:0] band_height (src lines)
  qword 1: [63:48] src_w  [47:32] src_h  [31:16] out_w        [15:0] out_h
  qword 2: [63:32] sequence number       [31:0]  total_commands
  qword 3: [63:0]  reserved
```

`sequence number` replaces the current frame counter for staleness detection (section 10).

### 7.2 Command word (32 bytes = 4 DDR3 beats, naturally aligned)

```
  qword 0: [63:56] opcode      SPRITE | LINEAR | FILL | END_BAND
           [55:52] blend_mode
           [51:48] flags       bit0 flipx, bit1 has_clip
           [47:32] dst_x       signed, in source-resolution space
           [31:16] dst_y       row within this band (0 .. band_height-1)
           [15:0]  n_rows      rows of this sprite falling in this band
  qword 1: [63:32] src_addr    absolute byte address of the FIRST row's data (pre-seeked)
           [31:16] src_w       sprite width (needed for flipx and edge clipping)
           [15:0]  reserved
  qword 2: [63:32] pal_addr    absolute byte address of the 256-entry RGB565 palette
           [31:0]  src_stride  LINEAR only (bytes per row); ignored for SPRITE
  qword 3: [63:48] clip_x0 [47:32] clip_x1 [31:16] clip_y0 [15:0] clip_y1
```

Opcodes:

- `SPRITE` — RLE stream, 8-bit indices through `pal_addr`.
- `LINEAR` — raw RGB565 rows with a colour key. Used for the background blit **and** for
  CPU-rasterised fallback sprites. This is the command that makes z-order exact.
- `FILL` — solid rectangle (cheap, covers a few engine cases).
- `END_BAND` — terminates the band's list.

Volume: ~93 sprites/frame x ~4 bands each = ~372 commands x 32 B = **~12 KB/frame =
0.7 MB/s**. Negligible against the 787.5 MB/s port.

### 7.3 Who bins into bands: the ARM

The ARM already knows each sprite's y extent, and duplicating a command per band is far
cheaper than making the FPGA re-read one global list 16 times. So the ARM emits **one
list per band**, with the per-band `src_addr` already seeked and `n_rows` already clipped.
The FPGA walks band 0's list to `END_BAND`, then band 1's, sequentially. No indirection,
no random access, no `linetab` walk in hardware.

## 8. Band geometry — **a band is a whole number of OUTPUT rows**

> **This section was rewritten after checking the first draft against the full 450-PAK
> resolution census (`pak_dimension_census.md`). The first rule was wrong for 8 PAKs and
> impossible for 3.** Recorded here deliberately: the census is what caught it, and the
> lesson is the same one that closed the collision grid and putscreen — price a design
> against the real distribution, not against the one example in front of you.

### 8.1 The rule that does NOT work

The shipped downscaler derives each output row's source span as

```
  yy0 = (y     * src_h) / out_h
  yy1 = ((y+1) * src_h) / out_h        vcnt = yy1 - yy0
```

The first draft required a band's *source-line count* `B` to be a multiple of
`src_h / gcd(src_h, 224)`. Against the census that is unusable:

| PAK | `src_h` | minimum legal band under the bad rule | |
|---|---:|---:|---|
| Dragon Ball Z Tournament (960x475) | 475 | **475 lines = the entire frame** | impossible |
| Mortal Kombat Outworld Assassins (432x243) | 243 | **243 lines = the entire frame** | impossible |
| Xelam (500x650) | 650 | 325 lines (162,500 px) | impossible |
| Lust Rush (1600x900) | 900 | 225 lines (360,000 px) | impossible |
| 960x540 (4 PAKs) | 540 | 135 lines (129,600 px) | absurd |
| Gunman (400x300) | 300 | 75 lines (30,000 px) | wasteful |

**8 PAKs broken, 3 of them fatally.** Heights 475 and 243 are coprime with 224, so
`gcd = 1` and the "minimum band" is the whole frame.

### 8.2 The rule that does work

Invert it. **A band is a whole number of OUTPUT rows; the ARM derives the source range**
using the same floor formula the downscaler already uses:

```
  band k covers output rows [ k*R, (k+1)*R )
      src_y0 = floor( k*R     * src_h / out_h )
      src_y1 = floor( (k+1)*R * src_h / out_h )
```

Exact by construction. No divisibility constraint, no gcd, no alignment. Source lines per
band vary by +/-1, which is fine because the FPGA already takes the band's source-line
count as a per-band parameter.

`R` is chosen per PAK at load time as the largest value whose worst-case band fits the
band buffer, which is sized in **pixels**, not lines:

```
  BAND_BUDGET_PX = 32,768        (64 KB, 51 M10K single, 102 M10K double = 21% of the 486 free)
  R = max { r : max_k [ floor((k+1)*r*H/224) - floor(k*r*H/224) ] * W  <=  BAND_BUDGET_PX }
```

Verified across every distinct resolution in the census:

| PAK native | R (output rows/band) | src lines | band px | bands/frame |
|---|---:|---:|---:|---:|
| 1600x900 (Lust Rush) | 4 | 17 | 27,200 | 56 |
| 960x540 | 14 | 34 | 32,640 | 16 |
| **960x480 (He-Man)** | **15** | **33** | **31,680** | **15** |
| 960x475 (DBZ Tournament) | 16 | 34 | 32,640 | 14 |
| 800x480 | 18 | 39 | 31,200 | 13 |
| 720x480 | 21 | 45 | 32,400 | 11 |
| 640x640 | 17 | 49 | 31,360 | 14 |
| 640x480 (46 PAKs) | 23 | 50 | 32,000 | 10 |
| 640x360 (Bearz) | 31 | 50 | 32,000 | 8 |
| 500x650 (Xelam) | 22 | 64 | 32,000 | 11 |
| 480x360 | 42 | 68 | 32,640 | 6 |
| 480x272 (94 PAKs) | 56 | 68 | 32,640 | 4 |
| 432x243 (MK Outworld) | 69 | 75 | 32,400 | 4 |
| 400x300 (Gunman) | 60 | 81 | 32,400 | 4 |
| 384x224 | 85 | 85 | 32,640 | 3 |
| 336x240 | 90 | 96 | 32,256 | 3 |
| 320x240 (286 PAKs) | 95 | 102 | 32,640 | 3 |
| 256x224 | 128 | 128 | 32,768 | 2 |
| 240x224 | 136 | 136 | 32,640 | 2 |
| 240x200 | 153 | 136 | 32,640 | 2 |

**All 450 PAKs fit, including the 1600-wide outlier** — a wide PAK simply gets fewer output
rows per band. That also closes the "width > 960" open item: no capability gate is needed
for width, because the budget is in pixels.

Double buffering (102 M10K) lets the compositor fill band N+1 while the downscaler drains
band N.

### 8.3 Consequence for the downscaler
Because every band ends on an output-row boundary, the vertical box accumulator never has
to carry across a band boundary. `vcnt` still varies per output row (2 or 3 for He-Man) and
comes from the same floor formula.

## 9. Functional blocks

### 9.1 Band walker
Reads the frame header, then streams command words for the current band, dispatching each
to the fetch engine. Maintains the current palette address and issues a palette reload only
when it changes (section 9.3).

### 9.2 RLE fetch engine
A byte-stream decoder over a DDR3 read burst, implementing exactly the format in section 5:

```
  state SKIP : read clearcount
               0xFF -> row done; if last row -> command done; else advance dst row
               else -> dst_x += clearcount (signed step for flipx); goto VIS
  state VIS  : read viscount
               0 -> goto SKIP
               n -> stream n index bytes through the palette LUT into the band buffer
```

Horizontal clipping is applied per run: runs entirely outside `[clip_x0, clip_x1]` are
skipped without writing; runs crossing an edge are partially written. Vertical clipping is
already handled by the ARM via `dst_y` and `n_rows`.

### 9.3 Palette RAM
One 256-entry x 16-bit on-chip RAM, reloaded when `pal_addr` changes (512 B = 64 beats).
Worst realistic case ~10 distinct palettes per band x 16 bands x 64 beats = 10,240 beats
per frame = **0.6% of the clock budget** and 4.9 MB/s. Reload-on-change is therefore
sufficient; a 2-4 entry palette cache is the escape hatch if measurement disagrees.

### 9.4 Blend unit
RGB565 in, RGB565 out. Unpack 5/6/5, blend, repack.

**Scope is deliberately NOT frozen in this document.** The engine's 16-bit build has a set
of LUT-accelerated blend modes plus alpha levels, and implementing all of them in RTL
before knowing which ones He-Man actually uses would be building on an assumption. The
existing `[BLD]`/`[BAL]`/`[A15]` probes already produce a blend-mode and alpha-level
histogram. **Phase 1b: run them on He-Man + Avengers + PDC2 and implement only the modes
that appear**; everything else routes to the CPU fallback via the `LINEAR` path. Cost
estimate for the common alpha blend: 3 multipliers per pixel per operand, so ~12 DSPs at
2 px/clock, against 77 free.

### 9.5 Box downscaler
Consumes a completed band, emits its output rows, and writes them to the output
framebuffer. Must reproduce the shipped arithmetic **byte-for-byte**:

- `hcnt = src_w / out_w` taps horizontally, `vcnt = yy1 - yy0` vertically (varies per row,
  2 or 3 for He-Man).
- Each 5/6-bit field expanded to 8 bits exactly as shipped: `R5,B5 -> (v<<3)|(v>>2)`,
  `G6 -> (v<<2)|(v>>4)`.
- Sum the `hcnt x vcnt` block, then a single divide by `hcnt*vcnt` via the shipped
  reciprocal form `rc = (1<<20) / (hcnt*vcnt)`.
- BGR565 -> RGB565 channel swap on the way out.

The exact expansion, reciprocal and packing must be **lifted verbatim from
`src/native_video_writer.c`**, not re-derived, and proven with a `[DCV16]`-style
byte-identity probe (section 12).

### 9.6 CPU fallback path
`gfx_draw_scale` and every other non-fast-path sprite rasterises into the scratch region as
RGB565 + colour key, and the ARM emits a `LINEAR` command in the sprite's correct z-slot.
The CPU keeps only the rasterisation cost — which is precisely the 28.8% already accounted
for in the payoff arithmetic (section 15).

## 10. Ownership and sync protocol

| | today | after Tier-B |
|---|---|---|
| framebuffer writer | ARM | **FPGA** |
| framebuffer reader | FPGA scanout | FPGA scanout |
| ARM publishes | frame counter + active buffer | **display-list sequence number** |
| staleness detection | 30 vblanks without a frame-counter change -> blank | 30 vblanks without a **sequence number** change -> blank |
| keepalive thread | bumps the frame counter every 150 ms | bumps the **sequence number** every 150 ms |

The pipeline is: the ARM builds the list for frame N while the FPGA composites frame N-1.
That is **+1 frame of latency (16.7 ms)** by construction and must be stated in the
release notes. It is not avoidable without making the ARM wait on the FPGA, which would
reintroduce exactly the stall Tier-B exists to remove.

The `NativeVideoWriter_KeepaliveTick()` single-source-of-truth rule still applies: the
keepalive and the list publisher must share the sequence-number state, not keep separate
counters. Two counters racing on one control word is the loading-bar-jitter bug class.

## 11. New CDC paths

| crossing | direction | mechanism |
|---|---|---|
| `ddr_clk` (98.4375 MHz) <-> band buffer | same domain | none needed |
| band-complete pulse -> downscaler | same domain | none needed |
| optional faster blitter clock -> `ddr_clk` | fast -> fast | dual-clock FIFO |
| sequence number (ARM/DDR3) -> `clk_vid` (53.693 MHz) | **fast -> slow** | **2-FF sync with a pulse widened to >= 5 `ddr_clk` cycles** |

That last row is the Option Y concern-F trap verbatim: a 1-cycle pulse at 98 MHz crossing
into 53 MHz is not reliably captured by a 2-FF synchroniser. Widen at the source.

If a faster blitter clock is used, it comes from a **4th output counter on the existing
PLL** (Cyclone V PLLs have 9; `rtl/pll.v` instantiates 3), so it does **not** consume the
3/6 PLL budget — but it does add a clock domain, a CDC and a new `set_clock_groups` entry
in `OpenBOR.sdc`.

## 12. Diagnostic instrumentation (TEMPORARY DIAG, from day one)

Built in from the first RTL commit, all marked so the CI gate blocks the binary:

1. **VGA-colour state visualiser** — the cheapest FPGA debug tool we have; route band
   walker state / fetch-engine state / band-complete to `VGA_R/G/B` as 1 bit each. This is
   what localised the Option Y H-pass deadlock in 4 cycles after 7 failed deploys.
2. **`[DCV16]`-style byte-identity probe** — for the first N frames, the ARM ALSO computes
   the shipped NEON downscale and compares it to what the FPGA wrote. **Mismatches must be
   0.** This is the acceptance test for section 9.5.
3. **DDR3 ring-buffer probe** — 256 samples x 64 bits in M10K, sampling band index, command
   index, fetch state and beat counters, for anything the visualiser cannot resolve.
4. **Per-band cycle counters** — read back by the ARM to confirm the >= 2 px/clock budget
   holds on real content rather than on paper.

## 13. Implementation phases

| phase | content | gate |
|---|---|---|
| 1 | this document | user approval |
| 1b | blend-mode + alpha histogram on He-Man / Avengers / PDC2 via `[BLD]`/`[BAL]`/`[A15]`; freeze blend-unit scope | measurement, not opinion |
| 2 | ARM side only: sprite arena allocator, display-list builder, per-band binning, fallback rasteriser. **Shipped path unchanged** — the list is built and discarded. | list contents validated offline against the CPU's own draw order |
| 3 | RTL: band walker + RLE fetch engine + palette RAM + band buffer. No blend, no downscale. Opaque sprites only. | VGA visualiser shows correct band traversal |
| 4 | RTL: blend unit + box downscaler + output write. | `[DCV16]` mismatch = 0 |
| 5 | Ownership flip: FPGA writes the framebuffer, sequence-number protocol, keepalive rework. | singleton-state matrix clean, no black screens |
| 6 | Fallback path + capability gate + all-PAK regression | ATOV + TMNT-RP + modern PAK palette trio verified |
| 7 | Audit cycles until zero bugs AND zero concerns | section 14 |
| 8 | Hardware verification, then ship | timing >= +0.3 ns preferred |

Phases 2 and 3 are independently testable and neither changes what users receive.

## 14. Risks and open items

### 14.1 TOP RISK — `pll_hdmi` ships at +0.128 ns
That is the tightest path on the device (`output_files/OpenBOR.sta.summary`), and Tier-B is
a large new block of `clk_sys`-domain logic. The Option Y precedent: adding `clk_sys` logic
cost **0.273 ns** of `pll_hdmi` margin — more than enough to take +0.128 negative.
Mitigation ladder, in order:

1. SEED lottery with **non-adjacent** values (10 -> 20 -> 30 -> 50, not 11/12/13).
2. The structural fix: add `pll_hdmi` as a 4th asynchronous clock group in `OpenBOR.sdc`.
   Valid because no user RTL crosses into that domain; the framework handles its own HDMI
   CDC internally.

**Budget for this from the start.** Do not discover it at the end of the arc.

### 14.2 Open items requiring resolution before Phase 2
- **Reserved-region base and size must be VERIFIED**, not assumed (section 6).
- **Blend-mode scope must be MEASURED**, not guessed (section 9.4, Phase 1b).
- ~~Band height per PAK~~ -> **CLOSED by the census** (section 8): a band is a whole number
  of OUTPUT rows, `R` chosen per PAK from a pixel budget. Verified against all 20 distinct
  resolutions in `pak_dimension_census.md`.
- ~~Widths above 960~~ -> **CLOSED by the census**: max width in the library is 1600 (Lust
  Rush), and because the band budget is in PIXELS a wide PAK simply gets fewer output rows
  per band (R=4, 56 bands/frame). No width capability gate needed.
- **Sprite arena exhaustion** behaviour: fall back to `malloc` + CPU-only marking. Needs a
  measured headroom figure (46,001 KB observed on He-Man; is any PAK larger? Note Lust Rush
  is 3.12x He-Man's pixel area, so it is the obvious candidate to measure).

### 14.2b 🛑 THE BANDWIDTH BUDGET WAS SIZED FROM ONE PAK -- Lust Rush is the outlier
Phase 0b's headline "133 MB/s = 31% of the conservative ceiling" was computed from He-Man's
MEASURED sprite coverage (1.16 Mpx/frame = 2.52x its screen). Scaling that overdraw ratio by
area across the census gives:

| PAK / group | screen px | bg | sprites | out | TOTAL MB/s | |
|---|---:|---:|---:|---:|---:|---|
| **Lust Rush 1600x900** | 1,440,000 | 172.6 | 217.4 | 8.6 | **398.6** | 🛑 **92% of the 433 conservative ceiling** |
| 960x540 (4 PAKs) | 518,400 | 62.1 | 78.3 | 8.6 | 149.0 | ok |
| **He-Man 960x480** | 460,800 | 55.2 | 69.6 | 8.6 | **133.4** | ok (the measured anchor) |
| 640x480 (46 PAKs) | 307,200 | 36.8 | 46.4 | 8.6 | 91.8 | ok |
| 480x272 (97 PAKs) | 130,560 | 15.6 | 19.7 | 8.6 | 44.0 | ok |
| 320x240 (283 PAKs) | 76,800 | 9.2 | 11.6 | 8.6 | 29.4 | ok |

**Only Lust Rush is anywhere near the ceiling**, and it is a single PAK at 3.12x He-Man's
area. Everything else has 3x margin or better. Options, to settle in Phase 2: measure Lust
Rush's real overdraw (it may be far below He-Man's 2.52x -- a 1600x900 cart is unlikely to
also carry 2.5x overdraw); lean on the 591 MB/s good-burst figure rather than 433; or let
the capability gate route it to CPU. Do NOT treat 398.6 as measured -- only the He-Man row
is anchored in data; every other row scales one PAK's overdraw ratio by area.

### 14.3 Accepted consequences
- +1 frame of latency (section 10).
- Two rendering paths coexist permanently (FPGA fast path + CPU fallback), which is a
  maintenance cost accepted in exchange for never regressing an unsupported PAK.

## 14.4 NO BROKEN PAKs -- the all-450 no-regression contract (user directive)

**Requirement: all 450 PAKs must still work and play after Tier-B. No broken PAKs, no
regressions.** A hard gate, and it shapes the design, not just the testing.

### 14.4.1 The design property that makes it achievable
**The capability gate is CONSERVATIVE by construction: offload only what is provably
byte-identical, route everything else to the existing CPU path.** Then the worst case for
any PAK is *no speedup* -- never a regression. Every gate defaults to CPU on doubt:

| condition | action |
|---|---|
| blend mode outside 1..6 (incl. the 5 PAKs declaring alpha>6) | CPU |
| sprite needs scale / rotate / water / flipy / shiftx / fill | CPU (the measured 29%) |
| sprite working set exceeds the arena | CPU for that PAK (see 14.4.4) |
| PAK width exceeds the band buffer | CPU for that PAK |
| anything the software model has not proven identical | CPU |

### 14.4.2 The regression net ALREADY EXISTS -- 431 golden traces
`#Golden_Traces/OpenBOR_7533/` holds **one `.trace` per PAK for 431 of the 450**, each
`FRAME:VIDEOCRC:AUDIOCRC` over 120 presented frames from boot, **100% deterministic**
(synthetic clock via `OB_TEST`). Re-scan after a change and diff = an exact blast-radius
list. **Tier-B's required result is ZERO changed traces.**

Coverage by resolution: **every resolution is fully covered except 1600x900 (Lust Rush),
which has ZERO** -- plus small gaps (480x272 90/97, 320x240 274/283, 640x480 45/46,
960x540 3/4). 19 PAKs lack a trace, almost all the known script-compile-fail (ec=1) class.
Two engine crash bugs were fixed 2026-07-23, so **a re-scan on current main may recover
several**; do that before treating any as permanently uncovered.

### 14.4.3 The verification ladder (mechanical unless stated)
1. **Phase 2 (ARM-side): re-run the 431 traces, require ZERO diffs.** Phase 2 builds the
   display list and discards it, so by construction nothing may change -- this proves the
   new ARM code is side-effect-free.
2. **Software model of the FPGA compositor, run headless in place of the CPU compositor
   across all 431, require ZERO diffs.** The load-bearing rung: it validates band geometry,
   blend math, the downscaler, z-order and the capability gate over the WHOLE library
   before any RTL exists and without hardware.
3. **RTL verified against that model** (standard practice -- the model is the reference).
4. **On-device**: the locked palette trio (ATOV / TMNT-RP / a modern PAK) + **Bearz** (sole
   OVERLAY user at 14.7% of its pixels, and a mixed 1-and-2-tap vertical box) + He-Man +
   **Lust Rush** (the only uncovered resolution).
5. **The 19 without traces**: re-scan first, then on-device verify whatever still will not run.

### 14.4.4 ARENA SIZING WAS WRONG -- 64 MB is far too small
Measured across 435 PAKs (`pak_blend_runtime_2026-07-29.tsv`, `spritekb`):

```
  largest 150.5 MB (Dragon Ball Z Tournament, 848 sprites)  median 2.8 MB  mean 7.4 MB
  Sonic Super Jam 129.7 | Beat Em Up Ultimate Alliance 89.9 | Ogres Mayhem 79.8
  Mortal Kombat - The Chosen One 72.0 | DBZ Attack of Saiyans 63.8
```

| arena | PAKs that would NOT fit |
|---:|---|
| 64 MB (this doc's proposal) | **5** |
| 96 MB | 2 |
| 128 MB | 2 |
| 160 MB | 0 |

He-Man's 46 MB is **not** the worst case -- it is 3.3x smaller than the largest. The
reserved region below the existing window (`0x30000000..0x3A000000`) is ~160 MB, so 150.5 MB
only just fits. **Response: size the arena to whatever the region allows AND keep the
per-PAK fallback** -- a PAK whose working set does not fit takes the CPU path and is
slower, never broken. That satisfies the contract regardless of the final number.

### 14.4.5 THE BANDWIDTH SCARE WAS AN ARTEFACT -- resolution is not the driver
Section 14.2b estimated Lust Rush at 398.6 MB/s by scaling He-Man's overdraw by area.
**Measured** per-PAK overdraw (415 PAKs) shows that model is wrong:

```
  measured overdraw: max 38.56x   median 1.05x   mean 1.99x
  He-Man is 1.55x -- not the 2.52x the estimate assumed, so even the anchor was off
```

Recomputed with real overdraw, **NO PAK exceeds the 433 MB/s conservative ceiling**:

| MB/s | overdraw | res | PAK |
|---:|---:|---|---|
| **325.9** | **38.56x** | 480x272 | Ninja - Stealth Assassins |
| 248.2 | 5.71x | 960x540 | SoR4 Silent Storm [Demo] |
| 230.5 | 8.71x | 720x480 | Urban Lockdown |
| 189.9 | 21.17x | 480x272 | Masters of the Universe - Eternian Battle |
| 106.7 | 1.55x | 960x480 | He-Man |
| median 26.3 | | | |

**The worst case is a 480x272 PAK with 38x overdraw, not the 1600x900 one.** Bandwidth
tracks OVERDRAW, not screen size. Section 14.2b's table is SUPERSEDED by this one.

## 15. Success criteria

| criterion | target | source |
|---|---|---|
| He-Man sustained fps | **59.92 locked** | measured frames 20.0 ms -> 9.75 ms, 29.0 ms -> 13.21 ms |
| downscale byte-identity | `[DCV16]` mismatch **= 0** | section 12.2 |
| palette regression | ATOV + TMNT-RP + modern PAK all canonical | the locked-palette verification ritual |
| DDR3 bandwidth | <= 200 MB/s measured | budget says 133 MB/s |
| compositing rate | >= 2 px/clock | per-band cycle counters |
| timing | all clocks >= +0.1 ns, `pll_hdmi` >= +0.3 ns preferred | `OpenBOR.sta.summary` |
| audit | one full cycle reporting **zero bugs, zero concerns** | section 13 phase 7 |
| **no broken PAKs** | **ZERO changed golden traces across all 431** | section 14.4 |
| fallback | every unsupported PAK renders exactly as it does today | phase 6 regression |

## 16. Rollback

Everything shipped today is committed on `main` and captured by the tag:

```
  git checkout pre-tierb          # or: git checkout pre-tierb-stable
```

Device-side restore is the RBF back into `_Other/` plus an atomic-rename of the ARM binary
`e5e6a8219f3994d7be6743d83c0c5999`. The shipped artifacts additionally exist in `db.json`,
in the per-core `latest` release asset, and on the dev MiSTer — four independent copies.

---

## Approval needed before Phase 1b / Phase 2

1. Confirm the phase ordering in section 13 (ARM-side first, RTL second).
2. Confirm the accepted +1 frame of latency (section 10).
3. Approve running Phase 1b (blend histogram) as the next concrete step.
