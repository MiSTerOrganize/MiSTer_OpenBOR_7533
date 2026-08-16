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
| **`ascal` scaler (`vbuf`, 128-bit port)** | `0x2000_0000` | **synthesis generic** `RAMBASE` | output-resolution dependent — **not bounded here** | `sys/sys_top.v:717` |
| **Core window (`ram1` via `DDRAM_*`)** | `0x3A00_0000` | compile-time in ARM + RTL | control/joysticks `+0x00…0x2F`, **BUF0 `+0x40`**, **BUF1 `+0x4_0040`** (143,360 B each), **cart data `+0x8_0000`** | `src/native_video_writer.c:8-16,56` |
| **`ddr_svc` ALSA channel (`ram2`)** | `alsa_address` | 🛑 **RUNTIME, programmed by the HPS** | — | `sys/sys_top.v:680,1668` |
| **`ddr_svc` palette channel (`ram2`)** | `LFB_BASE − 4096 B` | 🛑 **RUNTIME** (`pal_addr <= LFB_BASE[31:3] − 29'd512`) | 4 KB | `sys/sys_top.v:687,1026-1031` |
| Legacy `ddram` master | shares `ram1`, muxed | — | SDRAM-clear only; idle once `use_nv` | `fpga/OpenBOR.sv:500-543` |

### 🛑 Two findings that change how an arena may be placed

**(a) Not every consumer is fixed at synthesis.** `LFB_BASE` is written at runtime by the
HPS (`sys_top.v:443-444`, `reg [31:0] LFB_BASE = 0`), and both `pal_addr` and the linux
framebuffer follow it. **A Tier-B arena cannot be placed by reading the RTL alone** — a
statically-safe-looking address can be handed to the HPS later. Any chosen base needs either
a runtime check that `LFB_BASE` is outside it, or placement in a region the HPS framebuffer
path provably never programs.

**(b) The core window has an UNBOUNDED tail.** Cart data starts at `+0x8_0000` and its
length is the content size. For OpenBOR the PAK is mounted via `.s0` and read from SD, so
this region is largely vestigial *on this core* — but the extent is **not stated anywhere**,
and an arena placed above `0x3A00_0000` on the assumption that the window is ~1 MB would be
guessing. **Bound it before allocating above it.**

## 3. What is actually free — and the honest gap

Two candidate regions inside the 513 MB:

| Region | Size | Verdict |
|---|---|---|
| `0x1FF0_0000 – 0x1FFF_FFFF` | 1 MB | too small for the arena (needs 45 MB on He-Man, 150 MB library max) |
| `0x2000_0000 – 0x39FF_FFFF` | 416 MB | **contains `ascal` at its base.** Free only above ascal's unbounded extent |
| `0x3A00_0000 – 0x3FFF_FFFF` | 96 MB | **contains the core window**, whose tail is unbounded |

🛑 **So item 9 is CLOSED for the split and the consumer list, and NOT closed for extents.**
Two measurements remain, and both are small:

1. **`ascal`'s framebuffer extent** at the resolutions this core actually outputs.
2. **The core window's tail** — how far past `0x3A08_0000` cart data can reach on OpenBOR.

Until those land, the arena has **no defensible base**, and neither does BUF2. That is the
same conclusion §6 already carried — but now with the specific unknowns named, rather than
the whole table marked provisional.

## 4. What this unblocks, and what it does not

**Unblocked:** the size of the playing field (513 MB, not 256 MB), the consumer list, and
the knowledge that two of the four are runtime-programmed.

**Still blocking Phase 2's memory map:** the two extents above. **BUF2 stays folded into this
item** (register 7a) — it is 143,360 B and trivially placeable *once a base is defensible*,
and not before.

🛑 **Do not close item 9 in the register until both extents are measured.** Recording the
split as "item 9 done" would repeat the exact failure the round-5/6 reviews kept finding: a
correction narrated but not made.
