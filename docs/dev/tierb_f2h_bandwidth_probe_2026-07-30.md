# Tier-B GO/NO-GO gate — measured f2h DDR3 read bandwidth (2026-07-30)

`OPTION4_FPGA_BLEND_OFFLOAD_SCOPE.md` §4 scoped this as *"the single most important
deliverable of scoping"* and it had never been run. Register item 0 — the **433 MB/s
conservative ceiling** that §15 uses as its only hard bandwidth gate — had no derivation
anywhere in the workspace or in git history.

**It is now measured. 433 is not a ceiling.**

## Harness

| piece | what |
|---|---|
| `fpga/rtl/tierb_bw_probe.sv` | read-only Avalon master. 16 steps = 8 burst lengths {1,2,4,8,16,32,64,128 beats} x {sequential, scattered}. 2^24-cycle (170.4 ms) measured window per step, followed by an equal idle gap so the machine stays usable. Strictly single-outstanding: issue, drain every beat, issue again — the protocol `openbor_video_reader.sv` already proves works on this bridge. Publishes 49 qwords at `0x3A0F0000`, resweeping every ~5.5 s |
| `fpga/rtl/tierb_ddr_arb.sv` | strict-priority arbiter. Video reader = port A, absolute priority; probe = port B, fills the idle cycles |
| `tools/harness/read_bw_probe.sh` | on-device `devmem` decoder |

Traffic is the low 512 KiB of the core's own DDR3 window — reads only, so it cannot corrupt
anything, and it is realistic: that is the memory the compositor would be fetching.
Scattered addresses are 64 B aligned via a 32-bit LFSR.

RBF: `PROBE_OpenBOR_7533.rbf`, deployed locally only, **never committed** (per
`[[never-push-test-rbf-or-arm-binary]]`). Named off the `OpenBOR_7533` prefix deliberately
so it cannot hijack the existing `.mgl` files, which resolve `<rbf>` by prefix.

Timing with the probe in: **pll_hdmi 0.271 ns** (baseline was 0.128), clk_sys 1.785,
clk_pix 8.171, all positive, TNS 0.000, SEED 3 unchanged. Cost 9,014 ALMs (22%, was 20%)
and **zero** extra M10K.

## Results

Theoretical port peak = 64 bit x 98.4375 MHz = **787.5 MB/s**.

| burst (beats) | bytes | **idle** seq | **idle** scat | **He-Man loaded** seq | **He-Man loaded** scat |
|---:|---:|---:|---:|---:|---:|
| 1 | 8 | 49.0 | 44.9 | 41.5 | 38.5 |
| 2 | 16 | 94.3 | 85.7 | 80.2 | 74.1 |
| 4 | 32 | 171.0 | 152.8 | 143.8 | 133.6 |
| 8 | 64 | 278.2 | 252.6 | 239.3 | 226.9 |
| 16 | 128 | 401.2 | 379.9 | 357.6 | 343.3 |
| 32 | 256 | 529.7 | 503.7 | 483.3 | 476.1 |
| 64 | 512 | 631.0 | 606.4 | 591.2 | 586.0 |
| 128 | 1024 | **694.6** | **679.1** | **663.1** | **660.7** |

MB/s. "idle" = probe core loaded, no PAK (ARM polling `.s0`). "He-Man loaded" = the 960x480
PAK mounted and the engine running. **0 swallows in all 32 windows**, and `beats/burst`
equalled the requested burst length exactly in every one — the bridge is well-behaved under
single-outstanding bursts and the measurement is not truncated anywhere.

**The "loaded" column was independently re-measured with the engine PROVEN live**, because
the first run had only checked that the ARM *process* existed — which is not the same thing,
and a later attempt showed why (see the `.s0` note below). Second run, with the frame
counter advancing (+1139 ticks in 10 s) and the audio ring full at the moment of sampling:
seq 128 = **669.5**, scat 128 = **664.7**, every other cell within ~1% of the table above.
The figures stand.

🛑 **The strict-priority arbiter demonstrably protects both video AND audio.** With the probe
holding **85% of the port**, the DDR3 audio ring (`0x3A000030`/`0x3A000038`, 64 KiB = 341 ms
at 48 kHz stereo) stayed **90%+ full**: avg **59,285 B** on the probe core against **59,916 B**
measured on the shipping core with the same PAK — a 1.1% difference, i.e. no underrun and no
measurable degradation. Video never dropped a frame either. This is direct empirical evidence
for the phase-1c arbiter design: a second master can saturate this port without disturbing
the reader, provided the reader keeps absolute priority and the probe's bursts stay short
enough (<= 128 beats = 1.3 us) that the reader's per-line slack absorbs them.

## What it says

1. 🛑 **433 MB/s is not a ceiling, and it never was one.** The port sustains **663 MB/s
   with a PAK running** — 84% of theoretical. 433 is roughly what this port delivers at a
   ~20-beat burst; it is a point on a curve, not a limit.

2. 🛑 **Burst length is the entire story — a 16x spread.** 41.5 MB/s at burst 1 rising to
   663 MB/s at burst 128. Everything else in this measurement is a rounding error next to
   it.

3. **Scatter is nearly free.** 660.7 vs 663.1 at burst 128 (0.4%), 226.9 vs 239.3 at burst
   8 (5%), 38.5 vs 41.5 at burst 1 (7%). So DDR3 row locality is **not** what governs this
   port — **latency amortisation is**. That is good news the design did not assume: the
   compositor's scattered per-sprite row fetches pay almost nothing for being scattered.
   They pay for being *short*.

4. **A9 contention is small.** Loading a PAK and running the engine costs ~5% at long
   bursts (694.6 -> 663.1) and ~15% at burst 1. And this over-states it for Tier-B: under
   the offload the ARM stops compositing, so its DDR3 load falls rather than rises.

5. **The video reader costs 1.4%**, measured as the probe's stall fraction. Section 8's
   derived figure is 0.813 us of bus per 63.7 us scanline = **1.28%**. Independent
   confirmation of an assumption the document had only computed.

## Verdict on the gate: PASS, conditional on burst length

Against §14.4.5's port loads (all terms counted):

| | needs | first burst that clears it (scattered, PAK loaded) |
|---|---:|---|
| He-Man | ~122.6 MB/s | **4 beats / 32 B** (133.6) |
| worst PAK (Ninja) | ~362.7 MB/s | **32 beats / 256 B** (476.1); 16 beats gives 343.3 and does **not** clear it |

**So the bandwidth premise holds — but it converts into a hard constraint on the fetch
engine that this document did not previously state:**

> 🛑 **The RLE fetch engine must issue reads of at least 32 beats (256 B), and must never
> fetch a sprite row at a time.** A typical OpenBOR RLE row is far shorter than 256 B. Row-
> at-a-time fetching lands at burst 1-8 = **38-227 MB/s**, which fails the worst PAK by a
> factor of ~1.6 and clears He-Man only barely. The engine must coalesce — whole-sprite or
> multi-row prefetch into a staging buffer, with the byte-serial RLE decoder reading from
> that buffer rather than from DDR3 directly.

This is a *different* constraint from register item 1 (the ~1 px/clk byte-serial decode
rate) and does not replace it. Item 1 says the decoder cannot consume fast enough; this
says the fetcher cannot supply fast enough if it asks in small pieces. Both must be fixed,
and the staging buffer that fixes this one is also the natural place to widen the decoder.

## Caveats, stated plainly

- Single-outstanding only. Multiple outstanding reads would likely go higher; we do not
  need them, so they were not measured.
- "He-Man loaded" is the engine running at its title screen, not a controller-driven
  combat scene. Active gameplay would raise A9 traffic somewhat — but see point 4: Tier-B
  *removes* the ARM's compositing load, so today's ARM traffic is an upper bound on the
  offloaded case, not a lower one.
- Read-only. Writes were not measured, deliberately: §14.4.5 puts the whole write side
  (scanout) at 8.59 MB/s against 100-360 MB/s of reads, so it cannot bind.
- The probe measures what the *port* delivers. It says nothing about whether the compositor
  can consume it — that is item 1.

## Re-running it

`fpga/OpenBOR.sv` and `fpga/files.qip` on `main` are deliberately **clean** — the probe
wiring is not committed into them, so no ship build can ever pick it up. To reproduce:

```bash
git apply tools/harness/tierb_probe_wiring.patch     # wires arbiter + probe into OpenBOR.sv
cd fpga && quartus_sh --flow compile OpenBOR         # ~10 min, SEED 3, no lottery needed
# deploy output_files/OpenBOR.rbf as _Other/PROBE_OpenBOR_7533.rbf  (NOT the OpenBOR_7533
# prefix -- that would hijack every existing .mgl, which resolves <rbf> by prefix)
# then: sh /tmp/read_bw_probe.sh on the device
git checkout -- fpga/OpenBOR.sv fpga/files.qip       # when done
```

The probe-build `output_files/` artifacts are likewise **not** committed: committing the
diagnostic build's `.sta.summary` / `.fit.summary` would misrepresent the shipping
bitstream's timing and utilization, which is what the "commit the summaries alongside the
RBF" rule exists to record.

## Incidental finding: an MGL can lose its `.s0` on a same-setname RBF swap

Loading `PROBE_He-Man.mgl` **directly from another OpenBOR core** (rather than from MENU)
left `.s0` **empty** and the engine correctly parked in its wait-for-PAK loop
(`hrtimer_nanosleep`, `framecnt` frozen at 1). Loading the identical MGL from MENU worked
first time.

Mechanism: both RBFs share the CONF_STR setname `OpenBOR`, so `/tmp/CORENAME` does not
change across the swap and Master_Daemon only notices via its **2-second poll** of the RBF
path. The handler it then spawns deletes `.s0` when it sees no reset/hot-swap marker — and
that delete can land at t+2..4 s, on top of the MGL's own `delay="2"` write.

This contradicts the safety argument recorded in CLAUDE.md's MGL section, which says the
handler's cleanup cannot race an MGL "because MGL's 2 s timer writes after handler
completes". That holds for a *cold* core load, where the handler runs at t~0. It does not
hold for a same-setname RBF swap, where the handler's own start is deferred by the daemon's
poll interval. Pre-existing, unrelated to Tier-B, and worth a marker or a longer MGL delay;
recorded here because it invalidated a measurement run before it was caught.
