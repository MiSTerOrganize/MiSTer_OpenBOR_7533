# Tier-B register item 9 — the DDR3 consumer enumeration (2026-08-15)

**Closes the keystone Phase 2 gate.** Every base address in the design doc's §6 was marked
*"provisional until M11's enumeration"*, and BUF2 (register 7a) had nowhere to live because
of it. This is that enumeration.

Sources are named per row. **Nothing here is inferred from another document** — the
authority for the Linux/FPGA split turned out to be the kernel command line, which no prior
revision had read.

---

## 1. The split — from the kernel cmdline, not from `/proc/iomem`

Read off the running device 2026-08-15:

```
console=ttyS0,115200 loglevel=4 loop.max_part=8 mem=511M memmap=513M$511M ...
/proc/device-tree/memory/reg = 00 00 00 00 40 00 00 00     -> base 0x0, size 0x4000_0000
```

| | |
|---|---|
| **Total DDR3** | **1 GB** — `0x0000_0000 – 0x3FFF_FFFF` (device tree) |
| **Linux owns** | `0x0000_0000 – 0x1FEF_FFFF` — **511 MB** (`mem=511M`; `MemTotal 504,080 kB` agrees) |
| **Withheld from Linux** | `0x1FF0_0000 – 0x3FFF_FFFF` — **513 MB** (`memmap=513M$511M`, the `$` form = reserved) |
| reserved-memory DT nodes | **none** — the carve-out is done entirely by cmdline |

🛑 **This CONFIRMS the doc's §9.9.4 figure (~513 MB above `0x1FF0_0000`) and supersedes its
derivation.** §9.9.4 reached it by inference from `/proc/iomem`; `memmap=513M$511M` states it
exactly. It also re-confirms that `ddram.sv`'s *"256MB at the end of 1GB"* comment is wrong —
the window is twice that and starts far lower.

## 2. Who is already in the 513 MB

| Consumer | Base | Fixed how | Extent | Source |
|---|---|---|---|---|
| **`ascal` scaler (`vbuf`, 128-bit port)** | `0x2000_0000` | synthesis generic `RAMBASE` | **24 MB — `0x2000_0000 – 0x217F_FFFF`** (§5.1) | `sys/sys_top.v:717`, `sys/ascal.vhd:1689` |
| **Core window (`ram1` via `DDRAM_*`)** | `0x3A00_0000` | compile-time in ARM + RTL | **896 KB — `0x3A00_0000 – 0x3A0D_FFFF`** (§5.2) | `src/native_video_writer.c:86-96`, `fpga/rtl/openbor_video_reader.sv:144-157` |
| **`ddr_svc` ALSA channel (`ram2`)** | `alsa_address` | 🛑 **RUNTIME, programmed by the HPS** | — | `sys/sys_top.v:680,1668` |
| **`ddr_svc` palette channel (`ram2`)** | `LFB_BASE − 4096 B` | 🛑 **RUNTIME** (`pal_addr <= LFB_BASE[31:3] − 29'd512`) | 4 KB | `sys/sys_top.v:687,1026-1031` |
| Legacy `ddram` master | shares `ram1`, muxed | — | SDRAM-clear only; idle once `use_nv` | `fpga/OpenBOR.sv:500-543` |

### 🛑 Findings that change how an arena may be placed

**(a) Not every consumer is fixed at synthesis.** `LFB_BASE` is written at runtime by the
HPS (`sys_top.v:443-444`, `reg [31:0] LFB_BASE = 0`), and both `pal_addr` and the linux
framebuffer follow it. **A Tier-B arena cannot be placed by reading the RTL alone** — a
statically-safe-looking address can be handed to the HPS later. Any chosen base needs either
a runtime check that `LFB_BASE` is outside it, or placement in a region the HPS framebuffer
path provably never programs. **This is unchanged by §5 and is still the one open hazard.**

**(b) ~~The core window has an UNBOUNDED tail.~~ RESOLVED 2026-08-16 — see §5.2.** It is
bounded, at `0x3A0E_0000`, by a consumer this table did not previously list: the **audio ring**.
What is genuinely unbounded is a *different* thing — the cart WRITE path in RTL — and it is
dormant on this core. Both are stated precisely in §5.2.

## 3. What is actually free

| Region | Size | Verdict |
|---|---|---|
| `0x1FF0_0000 – 0x1FFF_FFFF` | 1 MB | too small for the arena (45 MB on He-Man, 150 MB library max) |
| **`0x2180_0000 – 0x39FF_FFFF`** | **392 MB** | ✅ **the arena goes here.** Above ascal's measured 24 MB, below the core window. Bounded on both sides by fixed, synthesis-time consumers |
| `0x3A0E_0000 – 0x3FFF_FFFF` | 95 MB | ⚠️ **do not use.** Big enough, but it sits directly above the dormant unclamped cart writer (§5.2) — the one region where an overrun lands silently |

## 4. Status of register item 9

**CLOSED for the split, the consumer list, AND both extents** (§5). BUF2 (register item 7a)
is unblocked: 143,360 B inside the 392 MB region.

**Still open, and NOT part of item 9:** finding (a). `LFB_BASE` is runtime-programmed, so
whatever base Phase 2 picks needs a runtime assertion that the HPS framebuffer is outside it.
That is a Phase 2 design obligation, not a measurement.

## 5. The two extents, measured (2026-08-16)

### 5.1 `ascal` — 24 MB, and resolution-INDEPENDENT

The map previously recorded this as *"output-resolution dependent — not bounded here"*. It is
neither dependent nor unbounded:

| Fact | Value | Source |
|---|---|---|
| `RAMSIZE` (per framebuffer) | `0x0080_0000` = **8 MB** | `sys/ascal.vhd:118` default; `sys_top.v:719-723` selects it because **`MISTER_SMALL_VBUF` is not defined** for this core (`OpenBOR.qsf` defines only `MENU_CORE=1`, `OPENBOR_CORE=1`) |
| Buffer count | **3** | `buf_next()` ranges `0 TO 2` (`ascal.vhd:429-435`) |
| Placement | `b=0 → base`, `b=1 → base+size`, `b=2 → base+2·size` | `buf_offset()` (`ascal.vhd:436-443`) |
| **Extent** | **`0x2000_0000 – 0x217F_FFFF` (24 MB)** | `RAMBASE + 3 × RAMSIZE` |

🛑 **Why resolution cannot push past it:** `avl_wadrs <= i_wadrs AND (RAMSIZE - 1)`
(`ascal.vhd:1689`) **masks the write address into the 8 MB buffer**. An output too large for a
buffer wraps *inside* it; it cannot run off the end. So this bound holds at every resolution
the core can emit — no per-mode measurement is needed, which is why the original
"measure it at the resolutions this core outputs" framing was the wrong question.

🛑 **The `o_fb_base` path does not widen it.** `ascal.vhd:1704-1718` substitutes the runtime
`o_fb_base` for `avl_o_offset*` when `fb_ena=1` — but that is the **read** side. The **write**
side, `avl_i_offset0/1` (`:1720-1721`), goes through `buf_offset()` unconditionally. Only a
write can corrupt an arena, so the 24 MB bound is unconditional.

### 5.2 Core window — 896 KB in use, plus one dormant unclamped writer

**The table in §2 was missing a consumer.** There is an **audio ring** above the cart data,
and it is what actually terminates the window:

| Region | Address | Size |
|---|---|---|
| control / replay / joysticks / audio ptrs | `0x3A00_0000 – 0x3A00_003F` | 64 B |
| BUF0 | `0x3A00_0040` | 143,360 B |
| BUF1 | `0x3A04_0040` | 143,360 B |
| cart data | `0x3A08_0000` | 320 KB slot; reads clamped to **256 KB** |
| **audio ring** | **`0x3A0D_0000`** | **64 KB**, hard-masked |
| **tail** | **`0x3A0E_0000`** | **total 896 KB** |

- Audio ring: `AUDIO_RING_ADDR = 29'h0741A000`, `AUDIO_RING_BYTES = 32'h0001_0000`,
  `AUDIO_RING_MASK = 32'h0000_FFFF` (`openbor_video_reader.sv:155-157`). Every pointer update
  is `& AUDIO_RING_MASK` (`:354,359,785`), so it cannot leave its 64 KB.
- Cart data: `NV_CART_MAX_SIZE = 0x0004_0000` is **enforced twice** — rejected at
  `native_video_writer.c:1677`, clamped at `:1686` — and the ARM only ever `memcpy`s **out**
  of the region (`:1687`). It never writes there.

🛑 **The one genuinely unbounded thing, and why it is not a bound on the window today.**
The RTL cart writer is
`cart_write_addr <= CART_DATA_ADDR + {2'd0, ioctl_addr[26:3]}` (`openbor_video_reader.sv:470`)
with **no length check and no `ioctl_index` gate** — it is armed by bare `ioctl_download`
(`:444`). `ioctl_addr` is 27 bits, so a stream larger than the 320 KB slot would run through
the audio ring and onward, up to `0x3A08_0000 + 128 MB`.

**It is dormant on this core.** `CONF_STR` declares exactly one file entry, `"SC0,PAK,Load
PAK;"` (`OpenBOR.sv:255`) — an **S**-type *mounted image*, read from SD by the ARM via `.s0`.
There is **no F-entry**, so `ioctl_download` never asserts for content, and the ARM-side read
path is vestigial exactly as §2(b) originally suspected.

**So it does not shrink the window — it disqualifies a region.** The 95 MB above
`0x3A0E_0000` is the blast radius of this writer if anything ever arms it (an F-entry added
later, or any other framework use of `ioctl_download` — the missing `ioctl_index` gate means
it does not discriminate). The arena goes below it instead, where 392 MB is available and
nothing dormant points at it. **Fixing the writer is not a prerequisite for Phase 2**; placing
the arena out of its reach is, and §3 does that.

### What was verified vs inferred

Every value above is read from source at the cited line — the generics, the buffer count, the
mask, the enforcement sites, and the CONF_STR entry. **Nothing here is measured on hardware.**
That is adequate for these two, because both are synthesis-time constants and a mask; it would
NOT be adequate for finding (a), which is runtime and stays open.
