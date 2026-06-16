# OpenBOR_7533 — Framebuffer + ASCAL Architecture (FB_* interface)

Reference document for re-architecting the hybrid core's video path away from
the custom DDR3 reader + edge-aware downscale module (the abandoned "Option Y"
architecture, see § 8) toward the canonical MiSTer framework `FB_*` framebuffer
interface, where the ARM binary writes pixels directly into DDR3 and the
framework's `ascal.vhd` scaler reads from that memory region every frame and
generates HDMI output, automatically rescaling any input size to any output
size.

All source citations refer to canonical upstream repos:

| Repo | Role |
|---|---|
| `MiSTer-devel/Template_MiSTer` | The `sys/` framework — `sys_top.v`, `ascal.vhd`, `emu_ports.vh` (canonical FB_* signal declaration) |
| `MiSTer-devel/ao486_MiSTer` | Reference core that drives FB_* in pure RTL (no ARM involvement) — `ao486.sv` |
| `MiSTer-devel/Main_MiSTer` | ARM-side framework binary — `video.cpp` shows the canonical LFB write protocol over SPI |

---

## 1. FB_* signal reference (from `Template_MiSTer/sys/emu_ports.vh:42-65`)

All FB_* signals are declared as **emu module inputs/outputs from the core's
perspective**. The core (e.g. `OpenBOR.sv`) drives the outputs; `sys_top.v`
samples them and forwards them to `ascal.vhd`. Activated by `MISTER_FB`
verilog define in the QSF.

| Signal | Width | Dir (from core) | Carries | Set by |
|---|---|---|---|---|
| `FB_EN` | 1 | output | Master enable. `1` = framebuffer is the HDMI source, core's regular VGA_R/G/B output is bypassed. `0` = ASCAL scales from VGA_R/G/B emu output. | Core (1-bit register) |
| `FB_FORMAT` | 5 | output | Pixel format selector. See § 1.1. | Core |
| `FB_WIDTH` | 12 | output | Framebuffer horizontal size in pixels (0-4095). | Core |
| `FB_HEIGHT` | 12 | output | Framebuffer vertical size in pixels (0-4095). | Core |
| `FB_BASE` | 32 | output | DDR3 byte address of pixel (0,0). Must be aligned (see § 2). | Core |
| `FB_STRIDE` | 14 | output | Bytes per scanline. `0` = ASCAL auto-rounds to 256-byte multiple. Else must be a multiple of pixel size. | Core |
| `FB_VBL` | 1 | input | Framebuffer vertical blanking — pulses high during ASCAL's scanout vblank. ARM/RTL waits on this edge to swap buffers without tearing. | sys_top from `ascal.o_vbl` (after clk-vid resync) |
| `FB_LL` | 1 | input | Low-latency hint. `1` = ASCAL is running in low-latency mode; core may need to throttle writes. Mostly informational. | sys_top |
| `FB_FORCE_BLANK` | 1 | output | When high, ASCAL outputs blanked (black) HDMI even though FB_EN is set. Used during mode changes or for explicit blanking. | Core |

### 1.1 FB_FORMAT encoding (5-bit)

| Bits | Field | Values |
|---|---|---|
| `[2:0]` | Pixel bit depth | `011` = 8 bpp + palette LUT, `100` = 16 bpp, `101` = 24 bpp, `110` = 32 bpp |
| `[3]` | 16-bit subformat | `0` = RGB565, `1` = RGB1555 (ignored for 24/32-bit modes) |
| `[4]` | Byte order | `0` = RGB, `1` = BGR (applies to 16/24/32 modes) |

**For OpenBOR**: the engine renders into a 32-bit `s_screen` buffer at PIXEL_32
(post-v3.9/v3.10 palette pipeline). The wrapper's WriteFrame currently emits
RGB565 into DDR3 as a 16-bit anisotropic-NN-squished frame. Two viable paths:

- **Quick port**: keep RGB565 output, set `FB_FORMAT = 5'b00100` (16bpp, RGB565,
  RGB byte-order). ASCAL handles the rescale to whatever HDMI is asking for.
- **Quality port**: bypass the 32→16 conversion entirely. Write the engine's
  native 32-bit framebuffer straight to DDR3, set `FB_FORMAT = 5'b00110`
  (32bpp, RGB byte-order). Saves CPU on the conversion step and gives ASCAL the
  full color precision to work from.

### 1.2 FB_* PALETTE signals (8 bpp mode only — not used for OpenBOR)

When `FB_FORMAT[2:0] == 3'b011` (8 bpp palette mode), six extra signals expose
a 256-entry RGB palette LUT between core and ASCAL. Gated behind a second
verilog define, `MISTER_FB_PALETTE`. **OpenBOR will use 16-bit or 32-bit direct
modes** — the engine has already collapsed palette LUT lookups into RGB during
PAK rendering (see `[[openbor-v310-dual-flag-discriminator]]`). Palette signals
documented for completeness only:

| Signal | Width | Dir |
|---|---|---|
| `FB_PAL_CLK` | 1 | output | Palette write clock (typically `clk_sys`) |
| `FB_PAL_ADDR` | 8 | output | Palette entry index (0-255) |
| `FB_PAL_DOUT` | 24 | output | Palette entry RGB888 to write |
| `FB_PAL_DIN` | 24 | input | Palette entry RGB888 read back from ASCAL |
| `FB_PAL_WR` | 1 | output | Write enable (1-cycle pulse per entry) |

---

## 2. DDR3 memory layout

ASCAL's framebuffer region lives in the high-latency DDR3, shared with the ARM
via the HPS-FPGA bridge. ASCAL reads via its Avalon master interface
(`avl_*`), the ARM writes via memory-mapped access through `/dev/mem`.

### 2.1 DDR3 address regions (from `sys_top.v:716` ASCAL instantiation)

```
0x0000_0000 ── 0x1FFF_FFFF  : System DRAM (Linux uses ~512MB, kernel-owned)
0x2000_0000 ── 0x21FF_FFFF  : ASCAL internal buffers (RAMBASE=0x20000000, RAMSIZE=0x00800000 × 3 for triple buffer)
0x2200_0000 ── 0x29FF_FFFF  : LFB framebuffer region (Main_MiSTer's UI overlay, FB_ADDR = 0x20000000 + 32MB)
0x2A00_0000 ── 0x37FF_FFFF  : Available — this is where the OpenBOR core's framebuffer should live
0x3800_0000 ── 0x3FFF_FFFF  : Core-private DDRAM region (DDRAM_ADDR[28:25] = 4'h3 in ao486 — currently OpenBOR uses 0x3A000000 here for the existing video ring)
```

The address ARM writes pixels to and the address fed into `FB_BASE` must
match. The `FB_BASE` value is a **byte address**, even though the underlying
DDR3 interface is 64-bit-wide internally. ASCAL handles byte/word alignment as
long as `FB_STRIDE` is consistent.

### 2.2 Recommended OpenBOR layout

Reserve a 16MB region at `0x2A00_0000` for a triple-buffered native 320×224
RGB565 framebuffer:

```
FB_BASE_OFFSET   = 0x2A000000
BYTES_PER_PIXEL  = 2                                 (RGB565) — or 4 for RGB8888
FRAME_BYTES      = FB_WIDTH × FB_HEIGHT × BYTES_PER_PIXEL
                 = 320 × 224 × 2 = 143,360 bytes for native
                 = 1920 × 1080 × 2 = 4,147,200 bytes for Lust Rush at native
BUF_BYTES        = round_up_to_pow2(FRAME_BYTES)     (256-byte alignment minimum for stride=0 case)
```

ARM picks one of three buffers per frame, writes pixels, then atomically
updates `FB_BASE` via a control word visible to RTL. The 16MB region holds at
least 3 × 1080p RGB565 frames (~12MB) with margin.

### 2.3 Alignment constraints

- `FB_BASE` must be 256-byte aligned at minimum (single burst alignment in
  ascal's avalon master) — using a power-of-2 buffer size achieves this
  automatically.
- `FB_STRIDE` must be either `0` (ASCAL rounds up internally) or an exact
  multiple of bytes-per-pixel (so e.g. for RGB565 it can be `640` for native or
  any larger multiple of 2; for RGB8888 a multiple of 4).
- The framebuffer region must NOT overlap ASCAL's internal triple buffer at
  `0x2000_0000` — that's where ASCAL stages the SCALED HDMI frame, not the
  input. Different memory window.

---

## 3. ARM-side write protocol

The ARM binary maps the DDR3 framebuffer region via `shmem_map()`
(`Main_MiSTer/video.cpp:2353` pattern), then writes pixels directly. No SPI
involvement during steady-state rendering — only at startup/reconfig.

### 3.1 Setup phase (one-time per resolution change)

When the engine reports a new native PAK resolution (e.g. PAK 320×240 →
PAK 960×480 swap on hot-load), the ARM updates the framebuffer geometry:

```c
// 1. Compute new frame parameters
uint32_t fb_width  = pak_native_width;      // e.g. 320, 480, 960, 1920
uint32_t fb_height = pak_native_height;     // e.g. 224, 272, 480, 1080
uint32_t fb_stride = fb_width * 2;          // RGB565: 2 bytes per pixel
uint32_t fb_base   = FB_BASE_OFFSET;        // 0x2A000000 (front buffer)

// 2. Allocate / re-mmap the region if needed
void* fb_ptr = shmem_map(fb_base, fb_stride * fb_height);

// 3. Write the new geometry to a DDR3 control word that the RTL reads
//    (this is the OpenBOR ctrl word — the existing 0x3A000000 mechanism)
*ctrl_fb_width  = fb_width;
*ctrl_fb_height = fb_height;
*ctrl_fb_stride = fb_stride;
*ctrl_fb_base   = fb_base;
*ctrl_fb_format = 0x04;                     // 5'b00100 = 16bpp RGB565
*ctrl_fb_enable = 1;
```

The RTL side (`OpenBOR.sv`) reads these control words on `clk_sys` and drives
the `FB_*` outputs accordingly. ASCAL re-detects the new geometry and
re-computes its scaling coefficients automatically.

### 3.2 Steady-state per-frame writes

```c
// Engine's video_copy_screen — already patched to call this hook
// (see [[direct-write-required-for-fps]] for the existing pattern)
void NativeVideoWriter_WriteFrame(uint8_t *src, int w, int h, int pitch, int bpp, uint32_t *palette) {
    // Pick next buffer (triple-buffer rotation)
    int next_buf = (fb_active_buf + 1) % 3;
    uint16_t *dst = (uint16_t*)(fb_ptr + next_buf * BUF_BYTES);

    // Convert 32bpp engine output → RGB565 (or just memcpy if engine is already 16bpp)
    // The anisotropic-NN squish step from the previous architecture goes away —
    // ASCAL handles all rescaling.
    convert_to_rgb565(src, dst, w, h, pitch);

    // Atomic publish: update FB_BASE to point at this buffer
    *ctrl_fb_base = FB_BASE_OFFSET + next_buf * BUF_BYTES;
    fb_active_buf = next_buf;
}
```

### 3.3 Sync to ASCAL scanout

`FB_VBL` pulses high during ASCAL's vblank. The ARM can poll a DDR3 control
word that the RTL latches from FB_VBL to know when it's safe to swap buffers.
**Not strictly required** for triple-buffer mode (ASCAL just reads whatever
`FB_BASE` points at on the next frame), but useful for double-buffer setups or
to avoid tearing on slower hot-swap operations.

```c
// Optional: wait for next vblank before publishing
while (!(*ctrl_fb_vbl_count_change)) { usleep(100); }
*ctrl_fb_base = new_base_addr;
```

### 3.4 Keepalive thread interaction

The existing keepalive thread (per `[[fpga-keepalive-thread]]`) bumps a frame
counter every 150ms to prevent ASCAL/reader staleness blank-out. **Under the
FB+ASCAL architecture, this is mostly unnecessary** — ASCAL re-reads
`FB_BASE`'s region every output frame regardless of whether `FB_BASE` itself
changes. The only staleness concern is the engine's pause-screen / wait-for-PAK
windows where no new frame data lands. Keep the keepalive thread, point it at
the last-written buffer (matching existing rule), and have it bump a heartbeat
counter so RTL can decide whether to assert `FB_FORCE_BLANK` if writes stop
for >2s (engine crash / hung handler scenario).

---

## 4. Top-level wiring example (from `ao486_MiSTer/ao486.sv:502-531`)

This is the canonical wire-up showing how an emu module drives the FB_*
outputs. ao486 builds the values from internal VGA state; OpenBOR will build
them from the engine-reported control words.

```systemverilog
// FB_* state registers in the emu module
reg         fb_en;
reg  [31:0] fb_base;
reg  [11:0] fb_height;
reg  [11:0] fb_width;
reg  [13:0] fb_stride;
reg   [4:0] fb_fmt;
reg         fb_off;

// Drive from internal state (ao486 builds from vga_* signals; OpenBOR
// builds from DDR3 control-word reads)
always @(posedge clk_sys) begin
    fb_en     <= ctrl_fb_enable_q;
    fb_base   <= ctrl_fb_base_q;     // e.g. 32'h2A000000
    fb_width  <= ctrl_fb_width_q;    // e.g. 12'd320
    fb_height <= ctrl_fb_height_q;   // e.g. 12'd224
    fb_stride <= ctrl_fb_stride_q;   // e.g. 14'd640 (320 × 2 for RGB565)
    fb_fmt    <= ctrl_fb_format_q;   // e.g. 5'b00100 (16bpp RGB565)
    fb_off    <= ctrl_fb_blank_q;
end

// Tie to emu interface outputs
assign FB_EN          = fb_en;
assign FB_BASE        = fb_base;
assign FB_FORMAT      = fb_fmt;
assign FB_WIDTH       = fb_width;
assign FB_HEIGHT      = fb_height;
assign FB_STRIDE      = fb_stride;
assign FB_FORCE_BLANK = fb_off;
```

### 4.1 Required QSF additions

```tcl
# Enable the framebuffer signal block in emu_ports.vh
set_global_assignment -name VERILOG_MACRO "MISTER_FB=1"
# Do NOT define MISTER_FB_PALETTE — OpenBOR uses 16/32-bit direct modes
```

### 4.2 VIDEO_ARX / VIDEO_ARY override

ao486 demonstrates the canonical pattern for letting ASCAL pick the aspect
ratio automatically when FB is in use (`ao486.sv:545-570`):

```systemverilog
wire [12:0] fb_arx, fb_ary;

video_scale_int fb_scale (
    .*,
    .hsize(fb_width),
    .vsize(fb_height),
    .arx_o(fb_arx),
    .ary_o(fb_ary)
);

// When FB is active, derive aspect from framebuffer dimensions
// When inactive, use the core's CONF_STR-declared aspect
assign VIDEO_ARX = fb_en ? fb_arx : core_arx;
assign VIDEO_ARY = fb_en ? fb_ary : core_ary;
```

For OpenBOR, also expose the standard MiSTer OSD aspect-ratio entries (per
roadmap #1 in CLAUDE.md Section 6e) so the user can pick Original / Full
Screen / [ARC1] / [ARC2] — these override the auto-derived ARX/ARY.

---

## 5. Variable-resolution behavior

This is the critical capability that drove the abandonment of Option Y.
OpenBOR PAKs author at radically different native resolutions: Batman 320×240,
PDC2 480×272, He-Man 960×480, Lust Rush 1920×1080. Under Option Y, every PAK
had to be downscaled to the FPGA's fixed 320×224 video timing — which
sacrificed quality for hi-res PAKs and required complex edge-aware
downscale RTL.

### 5.1 What ASCAL does on resolution change

When the ARM updates `FB_WIDTH` / `FB_HEIGHT` / `FB_BASE` (e.g. on PAK hot
swap from Batman 320×240 → He-Man 960×480):

1. ASCAL detects the geometry change on the next vblank.
2. Internal triple-buffer triple-rotates one extra time to flush old-geometry
   pixels.
3. New scaling coefficients are computed automatically — both H and V
   independently. For 960×480 → 1920×1080 HDMI: H scale = 2.0×, V scale = 2.25×.
4. The output HDMI timing does NOT change — ASCAL keeps producing the same
   output resolution. It's the input that varies; output is fixed by the
   core's HDMI timing PLL (typically 1920×1080 @ 60Hz for modern displays, but
   anything the user has configured via MiSTer.ini).
5. `swblack` (controlled by the core; sys_top wires it to `hdmi_blackout`)
   determines whether 3 black frames are emitted during the change to suppress
   the visible glitch. Default behavior is yes — 3 black frames is much less
   jarring than the partial-frame tearing that would otherwise occur.

### 5.2 Recommended OpenBOR change handling

```c
void on_pak_resolution_change(int new_w, int new_h) {
    // 1. Pause engine briefly (or rely on the natural pause between PAKs)
    // 2. Assert FB_FORCE_BLANK by writing the ctrl word
    *ctrl_fb_blank = 1;

    // 3. Wait 2-3 ASCAL frames (~50ms is plenty)
    usleep(50000);

    // 4. Update geometry
    *ctrl_fb_width  = new_w;
    *ctrl_fb_height = new_h;
    *ctrl_fb_stride = new_w * 2;       // RGB565
    *ctrl_fb_base   = FB_BASE_OFFSET;  // reset to front buffer

    // 5. Reallocate framebuffer region if size grew significantly
    if (new_w * new_h * 2 > current_buf_size) {
        munmap(fb_ptr, current_buf_size * 3);
        fb_ptr = shmem_map(FB_BASE_OFFSET, new_w * new_h * 2 * 3);
        current_buf_size = new_w * new_h * 2;
    }

    // 6. Write one black frame to the new buffer
    memset(fb_ptr, 0, current_buf_size);

    // 7. Release blank
    *ctrl_fb_blank = 0;
}
```

The triple-buffer + atomic FB_BASE swap ensures no tearing during normal
gameplay. Resolution changes are guarded by the explicit blank window above.

### 5.3 Resolution range supported by ASCAL

- Input: up to `IHRES = 2048` pixels wide (configurable generic in
  `Template_MiSTer/sys/sys_top.v:744`). 1920×1080 native fits.
- Output: up to `OHRES = 2304` (default), 4096 max. Covers every realistic
  HDMI output up to 4K downsampled to 1080p.
- Aspect handling: independent H/V scale factors mean non-square PAR is
  free. 480×272 (16:9) and 320×240 (4:3) both squish/stretch to whatever
  output AR the user picks via OSD.

---

## 6. HDMI output scaling math

### 6.1 What determines output resolution

The core's HDMI PLL (`pll_hdmi`) and timing parameters (`HEIGHT`, `WIDTH`,
`HFP`, `HBP`, `HS`, `VFP`, `VBP`, `VS` — all wired into ASCAL at
`sys_top.v:780-790`) determine the output resolution. These come from the
user's `MiSTer.ini`:

- `video_mode = 1920,1080,60` etc. — explicit modeline
- `video_mode = N` — preset index (0=auto, etc.)
- HDMI EDID auto-detection (if `video_mode` is unset)

ASCAL takes the input framebuffer at `FB_WIDTH × FB_HEIGHT` and produces a
scaled image at this output resolution every output frame. The scaling math
is independent H/V accumulators (one per axis), letting any input × any
output combination work.

### 6.2 Scaling mode selection

`MiSTer.ini` parameters that affect ASCAL behavior:

| INI parameter | What it controls |
|---|---|
| `vsync_adjust` | 0 = pure scale (free-run), 1 = adapt output rate to input rate, 2 = adjust output PLL to match input (low-latency mode — sets FB_LL) |
| `vscale_mode` | 0 = normal (any-to-any), 1 = V integer scale, 2 = HV integer scale narrower, 3 = HV integer scale wider |
| `hdmi_limited` | Color range — affects output, not scaling math |
| `vrr_mode` | 0 = disabled, 1/2/3 = VRR / FreeSync output modes |

Per [`[[per-core-feature-matrix]]`](../../CLAUDE.md), MiSTer's roadmap #3
"Scale Mode" OSD entry exposes vscale_mode 0-3 to the user via the core's
OSD. This is a CONF_STR addition + `status[N:M]` bit routing — doesn't change
the FB_* wiring at all.

### 6.3 Aspect ratio handling

Three layers (CLAUDE.md Section 6e roadmap #1):

1. **Original (4:3 for Sega CD region)**: ASCAL takes VIDEO_ARX/VIDEO_ARY
   and adds pillar-/letterbox bars to maintain the declared AR on the actual
   output panel.
2. **Full Screen**: ASCAL ignores AR and stretches to fill the output panel
   completely.
3. **[ARC1] / [ARC2]**: ASCAL uses the user's `aspect_ratio_1=` /
   `aspect_ratio_2=` from MiSTer.ini.

For OpenBOR, declare `VIDEO_ARX = 4, VIDEO_ARY = 3` (Sega CD reference) when
ARX/ARY auto-derivation from FB dimensions isn't desired — i.e. respect the
existing NTSC region match per `[[ntsc-region-match]]`.

### 6.4 CRT positioning (H ±3 / V ±3) under FB+ASCAL

Per CLAUDE.md Section 6e the cores have OSD H/V position controls. Under the
FB path, these still work — they adjust ASCAL's `hmin`/`hmax`/`vmin`/`vmax`
ports (the "active region within output frame" controls), which shifts the
scaled image left/right/up/down within the output panel.

---

## 7. ARM-side LFB protocol (alternative: skip RTL FB_* entirely)

`Main_MiSTer/video.cpp:3288-3340` shows a **completely different path**:
the ARM uses SPI cmd `UIO_SET_FBUF = 0x2F` to write the FB_* parameters
directly into `sys_top.v`'s LFB registers (`sys_top.v:430-462`). This
bypasses the core's RTL FB_* wiring entirely — `sys_top.v:853-868` muxes
between the LFB registers and the emu's FB_* outputs based on which is
asserted.

### 7.1 When LFB is the right choice

- Main_MiSTer uses LFB to overlay the menu UI on top of any core. The core's
  RTL FB doesn't need to know about it.
- For OpenBOR specifically, **LFB is not the right path** — we want the
  core's RTL to own the framebuffer because it's the engine's actual video
  output, not a UI overlay. The engine needs to coordinate buffer swaps
  with its own render timing, which LFB-from-ARM can't do without a
  round-trip latency for every frame.

### 7.2 LFB cmd 0x2F protocol (for reference / debugging)

```
spi_uio_cmd_cont(UIO_SET_FBUF)
spi_w(FB_EN | FB_FMT_RxB | FB_FMT_8888)  // word 0: flags
spi_w(fb_addr & 0xFFFF)                  // word 1: base address [15:0]
spi_w(fb_addr >> 16)                     // word 2: base address [31:16]
spi_w(fb_width)                          // word 3: width
spi_w(fb_height)                         // word 4: height
spi_w(xoff)                              // word 5: scaled left (output positioning)
spi_w(xoff + width - 1)                  // word 6: scaled right
spi_w(yoff)                              // word 7: scaled top
spi_w(yoff + height - 1)                 // word 8: scaled bottom
spi_w(fb_width * bpp)                    // word 9: stride

// Format constants (Main_MiSTer/video.h:46-52):
//   FB_FMT_565   0b00100
//   FB_FMT_1555  0b01100
//   FB_FMT_888   0b00101
//   FB_FMT_8888  0b00110
//   FB_FMT_PAL8  0b00011
//   FB_FMT_RxB   0b10000     (RGB→BGR bit)
//   FB_EN        0x8000
```

This path is useful for ARM-side diagnostic overlays (e.g. "loading PAK..."
text) since it works without recompiling the RBF. **Not the primary OpenBOR
video path** — but worth understanding because the menu UI uses it and the
core's FB_EN OR's with LFB_EN inside sys_top.

---

## 8. Comparison vs abandoned Option Y architecture

| Concern | Option Y (abandoned, 2026-06-04) | FB+ASCAL (this doc) |
|---|---|---|
| **Input geometry** | Fixed 320×224 — all PAKs squished to this | Variable per-PAK (320×240, 480×272, 960×480, 1920×1080) |
| **ARM-side downscale** | Required — anisotropic-NN squish in `WriteFrame` | Not required — ASCAL handles all rescaling |
| **RTL complexity** | Custom DDR3 video reader + H-pass FIFO + edge-aware NN/bilinear downscale module (~6 RTL files, multiple CDC paths, audited across 6 cycles) | Stock framework — just wire FB_* outputs from a state register block |
| **HDMI output res** | Fixed Sega CD NTSC 320×224 @ 59.92Hz | User-configurable via MiSTer.ini, automatic adapt-to-display |
| **Quality at hi-res** | Lossy — He-Man 960×480 → 320×224 via 3× downscale always | Native — ASCAL upscales 960×480 → 1920×1080 with polyphase or bilinear |
| **Quality at low-res** | Engine renders at native, ARM downscales | Engine renders at native, ASCAL upscales — same end result, less ARM CPU |
| **CDC paths** | clk_sys (98MHz) ↔ clk_vid (53MHz) for reader_frame_ready + new_frame + per-line strobes — required 5-cycle pulse-width widening to survive 2-FF sync (Option Y cycle 5 concern F) | None added by FB_* — ASCAL's avl_clk runs at clk_100m, all FB_* outputs are pure clk_sys → clk_sys (sys_top resynchronizes internally) |
| **Resolution changes** | Required RBF recompile for different fixed timing | Runtime — ARM just updates `FB_WIDTH`/`FB_HEIGHT`/`FB_BASE` ctrl words |
| **OSD aspect-ratio / scale-mode / V-crop integration** | Worked around — features rolled into custom RTL | Works out-of-box — these are sys/ framework features that operate on the post-ASCAL output |
| **Power-of-2 buffer alignment** | Required for the ring-buffer reader | Required for FB_BASE alignment too — same constraint, different reason |
| **Per-PAK bring-up testing** | Required hardware verification per resolution band (320×240 / 480×272 / 960×480 / 1920×1080) — each in its own audit cycle | Single FB protocol covers all — bring-up testing is "does it scale correctly at each input W×H" which is ASCAL's responsibility |
| **Framework parity** | Diverged from ao486/Genesis/NES/SNES — custom video path | Matches ao486 (ao486 uses FB for hi-res VESA modes specifically because its native VGA can't drive 1920×1080) — well-trodden code path |

### 8.1 Migration plan summary

1. **Phase 1** — Strip out Option Y RTL: delete the custom DDR3 reader,
   H-pass FIFO, edge-aware downscale, frame-counter staleness watchdog.
   Replace with a small ctrl-word-to-FB_* register block.
2. **Phase 2** — Update `OpenBOR.sv` top-level: add `MISTER_FB=1` QSF macro,
   wire FB_EN/FB_FORMAT/FB_WIDTH/FB_HEIGHT/FB_BASE/FB_STRIDE/FB_FORCE_BLANK,
   compute VIDEO_ARX/VIDEO_ARY via `video_scale_int` when fb_en.
3. **Phase 3** — Add a DDR3 ctrl-word region at `0x3A000000` (existing
   OpenBOR ctrl region) extended with FB_* params. RTL polls ctrl on
   clk_sys, registers into the FB_* output registers.
4. **Phase 4** — ARM-side `NativeVideoWriter` rewrite: drop the anisotropic
   squish, write engine output directly into the 0x2A000000 region in
   RGB565 (or RGB8888) at the PAK's native resolution. Update ctrl word
   on PAK resolution change.
5. **Phase 5** — Hardware verify against the existing test PAK matrix
   (ATOV 320×240, TMNT-RP, Avengers UBF 480×272, He-Man 960×480, Lust Rush
   1920×1080). Note: per [[openbor-7533-engine-hardening-2026-05-28]] the
   v3.10 palette pipeline is locked — do not modify engine-side palette
   logic during this video path migration.
6. **Phase 6** — Re-evaluate keepalive thread necessity (likely keep as
   heartbeat-based crash detector rather than staleness-prevention). Re-run
   full audit per [[iterative-audit-until-zero-concerns-for-major-arch]].

---

## 9. Key reference URLs

- `Template_MiSTer/sys/emu_ports.vh` — canonical FB_* port declaration
- `Template_MiSTer/sys/sys_top.v:430-462` (LFB write registers)
- `Template_MiSTer/sys/sys_top.v:714-820` (ASCAL instantiation)
- `Template_MiSTer/sys/sys_top.v:845-868` (LFB ↔ emu FB mux)
- `Template_MiSTer/sys/ascal.vhd` — the scaler itself
- `ao486_MiSTer/ao486.sv:502-531` — reference emu-side FB_* wiring
- `Main_MiSTer/video.h:36-52` — ARM-side FB constants
- `Main_MiSTer/video.cpp:3288-3340` — ARM-side LFB write canonical pattern
- `Main_MiSTer/user_io.h` — `UIO_SET_FBUF = 0x2F` command

End of document.
