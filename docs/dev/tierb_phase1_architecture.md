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
   compositing-bound. Target: **He-Man locked at 59.92 Hz** (today **34-48 fps** -- the
current `#FPS_BUCKETS.md` anatomy, 41 samples, post-16-bit -- which supersedes both the
old "34-56" Phase-0 range and section 15's 20.0/29.0 ms pair, that being an older
two-sample measurement of the same thing).
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
| Every number here is measured on the current shipped engine -- **except where explicitly labelled** an estimate (14.2b), an upper bound (14.4.5, 14.5) or a prediction (15) |
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
  (**106.7 MB/s for He-Man**, worst PAK 325.9 MB/s, against a 433 MB/s conservative
  ceiling -- see 14.4.5, and note 14.5: these are upper bounds from a census that excludes
  the slow path, so the headroom claim is **unsupported until it is re-run**. The earlier "133 vs 369" pair came from the
  superseded 2.52x-overdraw estimate in 14.2b; 369 was never reproducible from any
  measurement and is withdrawn.)
- **The ARM reads back composited pixels on TEN known paths** -- seven predictable and
  three that **cannot be predicted a frame ahead**. The canonical enumeration is the table
  in 9.10; do not restate it elsewhere.
  Those frames run in **CPU-COMPOSITE mode** -- see section 9.10. The earlier claim that
  the ARM never reads back was FALSE and is what review finding C3 caught.
- **Z-order is exact for OPAQUE fallbacks**, which are rasterised to a scratch buffer and
  submitted as ordinary `LINEAR` commands in their correct slot — the FPGA executes one
  strictly ordered list per band. A **blended** fallback cannot be reduced to a source
  bitmap that way (its destination operand is missing), so it either carries its blend
  mode on the `LINEAR` command or the whole frame falls back — see M4 and 9.7.
- **The ARM does all the addressing arithmetic.** It pre-seeks every sprite's RLE row
  pointer per band, so the FPGA never walks `linetab` and never does random access.

## 5. The RLE format (verified against pristine v7533 `sprite.c::encodesprite`)

This is the load-bearing fact that shapes the fetch engine. Do not re-derive it from
memory; it was read from upstream source.

```
s_sprite
  header: magic, centerx, centery, offsetx, offsety, srcwidth, srcheight,
          width, height, pixelformat, mask, palette      <-- TWELVE fields
          => on arm32 `data[]` begins at byte 48 (12 x 4), NOT 32.
             The ARM's linetab pre-seek is built on this offset; an earlier
             draft listed only 8 fields and would have been 16 bytes short.
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

> **The REGION is settled; the CONSUMERS are not.** `/proc/iomem` shows System RAM as
> `00000000-1fefffff` (~511 MB), so roughly **513 MB** above `0x1FF00000` is outside
> Linux's map -- not the 256 MB an earlier revision inferred from `rtl/ddram.sv`'s "256MB
> at the end of 1GB" comment, which was **wrong** (9.9.4). What is still owed before any
> allocation is M11's enumeration of every EXISTING consumer of that space (`ascal`'s
> `vbuf` at `RAMBASE 0x20000000`, `ddr_svc`/`ram2`, the legacy `ddram` master, and the
> core's own `0x3A000000` window) and their extents. All bases below are proposals
> contingent on that enumeration.

| Region | Proposed base | Size | Written by | Read by |
|---|---|---|---|---|
| Sprite arena (RLE + palettes) | TBD | **sizing open** (9.9.3) | ARM (at PAK load) | FPGA |
| CPU-fallback scratch (**per slot x3**) | TBD | **sizing open** (9.11.4) | ARM (per frame) | FPGA |
| Display-list ring (**3 slots**) | TBD | **sizing open** (9.11.4) | ARM (per frame) | FPGA |
| Existing window | `0x3A000000` | unchanged | both | both |
| Output framebuffer BUF0/BUF1 | `0x3A000040` / `0x3A040040` | unchanged | **FPGA on FPGA frames, ARM on CPU frames** | FPGA scanout |
| Compositor `status_word` (new) | TBD | 8 B | FPGA | ARM (9.11.1) |
| **Slot-grant word (new)** | TBD | 8 B | ARM | FPGA -- the doorbell; one bit per slot plus the list base (9.11.1) |
| **`quiesce_req` (new)** | TBD | 8 B | ARM | FPGA (9.11.3). `quiesce_ack` rides in `status_word`'s upper half |
| **`compositor_disable`** | n/a -- **a control REGISTER, not DDR3** | -- | ARM | arbiter + compositor, via always-alive logic (9.11.3) |

🛑 **The sprite arena is a DUPLICATE, not a relocation** (9.9.3): the CPU keeps its
ordinary cached sprite -- it must, because uncached reads measured **22.2x** slower -- and
the arena holds a second copy of only the fast-path-eligible subset. Phase 0 finding 2's
"relocation, not duplication" claim is WITHDRAWN, and **every base address and size in the
table above is provisional** until the consumer enumeration of M11 is done (`ascal` is
instantiated with `RAMBASE 0x20000000` in `sys_top.v:717`, so the space is genuinely
contested). Region evidence comes from `/proc/iomem`, not from `ddram.sv`. The ARM
reaches it by `mmap`ing `/dev/mem` exactly as `native_video_writer.c` already does for
`0x3A000000`, and runs an allocator inside it (**with free/reuse -- NOT a bump allocator**; `model_cache[]` entries unload, see 14.4.4). If the pool is exhausted or the mapping
fails, sprite allocation falls back to `malloc` and those sprites are marked CPU-only —
a defense-in-depth gate, not an error path.

## 7. Frame header and display-list encoding

### 7.1 Frame header (one per frame, in the ring)

```
  qword 0: [63:32] magic 'TB01'          [31:16] n_bands      [15:8] downscale_variant
                                                              [7:0]  frame_flags
  qword 1: [63:48] src_w  [47:32] src_h  [31:16] out_w        [15:0] out_h
  qword 2: [63:32] sequence number       [31:0]  total_commands
  qword 3: [63:32] cover_addr  LINEAR only: base of the 1-bit coverage plane, or 0 for a
                               fully-opaque source (background) -- see 9.7
           [31:16] cover_stride bytes per coverage row
           [15:1]  reserved
           [0]     cover_mode  0 = colour-key on 0x0000 (engine-identical, parallax),
                               1 = use the coverage plane (CPU-rasterised sources)

  then, ahead of each band's command list, a PER-BAND DESCRIPTOR:
    { src_y0, src_lines, out_y0, out_rows, cmd_count }
```

Three fields the first draft lacked, each mandated by a review finding:
- **`downscale_variant`** (C4) -- the ARM decides which of the five shipped output paths
  this frame takes; the FPGA must never infer it from width, because variant selection
  also depends on a pointer alignment the FPGA cannot observe.
- **`frame_flags`** -- carries the CPU_COMPOSITE marker (9.10) so the mode is explicit.
- **The per-band descriptor** (M7) -- source lines vary by +/-1 per band, so a single
  global `band_height` cannot express the geometry. `vcnt` per output row comes from the
  descriptor plus the clamped formula of 8.2. `END_BAND` still terminates each list.

`sequence number` identifies the display list. It is **not** what the reader watches:
the reader still polls `ctrl_word`, which only the ARM writes (9.11.1).

### 7.2 Command word (32 bytes = 4 DDR3 beats, naturally aligned)

```
  qword 0: [63:56] opcode      SPRITE | LINEAR | FILL | END_BAND
           [55:52] blend_mode
           [51:48] flags       bit0 flipx, bit1 has_clip
           [47:32] dst_x       signed, in source-resolution space
           [31:16] dst_y       SOURCE row within this band (0 .. src_lines-1 from the
                               per-band descriptor). NOT an output row: a band is R output
                               rows spanning up to ~3R source lines (8.2), and sprites are
                               placed in source-resolution space like dst_x
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
- `LINEAR` — raw RGB565 rows. Transparency is per-source-kind and the ARM resolves it:
  the fullscreen background is fully opaque, a plainly-blitted parallax layer keys on
  `0x0000` as the engine does, and anything CPU-rasterised (fallback sprites, and parallax
  layers that take a scale/rotate/water path) carries a 1-bit coverage plane instead.
  See the three-case table in 9.7. Used for the background blit **and** for
  CPU-rasterised fallback sprites. This is the command that makes z-order exact.
- `FILL` — solid rectangle (cheap, covers a few engine cases).
- `END_BAND` — terminates the band's list.

Volume scales as **sprites x that PAK's band count**, and band count runs 2 (240x200) to
56 (Lust Rush) across the census -- so it must be computed per PAK, never assumed from
one. He-Man is ~93 sprites/frame **on device** (14.2 blits/frame headless; the two are
not interchangeable) across **15** bands, i.e. up to ~1,395 commands x 32 B =
**~45 KB/frame**, plus per-band descriptors, per-band background `LINEAR`s and
`END_BAND`s. Still negligible as bandwidth (~2.7 MB/s against 787.5 MB/s) but **not**
negligible against a ring slot: see the ring-sizing open item in 9.11.4.

### 7.3 Who bins into bands: the ARM

The ARM already knows each sprite's y extent, and duplicating a command per band is far
cheaper than making the FPGA re-read one global list once per band (2 to 56 times
depending on the PAK; 15 for He-Man). So the ARM emits **one
list per band**, with the per-band `src_addr` already seeked and `n_rows` already clipped.
The FPGA walks band 0's list to `END_BAND`, then band 1's, sequentially. No indirection,
no random access, no `linetab` walk in hardware.

## 8. Band geometry — **a band is a whole number of OUTPUT rows**

> **This section was rewritten after checking the first draft against the full 450-PAK
> resolution census (`pak_dimension_census.md`). The first rule was wrong for 8 PAKs and
> impossible for 2.** Recorded here deliberately: the census is what caught it, and the
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

**8 PAKs broken, 2 of them fatally.** Heights 475 and 243 are coprime with 224, so
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
  BAND_BUDGET_PX = 32,768        (64 KB; an M10K in x16 mode is 512 DEEP, so this is
                                  64 M10K single / 128 double = 26.3% of the 486 free --
                                  NOT the 51/102/21% an earlier draft floored it to)
  span(y) = [yy0, yy1) with yy0 = floor(y*H/224), yy1 = floor((y+1)*H/224),
            then the SHIPPED clamps:  if (yy1<=yy0) yy1=yy0+1
                                      if (yy1>H)    yy1=H
                                      if (yy0>=H)   yy0=H-1
  R = max { r : max_k [ union of span(y) for y in band k ] * W  <=  BAND_BUDGET_PX }

  ^ the CLAMPED form is normative (M1). Re-deriving all 20 census resolutions with the
    clamps reproduces every row below exactly and yields ZERO band overlaps, including
    the four H<224 upscale cases.
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
| 480x272 (98 PAKs) | 56 | 68 | 32,640 | 4 |
| 432x243 (MK Outworld) | 69 | 75 | 32,400 | 4 |
| 400x300 (Gunman) | 60 | 81 | 32,400 | 4 |
| 384x224 | 85 | 85 | 32,640 | 3 |
| 336x240 | 90 | 96 | 32,256 | 3 |
| 320x240 (282 PAKs) | 95 | 102 | 32,640 | 3 |
| 256x224 | 128 | 128 | 32,768 | 2 |
| 240x224 | 136 | 136 | 32,640 | 2 |
| 240x200 | 153 | 136 | 32,640 | 2 |

**All 450 PAKs fit, including the 1600-wide outlier** — a wide PAK simply gets fewer output
rows per band. That also closes the "width > 960" open item: no capability gate is needed
for width, because the budget is in pixels.

Double buffering (**128 M10K**, per the corrected 512-deep x16 sizing above) lets the
compositor fill band N+1 while the downscaler drains
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
               else -> advance dst_x by clearcount; goto VIS
                       (NOT "a signed step" for flipx -- the flipped path is a
                        structurally different clip loop, see M8)
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
- 🛑 **There is a THIRD palette-selection site the discriminator does not cover.**
  `_putsprite`'s `plainsprite` path -- taken when `drawmethod == NULL` or
  `drawmethod->flag == 0` -- calls `putsprite_x8p16(..., sprite->palette, NULL)`
  unconditionally. The B4 patch is a `strict_replace` on the `putsprite_ex` `case PIXEL_16:`
  only, so this call is untouched. The ARM must reproduce this branch when resolving
  `pal_addr`, or every flagless sprite gets the wrong LUT.
- `frame->palette == NULL` and a non-NULL `frame->mask` both become **offload-gate
  conditions** (CPU fallback). The reasons differ: with `frame->palette == NULL` the
  ternary deterministically yields `drawmethod->table` -- not "undefined" -- and the gate
  exists because if THAT is also NULL the engine dereferences a NULL palette
  (`spritex8p16.c:443`), so there is no address to put in the command;
  a masked sprite is routed to `putsprite_mask_`/`putsprite_mask_flip_`, which **ignores
  the blend function entirely** -- palette selection is unaffected. Right gate, and now
  the right reason.
- The `[DCV16]` byte-identity acceptance set **must** include ATOV + TMNT-RP + one modern
  PAK -- the exact trio the locked-path verification ritual already mandates.
- The 12 locked v3.10 patches are **not touched**; Tier-B only reads the flags they set.

### 9.4 Palette RAM
One 256-entry x 16-bit on-chip RAM, reloaded when `pal_addr` changes (512 B = 64 beats).
Worst case, using the census maximum of **56 bands** (Lust Rush) rather than one PAK's
count: ~10 distinct palettes per band x 56 bands x 64 beats = 35,840 beats
(2.2% of a frame, 17.2 MB/s). At He-Man's 15 bands it is ~10 x 15 x 64 = 9,600 beats
per frame = **0.6% of the clock budget** and **4.60 MB/s** (9,600 x 8 B x 59.92; the 4.9
an earlier revision printed was the withdrawn 16-band figure). Reload-on-change is therefore
sufficient; a 2-4 entry palette cache is the escape hatch if measurement disagrees.

### 9.5 Blend unit -- the ship build uses LUTs, so the FPGA must too (M3, M2, M16)

**M3: every `blend_*16` has TWO paths, and for the five LUT modes the ship build takes the
LUT one.** (HALF is the exception -- see below.)
```c
unsigned short blend_screen16(unsigned short c1, unsigned short c2) {
    unsigned char *tbl;
    if((tbl = blendtables[BLEND_SCREEN])) return _color16(tbl[_ri], tbl[_gi], tbl[_bi]);
    return _color16(_screen16(...), _screen16(...), _screen16(...));   /* arithmetic */
}
```
`apply_patches.py` populates five of them: SCREEN, MULTIPLY, OVERLAY, HARDLIGHT, DODGE.

🛑 **But the ship build does not call `blend_screen16` for LUT modes at all.**
`apply_patches.py` #2 replaces the `else if(blend)` dispatch in `putsprite_x8p16` with a
scan over `blendfunctions16[0..4]`; a hit routes to **`putsprite_lut_` / `putsprite_lut_flip_`**,
which inline `_lutcolor(tbl[_lutri], tbl[_lutgi], tbl[_lutbi])` with the table hoisted out
of the pixel loop -- no fp call. The arithmetic is identical (`_lutcolor` = `_color16`,
`_lutri/gi/bi` = `_ri/gi/bi`), so byte-identity is unaffected; the RTL reference is the
inlined form. HALF, `blend_tint16` and `blend_rgbchannel16` sit outside
`blendfunctions16[0..4]` and take the fp path, as does every blend issued before
`create_blend_tables_x8` runs.
`tables[BLEND_HALF] = NULL`, so HALF alone takes the arithmetic path. Specifying "unpack
5/6/5, blend, repack" would have reproduced the path the ship build **does not run**.

**This makes the blend unit simpler and bit-exact by construction.** The LUT indices are
```
  _ri = (r1<<5)|r2            0..1023      (5-bit x 5-bit)
  _bi = (b1<<5)|b2            0..1023      shares the same range as _ri
  _gi = ((g1<<6)|g2) + 1024   1024..5119   (6-bit x 6-bit)
```
so one mode's table is **5,120 bytes** and all five are **25 KB**. Byte-wide that is
**5 M10K per table = 25 total**, not the ~20 an earlier draft got by assuming perfect
bit-packing -- the same floor-vs-depth error M1/N3 caught in the band buffer. And
`_color16(tbl[_ri], tbl[_gi], tbl[_bi])` is **three reads of the SAME table per pixel**,
so at the 2 px/clk target (6 reads against 2 M10K ports) the **active table needs 3
replicas**. Budget ~25 resident + replication, not "trivially on-chip". The FPGA is
still byte-identical to the CPU with **no divides and no DSPs**.

**HALF takes the arithmetic path** -- `blendtables[BLEND_HALF]` is left NULL -- but that
path is trivial: `blend_half16` (`pixelformat.c:583-596`) is
`_color16((_r1+_r2)>>1, (_g1+_g2)>>1, (_b1+_b2)>>1)`, a plain per-channel average on the
already-extracted 5/6/5 fields. There is no masking, and `create_half16_tbl` computes the
identical `(i+j)>>1`, so LUT and arithmetic agree exactly.

*(An earlier revision claimed `_half16` was a **masked** shift-add differing by +/-1, and
proposed gating HALF to CPU as an escape hatch. That was wrong: there is no `_half16`
macro anywhere in the engine -- the masked idiom belongs to the 32-bit `_half`. HALF is
reproducible in RTL as a shift and an add, and needs no escape hatch.)*

🛑 **The LUTs do not exist for the whole run.** `create_blend_tables_x8` is called at
`openbor.c:46513` (the gate is `:46511`), **after `load_models()` at `:46474`**, and is gated on
`pixelformat == PIXEL_x8` (true in our build). So every blend issued during PAK load --
including the `update_loading()` compositing path 9.10 enumerates -- runs on the
**arithmetic** path. Any "the ship build always takes the LUT" reasoning is false for
load-time frames.

🛑 **Operand order is load-bearing.** OVERLAY, HARDLIGHT and DODGE are **not commutative**;
the call is `blendfp(src, dest)` -- source high, destination low. Swapping them silently
changes every blended pixel.

**M2: `alpha` alone does NOT determine the blend function.** `getblendfunction16` also reads
two globals: `tintmode > 0` replaces *any* mode with `blend_tint16` (a composition of two
modes over `tintcolor`), and `usechannel` turns mode 6 into `blend_rgbchannel16`. Both are
script-mutable per sprite, and they are mutually exclusive (`if` / `else if`). The gate
"mode outside 1..6 -> CPU" would have passed a tinted sprite to the FPGA to be rendered with
the plain mode -- **a gate that offloads something not identical**, precisely the class
14.4.1 claims is impossible.

The exact conditions matter: `drawmethod_global_init` (`pixelformat.c:70-90`) sets
`tintmode = drawmethod->tintmode` and **derives** `usechannel` as
`(channelr<255)||(channelg<255)||(channelb<255)` -- nobody "sets" it -- and **forces both
to 0 when `!drawmethod->flag`**. So the gate is
`drawmethod->flag && drawmethod->tintmode > 0`, and
`alpha == 6 && drawmethod->flag && (channelr<255 || channelg<255 || channelb<255)`.
The ARM evaluates them when building the command, exactly as it resolves the palette (9.3).

🛑 Note the escalation: a tinted sprite is **blended with a mode the FPGA lacks**, so
by M4 it is not a cheap per-sprite fallback -- it takes the **whole frame** to
CPU_COMPOSITE. `tintmode` is typically set for a whole stage, so such a PAK loses the
offload for that level entirely. The tint population must be measured, not assumed rare.

**M16: the blend scope is measured over the library, not three PAKs.** Freezing scope from
He-Man/Avengers/PDC2 is the one-example fallacy this document's own lesson table forbids and
that the census has already caught twice. Because all five LUTs cost ~25 M10K resident,
**the scope question mostly dissolves: implement all six.** Phase 1b's histogram now serves
to prioritise verification order, not to decide what exists.

### 9.6 Downscaler -- FIVE shipped variants, not one box (the C4 resolution)

The first draft said `hcnt = src_w / out_w`. Now that variant 1 is known to be dead, the
320-wide PAKs run variant 5, where `x0 = x` and `x1 = x+1` give `hcnt = 1` -- which is
exactly what the naive `320/320` yields. So the naive **horizontal** factor happens to be
right for 339 PAKs (7 + 50 + 282) and **wrong for 111** (the 99 variant-4 PAKs plus the 12
non-320 variant-5 ones). That is not a reprieve: the naive formula omits the **vertical**
box, the truncated reciprocal and the rounding term, which are shared by variants 2-5 and
wrong for all 450. *(An earlier revision said "wrong for 393 of 450", computed before the
variant-1 measurement and left standing after it.)*
`src/native_video_writer.c` dispatches the 16bpp path on width into **five** distinct
implementations, and byte-identity requires reproducing whichever one a PAK actually hits.
🛑 **MEASURED ON DEVICE 2026-07-30: variant 1 never executes** (see below), so the
shipped library is served by variants 2-5 and the 282 PAKs that appear to belong to
variant 1 are actually handled by variant 5:

| # | condition (as written in the source) | what it does | PAKs |
|---|---|---|---:|
| 1 | `width == 320 && ((uintptr_t)src_row & 15) == 0` | **NO BOX AT ALL** -- NEON BGR->RGB swap only, doubly-truncated NN row pick. 🛑 **DEAD CODE: the gate can never be true** (measured, below) | **0** |
| 2 | `width == NV_FRAME_WIDTH * 3` (960) | 3x horizontal box, `hcnt == 3` | 7 |
| 3 | `width == NV_FRAME_WIDTH * 2` (640) | 2x horizontal box, `hcnt == 2` | 50 |
| 4 | `width * 2 == NV_FRAME_WIDTH * 3` (480) | 3:2 box with **per-parity** hcnt -- even dest column `hcnt=1`, odd `hcnt=2`, and **two reciprocals** `rc_e`/`rc_o` | **99** |
| 5 | else | scalar general box, **per-column** `hcnt = x1 - x0` from `x0=(x*W)/320`, `x1=((x+1)*W)/320`, clamped `if (hcnt > 7) hcnt = 7`, via `recip16[hcnt]` | **294** (12 by width + the 282 that fall through variant 1) |

Variant 5's widths: 1600, 800, 720, 500, 432, 400, 384, 336, and **256/240 which are NARROWER
than 320** -- there `x1 <= x0` clamps to `x0+1`, giving `hcnt = 1` and **column replication,
i.e. an UPSCALE**. The design never mentioned upscaling at all. Plus, per the measurement
below, **every 320-wide PAK**, where `x0 = x` and `x1 = x+1` give `hcnt = 1` -- a
vertical-only box, not the NN row pick variant 1 would have done.

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
1. 🛑 **Variant 1 is UNREACHABLE. MEASURED ON THE DEVICE 2026-07-30 -- not inferred.**
   Its gate is `width == 320 && ((uintptr_t)src_row & 15) == 0`, and `engine/sdl/videocommon.c:126`
   passes `vscreen->data` straight through. The result is arithmetic, not luck:

   | link | value | source |
   |---|---|---|
   | `getVideoSurface` hands over `vscreen->data`, not `screen->pixels` | **confirmed** | its `bscreen` branch is dead -- see the second measurement below |
   | `offsetof(s_screen, data)` | **20** | `types.h:97-108`; `magic/width/height/pixelformat` (4 each) + `palette` (4). Measured, not counted by hand |
   | 20 mod 16 | **4** | -- |
   | glibc arm32 `MALLOC_ALIGNMENT` | **8** (`2 * SIZE_SZ`) | so any `malloc` base is 0 or 8 mod 16 |
   | therefore `data & 15` | **4 or 12, never 0** | 0+4 or 8+4 |
   | `pitch` for 320-wide 16bpp | **640**, itself 16-aligned | so EVERY row inherits the same residue |

   Measured with `tools/harness/vscreen_align_probe.c` -- an armhf binary reproducing
   `s_screen` and `allocscreen()` verbatim, run **on the MiSTer** (md5-verified on landing)
   and, identically, under QEMU:

   ```
   offsetof(s_screen, data) = 20
   allocscreen(320,240,PIXEL_16), 128 trials, heap state perturbed between each:
       base%16 = 8, data%16 = 12   (every trial, mmap path)
       ->     0/128 screens with (data & 15) == 0
       -> 0/28672 individual rows with (src_row & 15) == 0
   small brk-path allocation (320x64):   0/128
   VERDICT: downscale variant 1 is UNREACHABLE on this allocator.
   ```

   **The second measurement -- which surface is even handed over.** The alignment argument
   only applies if `getVideoSurface` takes its `else` branch. It has a `bscreen` branch
   (`sdl/videocommon.c`) that returns `screen->pixels` instead, and `bscreen` is allocated
   in exactly two places (`:38-59`):

   | condition | status |
   |---|---|
   | `videomodes.pixel == 1` | **false** -- the Path-B 16-bit vscreen makes `pixel = 2` |
   | `savedata.swfilter && (savedata.hwscale >= 2.0 \|\| savedata.fullscreen)` | **false on every config on the device** |

   `s_savedata` (`engine/source/savedata.h`) sums to **exactly 324 bytes** on arm32 -- which
   is the size of the current-era `.cfg` files, so `savesettings` writes the struct raw and
   its fields decode at fixed offsets (`swfilter` 272, `fullscreen` 288, `hwscale` 316).
   Decoding **all 16** OpenBOR configs on the device:

   ```
   324-byte OpenBOR configs scanned : 16
   with swfilter != 0               : 0
   with bscreen actually LIVE       : 0
   ```

   The decode is trustworthy rather than "it read zeros": the fields VARY across files
   (A Tale of Vengeance has `fullscreen = 1`, AvP Aftermath `0`; `usegl`/`hwfilter`/`vsync`
   are all 1), so real data is being read at those offsets. And the values cannot drift at
   runtime: `swfilter`/`hwscale`/`fullscreen` are set only from the engine's video-options
   menu (`openbor.c:50151+`), which lives in the stock pause menu -- and
   `patches/pausemenu_patch.c` replaces that wholesale with an Options submenu of
   **Music Volume / SFX Volume / Back**. The video options are unreachable on MiSTer.

   **Consequence: the 282 320-wide PAKs (63% of the library) are served by variant 5**,
   the general per-column box -- with `x0 = x`, `x1 = x+1`, so `hcnt = 1` and the box is
   vertical-only. The FPGA must reproduce **that**, not the NN row pick. Variant 1 is dead
   code in the ship build and needs no RTL at all.

   🛑 **One narrow residual, and it is design-relevant: `swfilter` is PER-PAK.** Each PAK
   has its own `.cfg`, so a config imported from a PC OpenBOR install could carry
   `swfilter != 0` -- and A Tale of Vengeance already has `fullscreen = 1`, so for that PAK
   the condition would flip and `getVideoSurface` would hand over a **doubled-width**
   `screen->pixels` (640 for a 320-wide PAK), routing the frame to **variant 3** instead.
   This does not threaten the conclusion above, but it settles a design question: the
   downscale variant is a **per-PAK, runtime-resolved property, not a static one**. That is
   why consequence 2 stands even though the alignment term turned out constant -- the ARM
   must evaluate the real condition per frame and encode the answer, and the FPGA must
   never infer the variant from width.

   🛑 **Keep an assertion `pitch % 16 == 0`.** The whole result rests on the pitch
   being 16-aligned; a future non-aligned pitch would make the residue vary per ROW and
   reintroduce exactly the nondeterminism this measurement rules out.
2. **The variant is still ARM-decided and header-encoded**, even though the alignment term
   is now known to be constant. The FPGA cannot observe a pointer, and the constancy is a
   property of today's allocator + header layout rather than a guarantee -- so the ARM
   evaluates the real condition and encodes the answer. The FPGA must never infer the
   variant from width.
3. **The frame header gains a `downscale_variant` field**, and "output-path variant" joins
   the capability gate: any variant the RTL does not implement bit-exactly routes the whole
   frame to CPU-COMPOSITE mode (section 9.10).
4. **Build order, settled by the measurement: variant 5 FIRST.** It carries **294 PAKs**
   (65%), including every 320-wide one, and it is the hardest of the five -- per-column
   `hcnt`, a truncated reciprocal, the `hcnt > 7` clamp, and the narrow-width upscale case.
   Then 4 (99 PAKs), then 3 and 2 (50 + 7, constant `hcnt`). **Variant 1 is not built.**
   This inverts the draft order, which put the hardest variant last on the belief that it
   served 12 PAKs.
5. Two further branches exist in the same function and are **dead in the ship build**
   (`bpp` is always 16): `bpp == 8 && palette` (NN via `src_x_table`) and `bpp == 32`,
   the latter containing its own distinct >4x stride-cap arithmetic. Named here so a
   future reader does not rediscover them as missing requirements -- and note that
   variant 1 now joins them as **present in the source but never executed**.
6. The `src_y = (y*sy256)/256` + `if (src_y >= height) src_y = height - 1;` pair
   (`native_video_writer.c:176-177`) sits **above and outside** the variant if/else chain,
   so it is shared by all five variants -- and it computes the very `src_row` whose
   alignment gates variant 1. Variants 2-5 then ignore `src_row` for pixel data (they use
   `sbase = src + yy0*pitch`), but not for the gate. Part of the byte-identity contract.

### 9.7 CPU fallback path
An **opaque** non-fast-path sprite (`gfx_draw_scale` and friends) rasterises into the
scratch region and the ARM emits a `LINEAR` command in its correct z-slot; the CPU keeps
only the rasterisation cost, the 28.8% measured in section 2. A **blended** fallback
cannot be handled that way -- see the three-tier table in M4.

**`LINEAR` has three source kinds and only one of them needs a coverage channel.** An
earlier revision banned the colour key outright on the grounds that RGB565 has no spare
bit; that over-reached, because **the shipped engine already keys on exactly `0x0000`**
(`blendscreen16`, reached from `screen.c:552` with `key = transbg`), so for two of the
three kinds reproducing that key is what byte-identity REQUIRES, not what it forbids:

| `LINEAR` source | engine behaviour | needs a coverage channel? |
|---|---|---|
| fullscreen background (`openbor.c:45800`, NULL drawmethod) | `_putscreen` forces `table=NULL, alpha=0, transbg=0` (`screen.c:476-480`) -> `copyscreen` / per-row `memcpy` | **no -- fully opaque** |
| level parallax layers (`openbor.c:45151`, `&screenmethod`) | keys on `sp[i] == 0` via `blendscreen16` | **no -- key on `0x0000`, black pixels included; a coverage plane here would DIVERGE** |
| CPU-rasterised fallback sprite | source RLE transparency (`TRANSPARENT_IDX == 0`) is destroyed by rasterising to RGB565 | **yes -- this is the only case** |

So the coverage plane (1 bit alongside the scratch rows, or 32-bit scratch with an alpha
byte) is a **fallback-scratch** feature, and its bandwidth is budgeted with M17 and the
per-slot scratch sizing of 9.11.4. The "28.8% of putsprite time" framing does not apply --
that is the fallback share, and the background/parallax `LINEAR`s are not part of it.

🛑 **Parallax layers are not plain backgrounds.** `spriteq_add_screen` at
`openbor.c:45151` passes `&screenmethod`, carrying `water.watermode`, `amplitude`,
`scalex`, `rotate`, `xrepeat`/`yrepeat`, `table`, `alpha` and `transbg` -- so `_putscreen`
routes them to `gfx_draw_plane` / `gfx_draw_water` / `gfx_draw_rotate` / `gfx_draw_scale`
exactly like a sprite (`screen.c:482-507`).

🛑 **This splits the parallax row of the table above, which is why the two must be read
together.** A parallax layer drawn through the plain `blendscreen16` path (no scale, no
rotate, no water) keys on `0x0000` and is a true `LINEAR` -- row 2 of the table. A parallax
layer whose `screenmethod` selects `gfx_draw_scale`/`rotate`/`water` is **CPU-rasterised**,
so it loses its transparency exactly like a fallback sprite and takes **row 3** -- it needs
the coverage plane. The ARM decides per layer, from the same drawmethod fields it already
inspects for the fast-path gate.

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
- 🛑 **BOTH directions are gated per grantee, not just returns** -- specifically, a
  synthetic `ddr_busy = 1` is presented to a non-grantee **while another master holds an
  outstanding grant** (masking unconditionally would deadlock at idle: everyone masked
  means nobody ever asserts `rd`, so the arbiter never sees a request). A masked requester
  must HOLD `rd`/`we` + `addr` until granted, which the reader does for free -- it only
  clears `ddr_rd` on `!ddr_busy` (`openbor_video_reader.sv:338-339`), and that is what
  makes "`reader.sv:345` stays correct unmodified" true. `ddr_dout_ready` gating alone is
  not enough: `DDRAM_BUSY` is `ram_waitrequest` (`sys_top.v:1868`), and every
  requester's idiom is `if (!ddr_busy) begin ...; ddr_rd <= 1'b1; state <= ST_WAIT_...; end`.
  A non-grantee that sees `!ddr_busy` will "issue" a read into a mux pointed elsewhere,
  then wait out `TIMEOUT_MAX` and lose the scanline -- **every line, for as long as the
  other master runs.** The arbiter must therefore present a **synthetic `ddr_busy = 1` to
  every non-grantee** as well as withholding `ddr_dout_ready`. With both gated,
  `reader.sv:345` stays correct unmodified.
- 🛑 **The arbiter needs its own beat timeout -- and force-release ALONE re-creates the
  corruption this section exists to prevent.** Grant held "until the burst's beats return"
  deadlocks against the reader's existing escape: `ST_WAIT_LINE` leaves on
  `timeout_cnt == TIMEOUT_MAX` *precisely because* beats did not arrive, so a grant pending
  returns that never come is never released -- both masters blocked, black screen until
  core reload. Today the reader self-heals. **But a late burst is late, not cancelled:**
  its beats still arrive, and with no ID they are indistinguishable from the next
  grantee's, so a bare force-release feeds them to the wrong master -- exactly the
  `beat_count` desync quoted above. Normative: on force-release the arbiter **keeps
  counting the abandoned burst and swallows its remaining beats before granting anyone
  else**, and it never revokes a grant with beats outstanding (including on
  `compositor_disable`, which must therefore take effect at the next grant boundary, not
  mid-burst). An abandoning requester signals abandon so the arbiter knows the beats are
  now unclaimed rather than pending.
- **Strict priority: video reader > compositor.** The reader must land 80 qwords per
  scanline; a scanline is 63.7 us and a full 80-beat burst is ~0.81 us at 98.4375 MHz, i.e.
  **~1.3% of a scanline**. The margin is enormous.
- **Non-preemptible, so cap the compositor's burst.** Worst-case reader wait is one
  compositor burst. Capping the compositor at 64 beats bounds that at **~0.65 us**, ~1% of
  a scanline -- still negligible against the reader's existing `TIMEOUT_MAX` guard.

### What this costs
Two extra states and a beat counter per requester (~100 ALM), plus the grant mux. No new
clock domain: everything is already `ddr_clk`. It must be **written and verified before any
compositor RTL**, because without it the very first co-existing burst corrupts video --
so it belongs in the phase table as its own item, ahead of every compositor block.

Two further requirements the draft missed:
- **A third requester exists in the source**, the legacy `ddram` master -- though `use_nv`
  is a literal constant (`OpenBOR.sv:340` `wire NATIVE_VID = 1'b1;`), so it is
  constant-folded out at synthesis. Excluding it is free rather than a requirement; keep
  it excluded so a future non-constant `use_nv` cannot quietly add a third master.
- 🛑 **Prerequisite fix in the reader itself.** 9.11.4's bounds are modelled on the
  reader's `TIMEOUT_MAX` discipline, but that discipline is not actually universal:
  `ST_WAIT_AUDIO_WR` and `ST_WAIT_AUDIO_RING` have **no timeout at all**, so a dropped
  beat there hangs the whole reader FSM -- video and audio together. Adding a second
  master to the port raises the odds of exactly that slip, so those two states must gain
  timeouts in the same arc as the arbiter.

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
| He-Man | 33.5 MB headless / 44.9 MB on-device | ~24-32 MB |
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
Enumerated for the PREDICTABLE six from pristine v7533 `openbor.c` -- the three
unpredictable paths are in 9.11.5 and are NOT in this table (every read of `vscreen` as a
source, plus every `copyscreen`/`putscreen` touching it):

| site | function | operation | why it reads the composited frame |
|---|---|---|---|
| `openbor.c:21659` | `pausemenu()` | `copyscreen(pausebuffer, vscreen)` | snapshots the live frame to draw the menu over. **Our `pausemenu_patch.c:70` does the same** |
| `openbor.c:21619` | `backto_mainmenu()` | `copyscreen(pausebuffer, vscreen)` | same snapshot on menu return |
| `openbor.c:45850-45853` | `update()` | `screenshot(vscreen, getpal, 1)` | key edge, `bothnewkeys & FLAG_SCREENSHOT` -- available at the TOP of `update()`, so **predictable a frame ahead**. (`openborscript.c:12148` is a player-key NAME TABLE, not a capture API; a script can only read the key state via `getplayerproperty("screenshot")`) |
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
Every one of the ten paths gets an explicit on-device check in the Phase 6 regression set.
🛑 **THE CANONICAL LIST -- every other enumeration in this document must match it.**

| # | path | predictable? |
|---|---|---|
| 1 | pause menu | yes |
| 2 | main-menu return | yes |
| 3 | screenshot key edge (`openbor.c:45850`) | yes -- `bothnewkeys` is readable at the top of `update()` |
| 4 | fade in | yes |
| 5 | fade out | yes |
| 6 | `screen_printf` debug overlay | yes |
| 7 | `update_loading()` compositing path | yes |
| 8 | `openborvariant("vscreen")` (+ the `changeopenborvariant` write side) | **no** (9.11.5) |
| 9 | script `putscreen`/`drawscreen` onto `vscreen` | **no** (9.11.5) |
| 10 | anigif cutscene capture | **no** (9.11.5) |

That is **seven predictable + three unpredictable = TEN**, not the "nine" earlier
revisions claimed -- the count was wrong because the debug overlay was dropped from one
list and the screenshot was double-counted in another. Getting this right matters because a
black pause snapshot is exactly the class of bug that shipped before -- CLAUDE.md records it for
the PIXEL_32 pausebuffer, and `pausemenu_patch.c:66-67` carries the comment explaining that
`copyscreen` early-returns on a format mismatch.

### 9.11 ONE ownership model -- the unified C6 / C7 / C8 resolution

Round 2 found eight critical defects and **five of them were in the seams between C6, C7
and C8**, because those three were specified in three separate passes and never reconciled.
They are replaced here by a single model. Nothing below may be read in isolation.

#### 9.11.1 The invariant: ONE state owner, not merely one writer

Round 2 replaced "the compositor writes `ctrl_word` on FPGA frames" with "the ARM is the sole
`ctrl_word` writer". Round 3 showed that framing was still wrong, in two ways -- both because
it reasoned about the *address* being written rather than the *state* behind it.

**The real ARM-side writer inventory.** There are four sites and **two independent state
sets**:

| writer | state it uses | site |
|---|---|---|
| `video_copy_screen` -> `WriteFrame` | `frame_counter`, `active_buf` | `apply_patches.py`; `native_video_writer.c:726-729` |
| `KeepaliveTick` | **the same two** | `native_video_writer.c:745-748` |
| black-frame present (wait-for-cart) | the same two | `patches/sdlport_patch.c:333` |
| 🛑 **`mister_present`** | **its own `mister_frame_cnt` / `mister_active_buf`** | `patch_sdl_dummy.py:41-43,154,243-245` |

So the round-2 claim that "`frame_counter` and `active_buf` live in exactly one place" was
**false**, and gating only `video_copy_screen` would have left `mister_present` free to write
a framebuffer and publish `ctrl_word` from stale private state during an FPGA frame. Defects
#3 and #4 were relocated, not collapsed.

Equally, the round-2 description of the keepalive was wrong in the other direction:
`KeepaliveTick()` does **not** use private state -- it shares `frame_counter`/`active_buf`
with `WriteFrame`, which is exactly the 2026-05-22 loading-bar-jitter fix. The convention it
relies on is that `WriteFrame` **post-toggles** `active_buf`, so `!active_buf` always names
the buffer most recently written.

**That convention is what a naive Tier-B publish breaks.** On FPGA frames the ARM stops
calling `WriteFrame`, so `active_buf` never toggles; a publish that writes `ctrl_word`
directly for `completed_buf` is silently reverted 150 ms later by the next keepalive tick,
which still emits `(!active_buf)` from the stale ARM-era value. Same failure mode as May,
one level up.

**Resolution -- three normative rules:**

1. 🛑 **Publish goes THROUGH the `native_video_writer.c` statics, never around them.** The
   publish step performs `frame_counter++; active_buf = completed_buf ^ 1;` and then the same
   `ctrl_word` store `WriteFrame` uses, so the `!active_buf`-names-the-published-buffer
   invariant holds in every mode. Publish and keepalive must be **mutually excluded** (the
   `frame_counter++`-then-store is a non-atomic read-modify-write across two threads).
2. 🛑 **Every one of the four sites is gated on frame mode**, not just `video_copy_screen`.
   `mister_present`'s separate state set must be unified with `native_video_writer.c`'s (or
   `mister_present` disabled outright) **before Phase 5** -- as long as two state sets exist,
   the invariant is unenforceable.
3. **The compositor never writes `ctrl_word`.** It reports completion in a status word.

```
  compositor -> status_word   (FPGA writes, ARM reads)
      [31:2]  completed_seq    display-list sequence the compositor just finished
      [1]     abort            band aborted / fetch timeout -- do NOT publish this frame
      [0]     completed_buf    which framebuffer it wrote
      (upper 32 bits: quiesce_ack, slot-release bitmap, band-timeout status)

  ARM -> ctrl_word            (ARM writes, reader reads -- UNCHANGED FROM TODAY)
      [31:2]  frame_counter
      [0]     active_buffer
```

🛑 **`completed_seq`, `abort` and `completed_buf` MUST share one 32-bit word** (C2). On arm32
a 64-bit read of the `/dev/mem` `O_SYNC` Device mapping is two loads and is not
single-copy-atomic, so splitting the sequence from the buffer bit across halves lets a poll
that straddles an FPGA write pair frame N's `completed_seq` with frame N+1's `completed_buf`
-- publishing the buffer the compositor is **currently writing**. That is precisely the torn
read M5 fixes in the ARM->FPGA direction, and nothing symmetric existed FPGA->ARM. Fields
that do not need to be read atomically with the sequence (quiesce_ack, slot bitmap) may live
in the upper half.

🛑 **The compositor may not begin a frame whose target framebuffer is published or holds an
unpublished completion** (C3). There are only two framebuffers, and the reader re-reads the
published buffer every frame. Nothing in the round-2 model constrained the compositor's
buffer choice: with three slots it could run three lists ahead of publication, so one late
ARM poll (a long engine tick, a re-composite frame per 9.11.5) let it write into the buffer
being scanned
out. **Consequence: at most TWO lists can be in flight**, whatever the slot count -- the
third slot buys queueing, not an extra frame of buffer freedom.

**The doorbell** (C4). `slot[k] = ARM | FPGA` needs a home: a **slot-grant word in the
ARM->FPGA control region** (section 6), one bit per slot, plus the list base. The publish
ordering is M5's, applied to the whole slot rather than just the header: write the body,
`__sync_synchronize()`, then set the grant bit. Without it the compositor has no defined way
to learn a list exists and no guarantee the body is complete when it does.

Each frame the ARM: (a) reads `status_word`; (b) on a new `completed_seq` with `abort` clear,
publishes via rule 1; (c) builds the next list into a slot it owns and rings the doorbell.

What this actually buys:

| former defect | status |
|---|---|
| keepalive is a second writer (#3) | **closed by rule 1** -- one state owner, and the keepalive stays enabled in every mode |
| no counter/buffer handoff (#4) | **closed by rules 1+2** -- there is one state set, so a mode change needs no handoff |
| 30 stale vblanks blank the screen (#8) | closed -- the keepalive still runs (29 stale vblanks is ~484 ms, far beyond its 150 ms), so a run of dropped or aborted frames never reaches the staleness blank |
| reader re-verification | avoided -- the reader, its staleness blank and its buffer selection are untouched |

#### 9.11.2 State table

`grant` is the arbiter's (9.8). `slot[k]` is A(RM) or F(PGA). `buf` is the ARM's `active_buf`.

| step | compositor | slots | ARM does | `ctrl_word` |
|---|---|---|---|---|
| reset | idle; the FPGA zeroes `status_word` on reset (the ARM never writes it) | all A | zero `ctrl_word`, clear the slot-grant word, publish a black frame as today | ARM |
| first frame (always CPU) | idle | all A | `WriteFrame` -> BUF0, publish | ARM |
| steady FPGA frame N | walks slot k | k=F, others A | read status; publish frame N-1's buffer; build list N into a free slot; mark it F | ARM |
| no free slot | busy | all F (queued, but only <=2 in flight per 9.11.1) | **drop**: skip presentation, keep playing, keepalive holds the reader | ARM (keepalive) |
| command/scratch overflow | -- | -- | emit frame N as **CPU_COMPOSITE** instead of truncating | ARM |
| mode change -> CPU | quiesce (9.11.3) | released to A | after `quiesce_ack`: `WriteFrame` targets `active_buf`, which rule 1 of 9.11.1 has kept equal to `published_buf ^ 1` -- so no toggle or special case is needed here | ARM |
| CPU frames | disabled | all A | exactly today's path | ARM |
| resume -> FPGA | re-enabled | all A | build a list; first FPGA publish is one frame later | ARM |
| band abort / timeout | abandons, sets abort bit, **releases its slot** | k -> A | sees abort in status, drops the frame, does not publish | ARM (keepalive) |

Two rules that were previously unstated and are load-bearing:

- **Abandon releases.** A slot returns to the ARM on completion **or** on abort, quiesce or
  disable. Without this, three aborts leak all three slots and every later frame drops
  forever (arch #18).
- **Publish suppression is part of quiesce.** A compositor finishing its last band during a
  quiesce must NOT report a completion the ARM would publish after it has taken over. Abandon
  means abandon (arch #5).

#### 9.11.3 Quiesce, and a disable the ARM can assert unilaterally

Arena memory is recycled under a running compositor (model unload, PAK load, hot-swap,
`.s1` replay reset, level load), so the ARM must be able to stop it. Both signals are DDR3
words polled by each side -- **there is no wire** (arch #22); the compositor polls once per
band, not once per frame, so a quiesce costs at most one band, not up to 16.7 ms.

```
  ARM:  set quiesce_req
  FPGA: finish or ABANDON the band in flight, suppress any pending completion,
        stop issuing DDR3 reads, release all slots, set quiesce_ack
  ARM:  wait for quiesce_ack (bounded), THEN free/reuse
  ARM:  clear quiesce_req to resume
```

🛑 **`compositor_disable` is a REGISTER, not a DDR3 word, and not part of the
handshake.** A DDR3 word only takes effect when the compositor polls it -- and a wedged
compositor by definition does not -- so a word cannot stop the thing disable exists to
stop. It also has to reach the **arbiter**, which is `ddr_clk` fabric with no DDR3 read
path of its own. Therefore: an ARM-writable control register, level-triggered,
unconditional, sampled by always-alive logic independent of the compositor FSM, and the
arbiter refuses grants while it is set. (Round 2 specified it as a DDR3 word alongside
`quiesce_req`, which closed nothing.)

🛑 **Disable implies unilateral reclaim.** "Abandon releases the slot" depends on the
**compositor** setting the release bitmap; a compositor wedged enough to need disable never
will. So asserting disable must let the ARM reclaim all slots and their scratch itself,
and the arena-free rule for the ack-timeout case must be stated: after disable is asserted
and the arbiter confirms no grant is outstanding, no compositor read can be in flight, so
the ARM may free. Without that the ARM has no legal action on timeout -- it may not free
(no ack) and cannot prove reads have stopped.

`enable` is NOT a quiesce trigger: it is `use_nv`, a constant `1'b1` (`OpenBOR.sv:489`).

#### 9.11.4 Ring, slots and fetch bounds

**Three slots**, each with an owner flag; the ARM may only write a slot it owns. Three gives
one frame of slack beyond the publication latency (1-2 frames, section 10). 🛑 **The third
slot buys QUEUEING, not buffer freedom** -- 9.11.1's cap still applies: at most **two**
lists may be in flight, where "in flight" means published-to-the-FPGA and not yet
completed-and-published-onward. **No free slot -> the ARM drops the frame** and
never blocks -- safe now that the keepalive always runs (9.11.1).

**Per-slot scratch**, reusable only once the slot returns to the ARM. Sizing is **not** the
inherited "1 MB": one full-screen 960x480 RGB565 fallback is 921,600 B, so a single sprite
fills it (arch #16). Scratch must be sized from the census like the ring is, and
**scratch exhaustion falls back to CPU_COMPOSITE exactly as command overflow does** -- it
must never truncate.

**Command volume is computed per PAK from its band count** (2 for 240x200, 56 for Lust Rush),
not assumed from He-Man -- and the per-band descriptor, per-band background `LINEAR` and
`END_BAND` all count. On He-Man's 15 bands that is ~1,395 sprite commands plus overhead,
which is **~45 KB/frame**, so a 128 KB ring cannot hold three slots of it. Ring size is an
open sizing item, not a fixed 128 KB.

**Fetch bounds** (all three abort the band, and abort releases the slot):

| budget | bound |
|---|---|
| per row | `src_w * 3 + 1` **bytes** -- the worst LEGAL row is `clearcount = 0` on every pixel (0x00 is explicitly legal, section 5), i.e. 3 bytes per pixel plus the terminator. Real `encodesprite` output peaks near `1.5 * src_w + 1`, so this is loose by design: the bound guards ARBITRARY content, which is the whole point of C8 |
| per command | `n_rows * (src_w * 3 + 1)` **bytes** (bytes, NOT beats; a beat is 8 bytes on the 64-bit port) |
| per band | a fixed cycle budget, to catch a pathological list rather than one bad row |

On breach: abort the band, set the abort bit, do not report completion, leave the previous
framebuffer intact. A dropped frame is invisible; a runaway fetch walking DDR3 is not.

*(Sections 9.12 and 9.13 were folded into 9.11 in round 2; the numbering gap is deliberate
so existing cross-references stay valid.)*

#### 9.11.5 Read-back that cannot be predicted (arch #6)

9.10 requires the ARM to set CPU_COMPOSITE **before** the frame it needs. Script-driven
read-back cannot honour that. The three that cannot are:

| path | site | why unpredictable |
|---|---|---|
| `openborvariant("vscreen")` | `openborscript.c:1132`, `:9359` | a cart can take the composited surface pointer at any time. 🛑 The **write** side, `changeopenborvariant("vscreen", ...)` (`:9905`), is worse: it lets a script REPLACE the surface outright, so the compositor's target is not even stable |
| script `putscreen`/`drawscreen` onto `vscreen` | routes to `blendscreen16` (`screen.c:552`) | reads the destination whenever `alpha > 0` |
| anigif cutscene capture | -- | runs mid-frame |

All run *during* a frame whose mode is already committed, and on an FPGA frame `vscreen`
is not composited -- a black return.

*(Two corrections to an earlier revision. **`getscreen()` does not exist in v7533** -- a
grep of the whole engine tree returns zero hits; the real hazard is the `vscreen`
openborvariant above. And **`blendscreen` is not a script path** -- it is a gamelib blit
(`screen16.c`) reached only from `_putscreen`, so the script-facing name is `putscreen`.)*

**Resolution:** on an unpredicted read-back the ARM **synchronously re-composites `vscreen`
on the CPU for that frame** and marks the *next* frame CPU_COMPOSITE. Cost is one frame of
duplicated work; the alternative is a black screenshot. These three paths must be listed
explicitly alongside the predictable ones (pause, menu return, fade in/out, debug overlay,
loading bar) -- the earlier enumeration claimed to be exhaustive and omitted all three.

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
shadows are **scaled AND alpha-blended** (`openbor.c:29704-29726`).
**"Z-order is exact" holds only for OPAQUE fallbacks.** (An earlier draft also claimed
`water` *displaces destination content*; that is **wrong** -- `gfx_draw_water` reads only
the source and writes each source row at a per-row x offset, so it IS a source
rasterisation with a per-scanline warp and belongs in the blended row, not the
whole-frame row, unless it carries a mode the FPGA lacks.)
Resolution -- a fourth gate tier:

| fallback kind | handling |
|---|---|
| opaque (no blend fp) | rasterise to scratch, submit as `LINEAR` in its z-slot |
| blended, but the mode is one the FPGA implements | submit as `LINEAR` **carrying `blend_mode`**; the FPGA blends it against the band exactly like a sprite |
| any blend mode the FPGA lacks (incl. `tintmode`/`usechannel`) | 🛑 **the whole FRAME falls back to CPU_COMPOSITE (9.10)** |

The third row is the honest one: some frames simply cannot be split, and forcing them would
break z-order. Frequency must be measured before Phase 2 -- if water-using PAKs are common,
they lose the offload entirely, which is a performance answer, not a correctness one.

**M5 -- torn header reads.** The 4-qword header is published with the sequence number LAST
and a `__sync_synchronize()` before it (the shipped writer already does this at
`native_video_writer.c:723` for exactly this reason). The FPGA reads the sequence number
first, then the body, then **re-reads the sequence number and discards the frame if it
changed**. 🛑 **This governs the FRAME HEADER within a slot. The publication event for the
slot itself is the doorbell grant bit of 9.11.1** (body -> barrier -> grant bit); the
header re-read remains necessary because a slot the ARM still owns may be rewritten while
the FPGA is speculatively reading it. The two are complementary, not alternatives. Without that, geometry from frame N can pair with a sequence from N+1.

**M6 -- command volume.** Folded into 9.11.4: the bound is computed **per PAK** from its band
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
| partial-clip pointer | **`count += diff; data -= diff`** (diff negative) | **`count -= diff; data += diff`** (diff positive) -- the sign flip is the part that gets implemented wrong |
| write (ship build, `spritex8p16.c`) | `dest[lx++] = palette[*data++]`, NEON-unrolled by 8 | `dest[--lx] = palette[*data++]` **backward, one pixel at a time** |

The RTL must implement both as distinct clip paths, not one path with a sign flip.

**M9 -- clipping must never skip bytes.** Consequence 1 of section 5 depends on rows being a
contiguous stream. **Normative: always consume every row to its `0xFF` terminator; clipping
suppresses WRITES only.** Skipping a clipped run's bytes desynchronises the stream and every
subsequent row of that sprite.

**M10 / M11 -- memory map.** The 64 MB arena and the scratch pinned at `0x34000000` are
withdrawn; sizing is per 9.9.3 (a duplicate of the eligible subset) with scratch now
**per-slot x3** (9.11.4). Region evidence is no longer inferred from `ddram.sv`: `/proc/iomem`
shows System RAM is `00000000-1fefffff` (~511 MB), so ~513 MB above `0x1FF00000` is outside
Linux's map. **Still owed before Phase 2: enumerate every existing consumer of that space**
(ascal `vbuf` with `RAMBASE 0x20000000`, `ddr_svc`/`ram2`, the legacy `ddram` master, and the
core's own `0x3A000000` window) and their extents -- the reviewer is right that "confirm base
and size" understated the job.

**M12 -- the CDC table was cargo-culted.** The row flagged as the Option Y trap is **not a
pulse crossing**: the sequence number is read from DDR3 by `ddr_clk` logic, and what crosses
to `clk_vid` today is `frame_ready_reg`, a **level**, via 2-FF -- levels need no widening.
Replaced with the crossings that actually exist: (a) compositor->`clk_vid` framebuffer-valid
and buffer index -- **eliminated entirely by 9.11**, since the ARM remains the sole
`ctrl_word` writer (the compositor writes only `status_word`) and the reader's existing
sync is reused unchanged; (b) reset synchronisation for the new
block; (c) band-buffer and any new FIFO `aclr` sequencing, which must hold clear for 8 cycles
like the existing `fifo_aclr_cnt`; (d) `quiesce_req`/`quiesce_ack`, a level handshake, 2-FF
each way. **No new fast->slow pulse crossing is introduced.**

🛑 **UNSETTLED -- whole-frame gates are discovered MID-FRAME, after the mode is
committed.** 9.10 is normative that "the ARM must set the flag BEFORE the frame it needs",
but four conditions are only discovered while walking `spriteq_draw` and emitting commands:
a tinted sprite (9.5), a blended fallback in a mode the FPGA lacks (M4), command or scratch
overflow (9.11.4), and the downscaler variant (9.6 -- now known to be per-PAK and
runtime-resolved). 9.11.5 solves this for read-back only. The two candidate mechanisms are
a **`spriteq` pre-scan** before command emission (costs a second walk of the queue) or a
**retroactive abandon** (discard the partly-built list, release the slot, re-run the frame
as CPU_COMPOSITE -- costs a frame but no second walk). Neither is specified. Phase-2
decision.

🛑 **UNSETTLED -- `[DCV16]` CANNOT RUN under this ownership model.** The probe (section 12)
requires, **on the same frame**, a CPU-composited `vscreen` to downscale AND an FPGA-written
framebuffer to compare it against. 9.11.1 forbids the ARM's present path on an FPGA frame,
and 9.10 composites `vscreen` only on CPU frames -- so the two operands never coexist. This
is load-bearing, not cosmetic: `[DCV16] == 0` is the phase-4 gate (section 13), the
byte-identity criterion (section 15) and the acceptance test for the downscaler (section 12).

Resolution is a Phase-2 decision and needs a **dedicated `[DCV16]` frame mode**: composite
on the CPU into `vscreen` as a CPU frame, suppress the ARM's `WriteFrame`, ALSO run the
display list so the compositor writes its framebuffer, downscale the CPU `vscreen` into a
private buffer, and compare that against what the FPGA wrote. That frame presents neither
result. Until this is specified, section 15's headline criterion is unmeasurable.

**M13 -- ">= 2 px/clock" for BLENDED pixels.** Blending is read-modify-write on the band
buffer, so 2 px/clk needs two reads *and* two writes per cycle. M10K is dual-port, giving one
read + one write per port per cycle. Resolution: **bank the band buffer by x-parity** (even/odd
columns in separate M10Ks), so a 2-pixel span hits two banks and each does its own RMW. An
RLE run starting at an arbitrary `dst_x` therefore needs a one-pixel alignment step at the run
head; the steady state is 2 px/clk. Opaque runs need no read at all and are write-only.

🛑 **Two banks cap the design at 2 px/clk, and section 15 asks for more.** The worst
PAK needs 3.06 px/clk on the uncorrected overdraw and **3.31 px/clk** on section 14.5's
corrected 41.67x -- above the criterion section 15 itself sets. Both numbers were written
in the same pass and neither was checked against this section. Resolution is a Phase-2
decision, and there are only two: **3-way (or 4-way) banking**, which also multiplies the
blend-LUT replica count and so the M10K budget, **or** Ninja-class PAKs join the
capability gate and run on CPU. It cannot stand as written.
This is a **structural requirement on the band buffer**, not a tuning knob.

**M14 -- what "ZERO changed traces" actually means.** Golden traces are
`FRAME:VIDEOCRC:AUDIOCRC` from the **headless** build, which has no FPGA -- so rung 2 of
14.4.3 compares the **software model** against the shipped CPU compositor, and the CRC surface
must be the composited `vscreen` (stated normatively here). The 1-2 frame publication latency (section
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
| ARM publishes | frame counter + active buffer | frame counter + active buffer (via rule 1 of 9.11.1), **plus** a display-list sequence number, which lives in the ring's frame header (7.1) and is NOT read by the video reader |
| `ctrl_word` writer (what the reader polls) | ARM | **ARM, in every mode** -- the compositor reports completion in a `status_word` the ARM reads (9.11.1) |
| staleness detection | 30 vblanks without a frame-counter change -> blank | **UNCHANGED** -- the reader still watches `ctrl_word`, which the ARM still writes, on the same 150 ms keepalive floor as today (9.11.1) |
| keepalive thread | bumps the frame counter every 150 ms | **UNCHANGED, and enabled in every mode.** It is safe **not** because "there is only one writer" -- `mister_present` is a second writer with its own state (9.11.1) -- but because rule 1 makes the publish step maintain `frame_counter`/`active_buf` so `!active_buf` always names the published buffer, and publish and keepalive are **mutually excluded** (the `frame_counter++`-then-store is a non-atomic RMW). It is what keeps a run of dropped frames away from the reader's 30-vblank staleness blank, and it never touches the display-list sequence number, so it cannot trigger a re-walk (closes M15) |

The pipeline is: the ARM builds the list for frame N while the FPGA composites frame N-1.
That is **1-2 frames of latency (16.7-33.4 ms), phase-dependent** -- and the variability is
new. Round 1's compositor-publishes model was +1 by construction; with publication moved to
an ARM poll (9.11.1), a completion landing just after the poll waits a further frame, so
the figure is a range with jitter, not a constant. Either accept the jitter or specify a
phase-locking rule. It must be stated in the
release notes. It is not avoidable without making the ARM wait on the FPGA, which would
reintroduce exactly the stall Tier-B exists to remove.

The `NativeVideoWriter_KeepaliveTick()` single-source-of-truth rule still applies: the
keepalive and the publish step must share `frame_counter` and `active_buf` -- NOT the
display-list sequence number, which the keepalive must never touch (9.11.1 rule 1). The
state that matters is `active_buf`, because the keepalive publishes `!active_buf`. Not separate
counters. Two counters racing on one control word is the loading-bar-jitter bug class.

## 11. New CDC paths

| crossing | direction | mechanism |
|---|---|---|
| `ddr_clk` (98.4375 MHz) <-> band buffer | same domain | none needed |
| band-complete pulse -> downscaler | same domain | none needed |
| compositor -> `clk_vid` framebuffer-valid / buffer index | **does not exist** | eliminated by 9.11.1 -- the ARM remains the sole `ctrl_word` writer, so the reader's existing 2-FF `frame_ready_reg` level sync is reused unchanged |
| reset -> new compositor block | -- | standard reset synchroniser |
| band-buffer / new FIFO `aclr` | -- | hold clear >= 8 cycles, like the existing `fifo_aclr_cnt` |
| `quiesce_req` / `quiesce_ack` | -- | **not wires** -- DDR3 words polled by each side (9.11.3); the compositor polls once per band |
| `compositor_disable` | ARM write -> `ddr_clk` fabric | 🛑 **a REGISTER, not a DDR3 word** (9.11.3) -- it must reach the arbiter, which has no DDR3 read path, and must work when the compositor is too wedged to poll. Level, 2-FF synchronised into `ddr_clk`. **OPEN: the design has no ARM->FPGA register channel yet** -- the only ARM->FPGA paths today are DDR3 and `hps_io`, so this needs either an `hps_io` status/ctrl word or a small always-alive DDR3 poller independent of the compositor FSM |

🛑 **No new fast->slow PULSE crossing is introduced**, so the Option Y concern-F trap
does not apply here. An earlier draft listed "sequence number -> `clk_vid`, 2-FF with a
widened pulse" and called it that trap verbatim; that row was cargo-culted. The sequence
number is read from DDR3 by `ddr_clk` logic and never crosses to `clk_vid` at all, and
what does cross today is `frame_ready_reg`, a **level**.

🛑 **But section 12's own diagnostic reintroduces it.** Routing `band-complete` --
a 1-cycle 98.4375 MHz pulse -- to `VGA_R/G/B`, which are consumed in `clk_vid` at
53.693 MHz, is exactly the fast->slow single-cycle pulse a 2-FF sync misses. Widen it at
the source or convert it to a toggle before it crosses.

**A 4th PLL output is NOT free.** It would come from a spare counter on the existing PLL
(Cyclone V PLLs have 9; `rtl/pll.v` instantiates 3), so it does not consume the 3/6 PLL
budget -- but `OpenBOR.sdc` matches counter names by **literal string** (`general[0]`,
`general[2]`), and regenerating `pll.v` re-rolls placement on a design whose worst path is
**+0.128 ns**. If a separate blitter clock is ever adopted: verify the generated counter
names and update the SDC, re-run the SEED ladder **with** the extra output, and treat any
`pll_hdmi` regression as blocking. **Default position: run the compositor on `clk_sys` and
add no PLL output at all.**

## 12. Diagnostic instrumentation (TEMPORARY DIAG, from day one)

Built in from the first RTL commit, all marked so the CI gate blocks the binary:

1. **VGA-colour state visualiser** — the cheapest FPGA debug tool we have; route band
   walker state / fetch-engine state / band-complete to `VGA_R/G/B` as 1 bit each --
   🛑 **`band-complete` is a 1-cycle 98.4375 MHz pulse and `VGA_*` is consumed in
   `clk_vid` at 53.693 MHz, so it MUST be widened or converted to a toggle before it
   crosses**, or the debug tool reintroduces the exact Option Y concern-F trap it exists
   to find (section 11). This is
   what localised the Option Y H-pass deadlock in 4 cycles after 7 failed deploys.
2. **`[DCV16]`-style byte-identity probe** — for the first N frames, the ARM ALSO computes
   the shipped NEON downscale and compares it to what the FPGA wrote. **Mismatches must be
   0.** This is the acceptance test for section 9.6 (the downscaler).
3. **DDR3 ring-buffer probe** — 256 samples x 64 bits in M10K, sampling band index, command
   index, fetch state and beat counters, for anything the visualiser cannot resolve.
4. **Per-band cycle counters** — read back by the ARM to confirm the compositing-rate
   budget (M13 -- note the target itself is unsettled)
   holds on real content rather than on paper.

## 13. Implementation phases

| phase | content | gate |
|---|---|---|
| 1 | this document | user approval |
| 1b | blend-mode + alpha histogram **across the 450-PAK library** via `[BLD]`/`[BAL]`/`[A15]` | measurement, not opinion. It sets verification ORDER, not scope -- all six modes are implemented regardless (9.5) |
| 1c | **DDR3 arbiter** (9.8) written and verified standalone, **plus the reader's missing `ST_WAIT_AUDIO_WR` / `ST_WAIT_AUDIO_RING` timeouts** (9.8) | must precede every compositor block: without the arbiter the first co-existing burst corrupts video, and without the audio timeouts a dropped beat hangs the whole reader FSM |
| 2 | ARM side only: sprite arena allocator, display-list builder, per-band binning, fallback rasteriser. **Shipped path unchanged** — the list is built and discarded. **The 14.2 register must be cleared first**, including the overdraw census re-run. | list contents validated offline against the CPU's own draw order |
| 3 | RTL: band walker + RLE fetch engine + palette RAM + band buffer. No blend, no downscale. Opaque sprites only. | VGA visualiser shows correct band traversal |
| 4 | RTL: blend unit + downscaler (variant 5 first, 9.6) + output write. | `[DCV16]` mismatch = 0 -- **requires the `[DCV16]` frame mode of 9.14 to exist first** |
| 5 | Ownership: compositor writes the framebuffer + `status_word`; ARM still publishes `ctrl_word`; **keepalive unchanged** (9.11.1). 🛑 **`mister_present`'s separate `mister_frame_cnt`/`mister_active_buf` must be unified with `native_video_writer.c`'s statics BEFORE this phase** -- while two state sets exist the invariant is unenforceable. | singleton-state matrix clean, no black screens |
| 6 | Fallback path + capability gate + all-PAK regression | ATOV + TMNT-RP + modern PAK palette trio verified |
| 7 | Audit cycles until zero bugs AND zero concerns | section 15 criteria |
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

🛑 **This is the authoritative register.** Everything Phase 2 is gated on appears here,
including items whose detail lives elsewhere in the document. An earlier revision listed five
items, two of them already closed, and omitted most of the real ones.

**Design decisions (a wrong answer changes the RTL):**

| # | item | where |
|---|---|---|
| 1 | **Compositing rate**: the specified 2-bank band buffer delivers 2 px/clk; the worst PAK needs ~3.31. Bank 3-4 ways (raising the M10K budget) or gate Ninja-class PAKs to CPU | 9.14 M13 |
| 2 | **`[DCV16]` frame mode** -- the probe cannot run under the ownership model, and it gates phase 4 and headlines section 15 | 9.14 |
| 3 | **Mid-frame whole-frame gates**: pre-scan vs retroactive abandon | 9.14 |
| 4 | **How the runtime-built blend LUTs reach FPGA M10K.** They are built after `load_models()` (9.5), so they cannot be `.mif`-initialised at synthesis; no transport, region or mechanism exists anywhere in this document | 9.5 |
| 5 | **The `LINEAR` source home.** 9.9.2 puts only fast-path SPRITES in the arena, but the background and parallax layers are per-frame cached-heap `s_screen`s with no FPGA-readable address | 9.7, 9.9.2 |
| 6 | **ARM->FPGA register channel** for `compositor_disable` -- none exists today | 9.11.3, 11 |
| 7 | Ring and per-slot **scratch sizing** (a single 960x480 fallback is 921,600 B) | 9.11.4 |

**Measurements owed (a wrong number changes the budget):**

| # | item | where |
|---|---|---|
| 8 | **The overdraw census RE-RUN** with the slow path included and T5's counters widened. 14.4.5's headroom claim is unsupported until this lands -- a prerequisite, not a nice-to-have | 14.5 |
| 9 | **M11's DDR3 consumer enumeration** (`ascal` `vbuf`, `ddr_svc`/`ram2`, legacy `ddram`, the `0x3A000000` window) and their extents | 6, 9.14 |
| 10 | **Arena write cost** -- population is one-time per PAK load, but into strongly-ordered memory | 9.9.4 |
| 11 | **Uncached-write rasterise cost** for the fallback scratch | 9.14 M17 |
| 12 | **M4 whole-frame-fallback frequency** -- if water/tint PAKs are common they lose the offload entirely | 9.14 M4 |
| 13 | **`~10 distinct palettes per band`** is asserted, not measured, and is the sole input to 9.4's bandwidth | 9.4 |
| 14 | **Sprite arena exhaustion headroom.** The observed working set is 44.9 MB on He-Man (46,001 KB) and 150.5 MB at the library max; Lust Rush is 3.12x He-Man's pixel area and has no measurement at all | 9.9.3, 14.4.4 |

**Assertions to add to the code:**

| # | item | where |
|---|---|---|
| 15 | `pitch % 16 == 0` -- the whole variant-selection determinism rests on it | 9.6 |

**Closed by the census** (kept so they are not re-opened): band height per PAK -> a band is a
whole number of OUTPUT rows with `R` chosen per PAK from a pixel budget (section 8); widths
above 960 -> max is 1600 and the budget is in PIXELS, so a wide PAK just gets fewer rows per
band, **no width capability gate needed**; blend-mode scope -> all six are implemented because
the LUTs make them cheap, and Phase 1b now sets verification ORDER only (9.5); reserved-region
base and size -> settled by `/proc/iomem` (9.9.4), leaving only item 9 above.

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
| 480x272 (98 PAKs) | 130,560 | 15.6 | 19.7 | 8.6 | 44.0 | ok |
| 320x240 (282 PAKs) | 76,800 | 9.2 | 11.6 | 8.6 | 29.4 | ok |

**Only Lust Rush is anywhere near the ceiling**, and it is a single PAK at 3.12x He-Man's
area. Everything else has 3x margin or better. Options, to settle in Phase 2: measure Lust
Rush's real overdraw (it may be far below He-Man's 2.52x -- a 1600x900 cart is unlikely to
also carry 2.5x overdraw); lean on the 591 MB/s good-burst figure rather than 433; or let
the capability gate route it to CPU. 🛑 **THIS WHOLE SUBSECTION IS SUPERSEDED by 14.4.5**,
which measured overdraw per PAK instead of scaling one PAK's figure by area, and found
bandwidth tracks OVERDRAW rather than screen size. It is retained only as the record of how
the budget was originally mis-sized. Do NOT treat 398.6 as measured -- and note the
anchor was off too: 14.4.5 measures He-Man at **1.55x**, not the 2.52x assumed here. The
line below claiming "only the He-Man row is anchored in data" is therefore also wrong. Only the He-Man row
is anchored in data; every other row scales one PAK's overdraw ratio by area.

### 14.3 Accepted consequences
- 1-2 frames of publication latency, phase-dependent (section 10).
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
| sprite needs scale / rotate / water / flipy / shiftx / non-transparent fillcolor | CPU (the measured 29%). *(This is the engine's fast-path gate; it is NOT about the `FILL` opcode of 7.2, which is an FPGA-side primitive for solid rectangles.)* |
| sprite working set exceeds the arena | **CPU for the sprites that did not fit** -- per-BLIT, not per-PAK (9.9.4, 14.4.4) |
| ~~PAK width exceeds the band buffer~~ | **not needed** -- all 450 fit (8.2) |
| anything the software model has not proven identical | CPU |
| **the frame will be read back or written by the ARM** (pause, menu return, screenshot, fade, debug overlay, loading bar) | **CPU-COMPOSITE frame, section 9.10** |
| **`tintmode > 0`, or `alpha == 6 && usechannel`** (M2 -- the blend fn is not what `alpha` says) | CPU |
| `frame->palette == NULL`, or a non-NULL `frame->mask` (C5) | CPU |
| a **blended** fallback whose mode the FPGA lacks (M4) | **CPU-COMPOSITE frame** |
| a **water / `gfx_draw_plane`** sprite | rasterises to scratch like any other fallback -- it is a SOURCE rasterisation with a per-scanline warp (M4), so it is whole-frame ONLY if it also carries a mode the FPGA lacks |
| the frame's **downscaler variant** is one the RTL does not reproduce bit-exactly (C4) | **CPU-COMPOSITE frame** |
| the frame needs **more commands than a ring slot holds** (M6/C7) | **CPU-COMPOSITE frame** |

### 14.4.2 The regression net ALREADY EXISTS -- 431 golden traces
`#Golden_Traces/OpenBOR_7533/` holds **one `.trace` per PAK for 431 of the 450**, each
`FRAME:VIDEOCRC:AUDIOCRC` over 120 presented frames from boot, **100% deterministic**
(synthetic clock via `OB_TEST`). Re-scan after a change and diff = an exact blast-radius
list. **Tier-B's required result is ZERO changed traces.**

*(Selection caveat: any PAK chosen here on the strength of the blend census -- e.g. "the
sole OVERLAY user" -- inherits 14.5's T1 defect, which makes slow-path blits invisible to
that census. "Sole" means "sole among fast-path blits". Re-select after the census re-run.)*

Coverage by resolution: **every resolution is fully covered except 1600x900 (Lust Rush),
which has ZERO** -- plus small gaps (480x272 **91/98**, 320x240 **273/282**, 640x480 45/46,
960x540 3/4). 19 PAKs lack a trace: **12 are the script-compile-fail (ec=1) class and
SEVEN ran cleanly** (Heaven's Anime Girls, Hiryu No Ken [Demo], Memory Loss, Monster Girl
Dimensions, Moscow RE-Action, Ogres Mayhem, Rescue Command) -- so a third of the gap is
unexplained, not "almost all" accounted for.

*(Both corrections were self-concealing. The per-resolution covered-counts were the
PRE-correction numbers -- Ultimate Super Mega Beatdown has a trace, so reclassifying it
480x272 moves it on BOTH sides of each fraction -- and either version totals 19.)*
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
OpenBOR loads models via a **`load`/`know` MIX** -- `know` defers, `load` does not -- so
the deferred fraction is **PAK-dependent, not uniform**, and "on demand" overstates it.
Models arrive through `load_cached_model()` into `model_cache[]` (each entry
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

Across the six rows shown the sampled fraction ranges **1.1% to 17.7%**; library-wide it
runs **0.07% to 155.9%, median 4.2%** (the >100% cases are PAKs whose loaded set exceeds
the archive size through decompression). It is uncorrelated with file size, so the true
maximum is unknown and could be far above 150 MB. **The earlier claim that a 160 MB arena
leaves "0 PAKs that would not fit" is RETRACTED.**

#### The design response -- make arena size a PERFORMANCE question, not a CORRECTNESS one
Two requirements fall out, and together they satisfy the no-regression contract regardless
of what the true maximum turns out to be:

1. **Per-SPRITE arena-exhaustion fallback, not per-PAK.** When the arena is full, the next
   sprite allocation falls back to `malloc` and that sprite is marked CPU-only. A PAK with a
   huge working set then loses offload *progressively* -- it never breaks, and never fails
   to load. Sizing becomes tuning.
2. **The arena needs a real allocator with free/reuse** (section 6 now says so; this item
   is what changed it). NOT the bump allocator an earlier draft of this document
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
Per-PAK overdraw (415 PAKs) shows that model is wrong. 🛑 **These are UPPER BOUNDS, not
measurements** -- `p0..p7` count unclipped sprite bounding-box area, and the census misses
the slow path entirely (section 14.5, N15/T1). The heading said "Measured"; it should not
have:

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

### 14.5 🛑 Known defects in the measurement TOOLING

Round 1 titled these "these invalidate published measurements" and no earlier revision
of this document mentioned them. Every figure in 14.4.5 and 14.4.4 must be read through
them.

| # | defect | effect on the numbers above |
|---|---|---|
| T1 | the overdraw census increments its histogram **only inside the fast-path branch** while dividing by full-run pixels | **253,871 of 2,147,759 blits (11.8%), across 186 PAKs, contribute nothing.** Some PAKs are understated by up to 13x. Known corrections: Ninja - Stealth Assassins 38.56x -> ~41.67x (325.9 -> **~350.3** MB/s -- only the sprite
term scales with overdraw; the background and output terms do not), Quack Ninja 1.31x -> 6.05x, Memory Loss 0.13x -> 1.74x |
| N15 | `p0..p7` count **unclipped sprite bounding-box area**, not composited or RLE pixels | every sprite-bandwidth figure is an **upper bound, not a measurement** -- the table is headed "Measured" and should not be |
| T5 | `_mister_tbb_px[]` is uint32 and sits at **63.6% of UINT32_MAX** at `OB_FRAMES=900` | a wrap reports a **low** overdraw -- plausible and wrong in the dangerous direction. The PAK at 63.6% is Ninja - Stealth Assassins, i.e. the same PAK named as the 325.9 MB/s worst case |
| T3 | 19 PAKs still have `skipped > 0` (1,494 images) in the sprite-size scan | the static ceiling is not yet a true ceiling |
| E2 | `data/videopc.txt` is checked **first** on Linux builds and the resolution scanner never looks at it | the census is **not engine-exact as claimed**; it drives 8.2's `R` table, 9.6's variant counts and 14.4.5's per-resolution bandwidth |

**Consequence for the headroom claim.** 14.4.5's "NO PAK exceeds the 433 MB/s conservative
ceiling" is computed from T1/T5/N15-affected inputs. It is **unsupported until the census
is re-run with the slow path included and the counters widened** -- that re-run is a
prerequisite for Phase 2, not a nice-to-have.

## 15. Success criteria

| criterion | target | source |
|---|---|---|
| He-Man sustained fps | **59.92 locked** | 20.0 ms and 29.0 ms are measured today; 9.75 ms and 13.21 ms are **predictions**, not measurements |
| downscale byte-identity | `[DCV16]` mismatch **= 0**, with the acceptance set exercising **variant 5 against 320-wide PAKs** (the majority path, 9.6) | section 12 -- 🛑 **but the probe cannot run as specified; see the UNSETTLED note in 9.14.** A dedicated `[DCV16]` frame mode must be designed in Phase 2 or this criterion is unmeasurable |
| palette regression | ATOV + TMNT-RP + modern PAK all canonical | the locked-palette verification ritual |
| DDR3 bandwidth | **<= 433 MB/s** (the conservative ceiling) on every PAK | 14.4.5: worst case 325.9 MB/s (**~350.3 corrected**), He-Man 106.7 -- **upper bounds from a census 14.5 shows is incomplete; the re-run is a Phase-2 prerequisite**. The old "<=200 vs budget 133" pair came from the superseded 14.2b estimate and was unmeetable by this document's own table |
| compositing rate | **UNSETTLED -- see M13.** 2 px/clk is what the specified 2-bank structure delivers; the worst PAK needs **3.31 px/clk** on 14.5's corrected 41.67x overdraw (3.06 on the uncorrected 38.56x) | per-band cycle counters. 2 px/clk sustains 3.29 Mpx/frame; Ninja needs **5.44 Mpx** corrected (5.03 uncorrected). Either bank 3-4 ways (raising the M10K budget) or gate Ninja-class PAKs to CPU |
| timing | all clocks >= +0.1 ns, `pll_hdmi` >= +0.3 ns preferred | `OpenBOR.sta.summary` |
| audit | one full cycle reporting **zero bugs, zero concerns** | section 13 phase 7 |
| **no broken PAKs** | **ZERO changed golden traces across all 431** -- a HEADLESS software-model-vs-CPU comparison, never hardware-vs-golden (M14). 19 PAKs have no trace at all, incl. Lust Rush | section 14.4 |
| hardware verification | the Phase 6 on-device set, judged visually and by `[DCV16]` | this is what covers the 19 untraced PAKs |
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

1. Confirm the phase ordering in section 13. Note it is **no longer "ARM-side first, RTL
   second"**: the DDR3 arbiter (phase 1c) is RTL and must land BEFORE any ARM-side work,
   because without it the first co-existing burst corrupts video (9.8).
2. Confirm the accepted **1-2 frames** of publication latency, phase-dependent (section 10)
   -- or require a phase-locking rule to make it a constant.
3. Approve running Phase 1b (blend histogram) as the next concrete step.
