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
- **The ARM reads back composited pixels on SEVEN known paths** (pause, main-menu return,
  screenshot, fade, debug overlay, and a second compositing path in `update_loading`).
  Those frames run in **CPU-COMPOSITE mode** -- see section 9.10. The earlier claim that
  the ARM never reads back was FALSE and is what review finding C3 caught.
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
`0x3A000000`, and runs an allocator inside it (**with free/reuse -- NOT a bump allocator**; `model_cache[]` entries unload, see 14.4.4). If the pool is exhausted or the mapping
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
  qword 2: [63:32] pal_addr    absolute byte address of the EFFECTIVE 256-entry palette
                               (see 9.3 -- the ARM resolves the v3.10 discriminator; the
                               FPGA must NEVER pick between frame->palette and
                               drawmethod->table itself)
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

### 9.3 The EFFECTIVE palette -- the C5 resolution

🛑 **The palette a sprite renders through is NOT always its own.** The shipped 16-bit
dispatch (`apply_patches.py`, Path B B4) is the **LOCKED v3.10 dual-flag discriminator**:

```c
table_arg16 = (frame && frame->palette &&
               drawmethod->has_remap_directive &&
              !drawmethod->has_palette_directive)
            ? NULL                      /* -> putsprite uses frame->palette  */
            : (unsigned *)drawmethod->table;   /* -> model->palette          */
```

Three PAK archetypes, per `[[never-touch-openbor-legacy-palette-path]]`:

| archetype | `has_remap` | `has_palette` | renders through |
|---|---|---|---|
| ATOV (legacy `remap`) | 1 | 0 | **`frame->palette`** (the sprite's own) |
| TMNT-RP (`palette` + `remap`) | 1 | 1 | `drawmethod->table` |
| Cap / He-Man / PDC2 (modern) | 0 | 1 | `drawmethod->table` |

A command word carries **one** `pal_addr`. If the ARM emits `frame->palette` unconditionally,
every modern PAK renders through the wrong LUT; if it emits `drawmethod->table`
unconditionally, ATOV does. **Either way this re-introduces the palette regression that took
months to fix -- architecturally, in a path CLAUDE.md marks as LOCKED.**

**Resolution:** the ARM evaluates that exact expression when building the command and emits
the **resolved effective palette address**. The FPGA never sees the flags and never chooses.
Additionally:
- `frame->palette == NULL` and a non-NULL `frame->mask` both become **offload-gate
  conditions** (CPU fallback), since the fast path's palette selection is then undefined for
  our purposes.
- The `[DCV16]` byte-identity acceptance set **must** include ATOV + TMNT-RP + one modern
  PAK -- the exact trio the locked-path verification ritual already mandates.
- The 12 locked v3.10 patches are **not touched**; Tier-B only reads the flags they set.

### 9.4 Palette RAM
One 256-entry x 16-bit on-chip RAM, reloaded when `pal_addr` changes (512 B = 64 beats).
Worst realistic case ~10 distinct palettes per band x 16 bands x 64 beats = 10,240 beats
per frame = **0.6% of the clock budget** and 4.9 MB/s. Reload-on-change is therefore
sufficient; a 2-4 entry palette cache is the escape hatch if measurement disagrees.

### 9.5 Blend unit -- the ship build uses LUTs, so the FPGA must too (M3, M2, M16)

**M3: every `blend_*16` has TWO paths and the ship build takes the LUT one.**
```c
unsigned short blend_screen16(unsigned short c1, unsigned short c2) {
    unsigned char *tbl;
    if((tbl = blendtables[BLEND_SCREEN])) return _color16(tbl[_ri], tbl[_gi], tbl[_bi]);
    return _color16(_screen16(...), _screen16(...), _screen16(...));   /* arithmetic */
}
```
`apply_patches.py` populates five of them: SCREEN, MULTIPLY, OVERLAY, HARDLIGHT, DODGE.
`tables[BLEND_HALF] = NULL`, so HALF alone takes the arithmetic path. Specifying "unpack
5/6/5, blend, repack" would have reproduced the path the ship build **does not run**.

**This makes the blend unit simpler and bit-exact by construction.** The LUT indices are
```
  _ri = (r1<<5)|r2            0..1023      (5-bit x 5-bit)
  _bi = (b1<<5)|b2            0..1023      shares the same range as _ri
  _gi = ((g1<<6)|g2) + 1024   1024..5119   (6-bit x 6-bit)
```
so one mode's table is **5,120 bytes**, and all five are **25 KB = ~20 M10K** -- trivially
on-chip. The FPGA does a table lookup per channel and is byte-identical to the CPU with
**no divides and no DSPs**. HALF is a plain average, a shift and an add.

🛑 **Operand order is load-bearing.** OVERLAY, HARDLIGHT and DODGE are **not commutative**;
the call is `blendfp(src, dest)` -- source high, destination low. Swapping them silently
changes every blended pixel.

**M2: `alpha` alone does NOT determine the blend function.** `getblendfunction16` also reads
two globals: `tintmode > 0` replaces *any* mode with `blend_tint16` (a composition of two
modes over `tintcolor`), and `usechannel` turns mode 6 into `blend_rgbchannel16`. Both are
script-mutable per sprite, and they are mutually exclusive (`if` / `else if`). The gate
"mode outside 1..6 -> CPU" would have passed a tinted sprite to the FPGA to be rendered with
the plain mode -- **a gate that offloads something not identical**, precisely the class
14.4.1 claims is impossible. **Fix: `tintmode > 0` and (`alpha == 6 && usechannel`) are both
offload-gate conditions -> CPU.** The ARM evaluates them when building the command, exactly
as it resolves the palette (9.3).

**M16: the blend scope is measured over the library, not three PAKs.** Freezing scope from
He-Man/Avengers/PDC2 is the one-example fallacy this document's own lesson table forbids and
that the census has already caught twice. Because all five LUTs cost only ~20 M10K total,
**the scope question mostly dissolves: implement all six.** Phase 1b's histogram now serves
to prioritise verification order, not to decide what exists.

### 9.6 Downscaler -- FIVE shipped variants, not one box (the C4 resolution)

The first draft said `hcnt = src_w / out_w`. **That is wrong for 394 of 450 PAKs.**
`src/native_video_writer.c` dispatches the 16bpp path on width into **five** distinct
implementations, and byte-identity requires reproducing whichever one a PAK actually hits:

| # | condition (as written in the source) | what it does | PAKs |
|---|---|---|---:|
| 1 | `width == 320 && ((uintptr_t)src_row & 15) == 0` | 🛑 **NO BOX AT ALL** -- NEON BGR->RGB swap only, row chosen by `src_y = (y * sy256) / 256` with `sy256 = (height*256)/224`, a **doubly-truncated NN** that is NOT `floor(y*H/224)` | **282** |
| 2 | `width == NV_FRAME_WIDTH * 3` (960) | 3x horizontal box, `hcnt == 3` | 7 |
| 3 | `width == NV_FRAME_WIDTH * 2` (640) | 2x horizontal box, `hcnt == 2` | 50 |
| 4 | `width * 2 == NV_FRAME_WIDTH * 3` (480) | 3:2 box with **per-parity** hcnt -- even dest column `hcnt=1`, odd `hcnt=2`, and **two reciprocals** `rc_e`/`rc_o` | **99** |
| 5 | else | scalar general box, **per-column** `hcnt = x1 - x0` from `x0=(x*W)/320`, `x1=((x+1)*W)/320`, clamped `if (hcnt > 7) hcnt = 7`, via `recip16[hcnt]` | 12 |

Variant 5's widths: 1600, 800, 720, 500, 432, 400, 384, 336, and **256/240 which are NARROWER
than 320** -- there `x1 <= x0` clamps to `x0+1`, giving `hcnt = 1` and **column replication,
i.e. an UPSCALE**. The design never mentioned upscaling at all.

### The exact arithmetic (variants 2-5)
```
  yy0 = (y*H)/224 ; yy1 = ((y+1)*H)/224
  if (yy1 <= yy0) yy1 = yy0+1 ;  if (yy1 > H) yy1 = H ;  if (yy0 >= H) yy0 = H-1
  vcnt = yy1 - yy0
  5/6-bit -> 8-bit:  R5,B5 -> (v<<3)|(v>>2)      G6 -> (v<<2)|(v>>4)
  rc  = (1<<20) / (hcnt*vcnt)                    <-- TRUNCATED reciprocal
  out = (sum * rc + (1<<19)) >> 20               <-- note the +(1<<19) rounding term
```
The `+ (1<<19)` and the truncated reciprocal together are **not** `round(sum/n)` and differ
from it for many `(sum, n)`. The 16bpp path does **not** clamp to 255; the 32bpp path does.

### Consequences for the design
1. **Variant 1 covers 282 PAKs (63%) and is not a box.** An FPGA that box-filters them
   changes every pixel of the majority of the library. It must reproduce the NN row pick,
   including the double truncation.
2. **Variant 1 is gated on ARM pointer alignment** (`src_row & 15`), which the FPGA cannot
   observe. An unaligned source silently falls through to variant 5. The ARM must therefore
   **decide the variant and encode it in the frame header** -- the FPGA must never infer it
   from width.
3. **The frame header gains a `downscale_variant` field**, and "output-path variant" joins
   the capability gate: any variant the RTL does not implement bit-exactly routes the whole
   frame to CPU-COMPOSITE mode (section 9.10).
4. Recommended build order: variant 1 first (63% of PAKs, and the simplest -- no box), then
   2 and 3 (57 PAKs, constant hcnt), then 4 (99 PAKs), then 5 last (12 PAKs, and the only
   one needing a per-column divide and an upscale case).

### 9.7 CPU fallback path
`gfx_draw_scale` and every other non-fast-path sprite rasterises into the scratch region as
RGB565 + colour key, and the ARM emits a `LINEAR` command in the sprite's correct z-slot.
The CPU keeps only the rasterisation cost — which is precisely the 28.8% already accounted
for in the payoff arithmetic (section 15).

### 9.8 DDR3 port ownership and arbitration -- the C1 resolution

There is exactly ONE port available to the core (`ram1`, 64-bit @ 98.4375 MHz); `ram2` and
`vbuf` are framework-owned. Today `OpenBOR.sv:492-497` drives it through a **static mux**
(`use_nv ? nv_* : old_*`) because only one master is ever live. Tier-B adds a second, and the
existing reader **cannot tolerate that**:

```verilog
// openbor_video_reader.sv:345
if (state == ST_WAIT_LINE && ddr_dout_ready) begin
    fifo_wr <= 1'b1;  fifo_wr_data <= ddr_dout;  beat_count <= beat_count + 7'd1;
```

Avalon read returns carry **no ID**. Any compositor read in flight while the reader sits in
`ST_WAIT_LINE` is latched into the line FIFO as pixel data and desyncs `beat_count` from the
80-beat burst -- a corrupt scanline plus a permanently mis-filled FIFO.

### The arbiter
A **burst-granular, strict-priority** arbiter replaces the static mux.

- **Only one master may have a burst in flight.** Grant is asserted when a requester's
  `rd`/`we` is taken and held until that burst's beats have all returned (reads) or been
  accepted (writes). Because Avalon returns are in-order per port, exclusive-per-burst
  granting makes every return unambiguously the grantee's.
- **`ddr_dout_ready` is gated per grantee** -- a non-granted master never sees a beat, so
  `reader.sv:345` stays correct unmodified.
- **Strict priority: video reader > compositor.** The reader must land 80 qwords per
  scanline; a scanline is 63.7 us and a full 80-beat burst is ~0.81 us at 98.4375 MHz, i.e.
  **~1.3% of a scanline**. The margin is enormous.
- **Non-preemptible, so cap the compositor's burst.** Worst-case reader wait is one
  compositor burst. Capping the compositor at 64 beats bounds that at **~0.65 us**, ~1% of
  a scanline -- still negligible against the reader's existing `TIMEOUT_MAX` guard.

### What this costs
Two extra states and a beat counter per requester (~100 ALM), plus the grant mux. No new
clock domain: everything is already `ddr_clk`. It must be **written and verified before any
compositor RTL**, because without it the very first co-existing burst corrupts video.

### 9.9 The arena is WRITE-ONLY from the ARM -- the C2 resolution

Review finding C2 asked whether the CPU could still read sprite RLE once it lived in an
uncached DDR3 arena. **Measured on the A9** (`tools/uncached_bench.c`, 256 KB buffer,
best of 24, pinned to core 0):

| pattern | cached | uncached | slowdown |
|---|---:|---:|---:|
| sequential byte scan | 1.163 ms | 35.787 ms | **30.8x** |
| **RLE decode walk** (what `gfx_draw_scale` does) | 1.020 ms | 22.609 ms | **22.2x** |
| linetab row seeks | 0.152 ms | 1.993 ms | 13.1x |

**The original arena design is dead.** He-Man's CPU fallback is ~28.8% of putsprite time
(1.9-5.0 ms); making its RLE reads 22x slower blows the 16.7 ms frame budget on its own.

#### 9.9.1 An independent, harder constraint: unaligned access FAULTS
The same run probed what a `/dev/mem` + `O_SYNC` mapping even permits:

```
  byte store, any offset             OK        u32 store, 4-byte aligned      OK
  u32 store, UNALIGNED (+1)          SIGBUS    memset 16 B, aligned           OK
  memset 16 B, UNALIGNED (+1)        SIGBUS    memcpy 16 B, UNALIGNED (+1)    SIGBUS
  byte loop 16 B, unaligned          OK
```

That mapping is **strongly-ordered (device) memory**, where unaligned multi-byte access
faults. Consequences, both verified the hard way in this bench:
- **`encodesprite` cannot write into the arena.** It builds every sprite with
  `memcpy(data, src + x0, x - x0)` at arbitrary byte offsets.
- **A hand-written byte loop is not sufficient either** -- at `-O2 -mfpu=neon` GCC
  vectorises it into a wide unaligned store that faults. Any arena writer must keep its
  pointers `volatile` or use an explicitly alignment-safe routine.

#### 9.9.2 The resolution: the CPU NEVER reads the arena
Sprites keep their normal cached heap allocation exactly as today. The arena receives a
**separate copy of only the fast-path-eligible sprites**, written once at load time.

- **ARM -> arena: write-only**, at PAK load, through an alignment-safe copy.
- **FPGA -> arena: read-only.**
- **The CPU fallback (`gfx_draw_scale` and the other three destinations) reads the
  ORDINARY CACHED sprite** and is therefore completely unaffected -- the 22x never applies
  to anything on the critical path.

#### 9.9.3 🛑 This kills "relocation, not duplication"
Phase 0 finding 2 claimed the arena costs no extra RAM because the working set was merely
moving. **That is now false: the arena is a DUPLICATE.** Revised cost, using the measured
71.2%-by-time fast-path share as a proxy for the eligible subset:

| | measured working set | arena duplicate (approx.) |
|---|---:|---:|
| median PAK | 2.8 MB | ~2 MB |
| He-Man | 33.5 MB headless / 46.0 MB on-device | ~24-33 MB |
| largest observed (DBZ Tournament) | 150.5 MB | ~107 MB |

Combined with 14.4.4's per-sprite fallback and free/reuse allocator, an arena that cannot
hold a PAK's eligible set simply offloads fewer sprites. Still degrades, never breaks.

#### 9.9.4 Open items this creates
1. **Arena WRITE cost is unmeasured.** Population is one-time per PAK load and writes are
   far cheaper than reads (write-combining), but a 150 MB PAK writing byte-wise into
   strongly-ordered memory could add real seconds to load. Measure before Phase 2.
2. **Does a sprite ever need both copies simultaneously?** A sprite drawn through the fast
   path in one frame and scaled in the next needs the cached copy anyway (it always has
   one), so the answer is no -- but the eligibility test must be per-BLIT, not per-sprite,
   and the arena copy is then a pure cache.
3. **`/proc/iomem` settles the region question** (finding N14/M11 were right that the
   `ddram.sv` citation was wrong): System RAM is `00000000-1fefffff` -- **~511 MB** -- so
   everything above `0x1FF00000` is outside Linux's map, roughly **513 MB** of reserved
   space, not the 256 MB the doc assumed. `devmem` read/write at `0x3A080000` works and
   `CONFIG_STRICT_DEVMEM` is not set. The arena has far more room than assumed; the
   proposed `0x30000000` base sits comfortably inside the reserved area.

### 9.10 CPU-COMPOSITE frame mode -- the C3 resolution

Tier-B's first draft asserted the ARM never reads the composited frame. **That is false.**
Enumerated exhaustively from pristine v7533 `openbor.c` (every read of `vscreen` as a
source, plus every `copyscreen`/`putscreen` touching it):

| site | function | operation | why it reads the composited frame |
|---|---|---|---|
| `openbor.c:21659` | `pausemenu()` | `copyscreen(pausebuffer, vscreen)` | snapshots the live frame to draw the menu over. **Our `pausemenu_patch.c:70` does the same** |
| `openbor.c:21619` | `backto_mainmenu()` | `copyscreen(pausebuffer, vscreen)` | same snapshot on menu return |
| `openbor.c:45856` | `update()` | `screenshot(vscreen, getpal, 1)` | user + script-triggered capture (`openborscript.c:12148`) |
| `openbor.c:45940` | `fade_out()` | `copyscreen(fbuffer, vscreen)` | captures the frame to fade FROM |
| `openbor.c:45945` | `fade_out()` | `putscreen(vscreen, fbuffer, &dm)` | writes the faded result BACK |
| `openbor.c:45869-45870` | `update()` | `screen_printf(vscreen, ...)` | draws debug text ONTO the composited frame |
| `openbor.c:22651-22667` | `update_loading()` | `putscreen` + `spriteq_draw` + `video_copy_screen` | 🛑 **a SECOND compositing + present path** the design never accounted for |

### The mechanism
A per-frame **`CPU_COMPOSITE`** flag in the frame header. When set, the ARM composites into
`vscreen` and writes the output framebuffer itself exactly as it does today, and the FPGA
**does not composite** -- it only scans out. When clear, the FPGA owns the frame.

This is cheap because **not one of these paths is performance-critical**: pause and the
main-menu return are static screens, fades are brief, screenshots are one-shot, the debug
overlay is developer-only, and the loading bar is already I/O-bound.

### Entry triggers (the ARM must set the flag BEFORE the frame it needs)
1. **Pause entry / main-menu return** -- one CPU-composited frame before the snapshot. The
   game state is frozen, so re-compositing it yields the identical image; the whole time the
   menu is up stays in CPU mode (it composites into `pausebuffer`, not `vscreen`).
2. **`fade_out()` / fade-in** -- CPU mode for the duration of the fade loop.
3. **Screenshot** -- one CPU frame, then capture.
4. **Debug overlay enabled** -- CPU mode for as long as it is on, since `screen_printf`
   writes onto the composited frame.
5. **`update_loading()`** -- CPU mode throughout. This path composites and presents on its
   own, outside the main `update()` flow.

### Consequences that must be designed, not assumed
- **Mode changes must be atomic with the frame.** A frame that is half CPU-composited and
  half FPGA-composited is a torn frame. The flag rides in the header alongside the sequence
  number and is subject to the same publish-ordering rule (finding M5).
- **The FPGA must quiesce cleanly** on entering CPU mode -- finish or abandon the band in
  flight, and not write the framebuffer the ARM is now writing. This is the same quiesce
  protocol finding C8 requires for PAK load / hot-swap / reset.
- **`vscreen` must still exist and still be composited-into on CPU frames**, so the 16-bit
  vscreen allocation and the whole CPU compositing path stay in the binary. Tier-B does not
  remove them; it bypasses them on gameplay frames only.
- **This is a THIRD gate condition** beyond the per-sprite and per-PAK gates: a per-FRAME
  gate. Section 14.4.1's table is extended accordingly.

### Verification
Every one of the seven paths gets an explicit on-device check in the Phase 6 regression set
(pause, menu return, screenshot, fade in/out, debug overlay, loading bar), because a black
pause snapshot is exactly the class of bug that shipped before -- CLAUDE.md records it for
the PIXEL_32 pausebuffer, and `pausemenu_patch.c:66-67` carries the comment explaining that
`copyscreen` early-returns on a format mismatch.

### 9.11 Frame-ready handshake -- the C6 resolution

The draft had the ARM's display-list sequence number drive the reader. It cannot: the reader
treats a counter change as **"a fully written framebuffer exists"**, so a bump that only means
"a list is ready" would start scanout of a buffer the compositor has not written yet.

```verilog
// openbor_video_reader.sv ST_CHECK_CTRL -- the EXISTING, PROVEN contract
else if (ctrl_word[31:2] != prev_frame_counter) begin
    prev_frame_counter <= ctrl_word[31:2];
    active_buffer      <= ctrl_word[0];
    buf_base_addr      <= ctrl_word[0] ? BUF1_ADDR : BUF0_ADDR;
    ...  state <= ST_READ_LINE;                       // starts scanning out NOW
end
```

**Resolution: do not change the reader at all -- change WHO writes `ctrl_word`.**

| | writes the framebuffer | writes `ctrl_word` (counter + buffer bit) |
|---|---|---|
| today | ARM | ARM |
| Tier-B, FPGA frame | **compositor** | **compositor**, on completing the frame |
| Tier-B, CPU_COMPOSITE frame (9.10) | ARM | ARM, exactly as today |

The **display-list sequence number lives in a SEPARATE compositor control word** and is never
seen by the reader. Exactly one `ctrl_word` writer exists at any instant, selected by the
frame mode. Cost: one 8-byte DDR3 write per frame.

Three things fall out for free:
- The reader, its 30-vblank staleness blank, and its double-buffer selection are **untouched
  and unre-verified** -- the highest-value property available here.
- **Finding M15 disappears.** The keepalive bumps `ctrl_word` (reader-facing), never the
  display-list sequence number (compositor-facing), so a keepalive tick can never make the
  compositor re-walk a list whose arena has been freed.
- CPU_COMPOSITE mode needs no special reader handling; it *is* today's path.

### 9.12 Display-list ring, backpressure and overflow -- the C7 resolution

The draft said "2 slots" **and** "the ARM never waits". Those contradict: if the FPGA is still
reading slot A while the ARM finishes the next frame, the ARM must either stall (the thing
Tier-B exists to remove) or overwrite a slot in use (the fetch engine then follows `src_addr`
into reused memory).

**Resolution:**
1. **Three slots**, each with an explicit **owner flag** (ARM or FPGA). The ARM may only write
   a slot it owns; the FPGA releases a slot when it finishes the frame. Three gives one frame
   of slack beyond the +1 pipeline latency of section 10.
2. **No free slot => the ARM DROPS the frame.** It does not publish and does not block. Game
   logic continues; only presentation is skipped, and the reader keeps showing the previous
   framebuffer via its existing stale-frame path. Dropping is a **performance** event, never a
   correctness one.
3. **Per-slot scratch.** The CPU-fallback scratch that `LINEAR` commands point into is
   **owned by the slot**, so 3 x 1 MB. A slot's scratch may only be reused once its owner flag
   returns to the ARM. Without this, reusing scratch under a list still being fetched is the
   same corruption in a different buffer.
4. **Command-count overflow falls back, it does NOT truncate** (finding M6). A slot holds a
   bounded number of commands; a frame needing more is emitted as **CPU_COMPOSITE** (9.10).
   Truncating would silently drop sprites -- visible corruption, and exactly the class the
   no-regression contract forbids. The ARM must count before publishing.

Sizing note: command volume scales as sprites x bands, and bands vary from 2 (240x200) to 56
(Lust Rush) across the census -- so the bound must be computed per PAK from its band count,
not assumed from He-Man.

### 9.13 Fetch-engine bounds and quiesce -- the C8 resolution

The fetch FSM exits a row only on `0xFF`. If arena content changed underneath it -- model
unload, PAK load, hot-swap, `.s1` replay reset -- the byte stream is arbitrary and `0xFF` may
never arrive. The existing reader guards **every** wait state with `TIMEOUT_MAX`; the new
block must not be the exception.

**Bounds (all three, each aborting the band):**

| budget | bound | rationale |
|---|---|---|
| per row | `src_w * 2 + 2` bytes | worst legal encoding alternates 1-clear/1-visible = 2 bytes per pixel, plus the terminator |
| per command | `n_rows * (src_w*2 + 2)` beats | a command cannot outlive its own declared row count |
| per band | fixed cycle budget | catches a pathological command list, not just a bad row |

On any breach: **abort the band, do NOT publish the frame, leave the previous framebuffer
intact, and latch a status bit the ARM can read.** A dropped frame is invisible; a runaway
fetch walking DDR3 is not.

**Quiesce protocol.** 14.4.4 requires a free/reuse allocator, so arena memory *will* be
recycled under a running compositor. Before the ARM frees or reuses any arena region -- model
unload, PAK load, hot-swap, reset, `enable` deassert, or entry into CPU_COMPOSITE mode -- it
must:

```
  ARM: set quiesce_req            (compositor control word)
  FPGA: finish or abandon the band in flight, stop issuing DDR3 reads, set quiesce_ack
  ARM: wait for quiesce_ack, THEN free/reuse
  ARM: clear quiesce_req to resume
```

with an ARM-side timeout on the ack so a wedged compositor degrades to CPU_COMPOSITE rather
than hanging the engine. This is the same lifecycle discipline the rest of the core already
has, and the design previously addressed **none** of these events.

### 9.14 Remaining MAJOR-finding resolutions (M1, M4-M14, M17, M18)

**M1 -- band clamps: VERIFIED, table unchanged.** The shipped per-row span applies three
clamps the draft omitted (`if(yy1<=yy0) yy1=yy0+1; if(yy1>H) yy1=H; if(yy0>=H) yy0=H-1`).
Re-deriving `R` for all 20 census resolutions **with** the clamps and testing whether any
band's first source row was already consumed by its predecessor: **zero overlaps, and every
`R`/lines/band-px/bands value is identical to section 8.2's table.** The concern was
legitimate; the outcome is clean. The clamped form is now the normative definition.

**M4 -- 🛑 blended fallbacks CANNOT be `LINEAR`; this is a real hole.** Rasterising a sprite
standalone into scratch loses the *destination* operand, so a fallback that blends cannot be
reduced to a source bitmap. Worse, the fallback set is exactly the blend-heavy one: OpenBOR
shadows are **scaled AND alpha-blended**, and `water` *displaces destination content*, so it
is not a source rasterisation at all. **"Z-order is exact" holds only for OPAQUE fallbacks.**
Resolution -- a fourth gate tier:

| fallback kind | handling |
|---|---|
| opaque (no blend fp) | rasterise to scratch, submit as `LINEAR` in its z-slot |
| blended, but the mode is one the FPGA implements | submit as `LINEAR` **carrying `blend_mode`**; the FPGA blends it against the band exactly like a sprite |
| water / displacement, or any mode the FPGA lacks | 🛑 **the whole FRAME falls back to CPU_COMPOSITE (9.10)** |

The third row is the honest one: some frames simply cannot be split, and forcing them would
break z-order. Frequency must be measured before Phase 2 -- if water-using PAKs are common,
they lose the offload entirely, which is a performance answer, not a correctness one.

**M5 -- torn header reads.** The 4-qword header is published with the sequence number LAST
and a `__sync_synchronize()` before it (the shipped writer already does this at
`native_video_writer.c:723` for exactly this reason). The FPGA reads the sequence number
first, then the body, then **re-reads the sequence number and discards the frame if it
changed**. Without that, geometry from frame N can pair with a sequence from N+1.

**M6 -- command volume.** Folded into 9.12: the bound is computed **per PAK** from its band
count (2 for 240x200, 56 for Lust Rush), and overflow falls back rather than truncating.

**M7 -- the encoding could not express variable band geometry.** Section 8.2 says source
lines vary +/-1 per band and claimed a per-band parameter that did not exist. **Added: a
per-band descriptor** ahead of each band's command list --
`{ src_y0, src_lines, out_y0, out_rows, cmd_count }` -- so the FPGA never derives geometry
from the global header, and `vcnt` per output row comes from the descriptor plus the
clamped formula. `END_BAND` remains the list terminator.

**M8 -- 🛑 flipx is NOT "a signed step".** `putsprite_flip_` mirrors the clip logic in a way
an implementer will get wrong from that description. Verbatim differences:

| | forward | flipped |
|---|---|---|
| loop | `while(lx < xmax)`, `lx += count` | `while(lx > xmin)`, `lx -= count` |
| skip-run exit | `if(lx >= xmax) break` | `if(lx <= xmin) break` -- **different inclusivity** |
| run fully outside | `if((lx+count) <= xmin) skip` | `if((lx-count) >= xmax) skip` -- **xmax plays xmin's role** |
| partial clip | `if(lx < xmin) { diff = lx-xmin; lx = xmin; }` | `if(lx > xmax) { diff = lx-xmax; lx = xmax; }` |
| other edge | `if((lx+count) > xmax) count = xmax-lx` | `if((lx-count) < xmin) count = lx-xmin` |
| write | `memcpy(dest+lx, data, count)` forward | `dest[--lx] = *data++` **backward, byte at a time** |

The RTL must implement both as distinct clip paths, not one path with a sign flip.

**M9 -- clipping must never skip bytes.** Consequence 1 of section 5 depends on rows being a
contiguous stream. **Normative: always consume every row to its `0xFF` terminator; clipping
suppresses WRITES only.** Skipping a clipped run's bytes desynchronises the stream and every
subsequent row of that sprite.

**M10 / M11 -- memory map.** The 64 MB arena and the scratch pinned at `0x34000000` are
withdrawn; sizing is per 9.9.3 (a duplicate of the eligible subset) with scratch now
**per-slot x3** (9.12). Region evidence is no longer inferred from `ddram.sv`: `/proc/iomem`
shows System RAM is `00000000-1fefffff` (~511 MB), so ~513 MB above `0x1FF00000` is outside
Linux's map. **Still owed before Phase 2: enumerate every existing consumer of that space**
(ascal `vbuf` with `RAMBASE 0x20000000`, `ddr_svc`/`ram2`, the legacy `ddram` master, and the
core's own `0x3A000000` window) and their extents -- the reviewer is right that "confirm base
and size" understated the job.

**M12 -- the CDC table was cargo-culted.** The row flagged as the Option Y trap is **not a
pulse crossing**: the sequence number is read from DDR3 by `ddr_clk` logic, and what crosses
to `clk_vid` today is `frame_ready_reg`, a **level**, via 2-FF -- levels need no widening.
Replaced with the crossings that actually exist: (a) compositor->`clk_vid` framebuffer-valid
and buffer index -- **eliminated entirely by 9.11**, since the compositor writes `ctrl_word`
and the reader's existing sync is reused unchanged; (b) reset synchronisation for the new
block; (c) band-buffer and any new FIFO `aclr` sequencing, which must hold clear for 8 cycles
like the existing `fifo_aclr_cnt`; (d) `quiesce_req`/`quiesce_ack`, a level handshake, 2-FF
each way. **No new fast->slow pulse crossing is introduced.**

**M13 -- ">= 2 px/clock" for BLENDED pixels.** Blending is read-modify-write on the band
buffer, so 2 px/clk needs two reads *and* two writes per cycle. M10K is dual-port, giving one
read + one write per port per cycle. Resolution: **bank the band buffer by x-parity** (even/odd
columns in separate M10Ks), so a 2-pixel span hits two banks and each does its own RMW. An
RLE run starting at an arbitrary `dst_x` therefore needs a one-pixel alignment step at the run
head; the steady state is 2 px/clk. Opaque runs need no read at all and are write-only.
This is a **structural requirement on the band buffer**, not a tuning knob.

**M14 -- what "ZERO changed traces" actually means.** Golden traces are
`FRAME:VIDEOCRC:AUDIOCRC` from the **headless** build, which has no FPGA -- so rung 2 of
14.4.3 compares the **software model** against the shipped CPU compositor, and the CRC surface
must be the composited `vscreen` (stated normatively here). The +1 frame of latency (section
10) exists only on hardware and would re-index `FRAME:` for any on-device comparison, so the
model runs **synchronously** and the trace gate is model-vs-CPU, never hardware-vs-golden.
Hardware verification is the Phase 6 on-device set, judged visually and by `[DCV16]`.

**M17 -- fallback scratch writes were unbudgeted.** The fallback previously wrote its output
into a cached `vscreen`; under Tier-B it writes into DDR3 scratch. Per 9.9 that scratch is
**mapped uncached**, and 9.9's measurement shows uncached *reads* at 22x -- writes are far
cheaper (write-combining) but are **not free and are not yet measured**. Added to the open
items: measure an uncached-write rasterise before Phase 2, and if it is material, place the
scratch in cached memory and have the ARM copy it across, or fall the frame back.

**M18 -- a 4th PLL output is not free.** `OpenBOR.sdc` matches counter names **by literal
string** (`general[0]`, `general[2]`). Regenerating `pll.v` from 3 outputs to 4 re-rolls
placement on a design whose worst path is **+0.128 ns**. Normative: if a separate blitter
clock is ever adopted, (a) verify the generated counter names and update the SDC, (b) re-run
the SEED ladder **with** the extra output, and (c) treat any `pll_hdmi` regression as
blocking. **Default position: run the compositor on `clk_sys` and add no PLL output at all.**

## 10. Ownership and sync protocol

| | today | after Tier-B |
|---|---|---|
| framebuffer writer | ARM | **FPGA** |
| framebuffer reader | FPGA scanout | FPGA scanout |
| ARM publishes | frame counter + active buffer | **display-list sequence number, in a SEPARATE word** (9.11) |
| `ctrl_word` writer (what the reader polls) | ARM | **compositor** on FPGA frames, ARM on CPU_COMPOSITE frames (9.11) |
| staleness detection | 30 vblanks without a frame-counter change -> blank | **UNCHANGED** -- the reader still watches `ctrl_word`, which now moves when the compositor completes a frame (9.11) |
| keepalive thread | bumps the frame counter every 150 ms | bumps `ctrl_word` every 150 ms as today -- **never** the display-list sequence number, so it cannot trigger a re-walk (closes M15) |

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
| **the frame will be read back or written by the ARM** (pause, menu return, screenshot, fade, debug overlay, loading bar) | **CPU-COMPOSITE frame, section 9.10** |
| **`tintmode > 0`, or `alpha == 6 && usechannel`** (M2 -- the blend fn is not what `alpha` says) | CPU |
| `frame->palette == NULL`, or a non-NULL `frame->mask` (C5) | CPU |
| a **blended** fallback whose mode the FPGA lacks, or any **water/displacement** sprite (M4) | **CPU-COMPOSITE frame** |
| the frame's **downscaler variant** is one the RTL does not reproduce bit-exactly (C4) | **CPU-COMPOSITE frame** |
| the frame needs **more commands than a ring slot holds** (M6/C7) | **CPU-COMPOSITE frame** |

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

### 14.4.4 ARENA SIZING -- THE MEASUREMENT IS A LOWER BOUND, DO NOT SIZE FROM IT

The 900-frame bot scan measured, across 435 PAKs:

```
  largest OBSERVED 150.5 MB (Dragon Ball Z Tournament)   median 2.8 MB   mean 7.4 MB
```

🛑 **That is NOT the maximum working set, and sizing the arena from it would be wrong.**
OpenBOR loads models **on demand** via `load_cached_model()` into `model_cache[]` (each entry
carries an `unload` flag), so the working set grows as new characters and levels are reached
and can shrink again. A 900-frame run from boot only ever samples early content. Compare
PAK file size against the measured working set:

| PAK | file | observed working set | fraction |
|---|---:|---:|---:|
| **Ultimate Double Dragon** | **1.31 GB** | **20.4 MB** | **1.5%** |
| Sonic Super Jam | 0.72 GB | 129.7 MB | 17.7% |
| Crisis Evil Bootleg | 0.71 GB | 16.0 MB | 2.2% |
| Vermilion Sword | 0.71 GB | 9.5 MB | 1.3% |
| Ogres Mayhem | 0.56 GB | 79.8 MB | 13.9% |
| Streets of Rage Zombies | 0.58 GB | 6.3 MB | 1.1% |

The sampled fraction ranges **1.1% to 17.7%** and is uncorrelated with file size, so the true
maximum is unknown and could be far above 150 MB. **The earlier claim that a 160 MB arena
leaves "0 PAKs that would not fit" is RETRACTED.**

#### The design response -- make arena size a PERFORMANCE question, not a CORRECTNESS one
Two requirements fall out, and together they satisfy the no-regression contract regardless
of what the true maximum turns out to be:

1. **Per-SPRITE arena-exhaustion fallback, not per-PAK.** When the arena is full, the next
   sprite allocation falls back to `malloc` and that sprite is marked CPU-only. A PAK with a
   huge working set then loses offload *progressively* -- it never breaks, and never fails
   to load. Sizing becomes tuning.
2. **The arena needs a real allocator with free/reuse, NOT the bump allocator this document
   proposed in section 6.** Because `model_cache[]` entries can be *unloaded*, a bump
   allocator would never reclaim their sprites; over a long session with many
   load/unload cycles the arena would fill even when the instantaneous working set is small,
   and every later sprite would permanently fall back to CPU.

#### To actually size it (Phase 2 work)
The honest measurement is a static upper bound: sum the decoded pixel area of every sprite
image in each PAK from image headers only (bounded, header-only reads, same pattern as
`pak_videoscan.py`). That covers all 450 including Ultimate Double Dragon and Lust Rush,
which the runtime scan cannot reach.

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
