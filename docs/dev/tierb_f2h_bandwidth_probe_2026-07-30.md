# Tier-B GO/NO-GO gate — measured f2h DDR3 read bandwidth (2026-07-30)

`OPTION4_FPGA_BLEND_OFFLOAD_SCOPE.md` §4 scoped this as *"the single most important
deliverable of scoping"* and it had never been run. Register item 0 — the **433 MB/s
conservative ceiling** that §15 uses as its only hard bandwidth gate — had **no derivation**
anywhere in the workspace or in git history.

*(Precisely: no derivation, but it does have provenance. `#FPS_BUCKETS.md:84-85` names "the
conservative ram1 ceiling" and pins it numerically — 133 MB/s is called 31% of it and 369
MB/s is called 85%, and both percentages recover 429-434. So the number was in circulation
as an unexplained denominator in a file this project cites elsewhere; what was missing was
any justification for it. An earlier revision here said it appeared "nowhere", which
overstated the search.)*

**It is now measured. 433 is not a ceiling.**

## Harness

| piece | what |
|---|---|
| `fpga/rtl/tierb_bw_probe.sv` | read-only Avalon master. 16 steps = 8 burst lengths {1,2,4,8,16,32,64,128 beats} x {sequential, scattered}. 2^24-cycle (170.4 ms) measured window per step, followed by an equal idle gap so the machine stays usable. Strictly single-outstanding: issue, drain every beat, issue again — the protocol `openbor_video_reader.sv` already proves works on this bridge. Publishes 49 qwords at `0x3A0F0000`, resweeping every ~5.5 s |
| `fpga/rtl/tierb_ddr_arb.sv` | strict-priority arbiter. Video reader = port A, absolute priority; probe = port B, fills the idle cycles |
| `tools/harness/read_bw_probe.sh` | on-device `devmem` decoder |

Traffic is the core's own DDR3 window — reads only, so it cannot corrupt anything, and it
is realistic: that is the memory the compositor would be fetching. Scattered addresses are
64 B aligned via a maximal-length 32-bit LFSR (taps 32,22,2,1) confined to the low 512 KiB;
the sequential pattern's index is 17 bits and therefore walks the full low **1 MiB**. Both
stay inside `0x3A000000-0x3A0FFFFF`, verified by construction.

RBF: `PROBE_OpenBOR_7533.rbf`, deployed locally only, **never committed** (per
`[[never-push-test-rbf-or-arm-binary]]`). Named so it does **not begin with**
`OpenBOR_7533`, because every existing `.mgl` resolves `<rbf>` by prefix and would
otherwise have been hijacked by it.

Timing with the probe in: **pll_hdmi 0.271 ns** (baseline was 0.128), clk_sys 1.785,
clk_pix 8.171, all positive, TNS 0.000, SEED 3 unchanged. **The harness costs 352 ALMs**
-- 0.84% of the device: 9,014 total (22%) against the ship build's committed 8,662 (21%,
`fpga/output_files/OpenBOR.fit.summary`) -- and **zero** extra M10K. *(An earlier revision
wrote "Cost 9,014 ALMs (22%, was 20%)", which reported the TOTAL as the cost -- overstating
the harness ~26x -- and truncated the baseline where it rounded the new value. 20% was last
true several RBFs ago.)*

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

## The numbers fit a two-parameter model, which is the strongest evidence they are real

All 32 measurements fit **`MB/s = 787.5 x N/(N+k)`** — a fixed dead-time `k` cycles per
burst, which is exactly what a single-outstanding master paying one round trip per burst
should produce:

| N | 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128 |
|---|---|---|---|---|---|---|---|---|
| implied k, idle sequential | 15.07 | 14.70 | 14.42 | 14.65 | 15.41 | 15.57 | 15.86 | 17.13 |
| implied k, loaded scattered | 19.45 | 19.25 | 19.58 | 19.77 | 20.70 | 20.93 | 22.01 | 24.57 |

`k` holds to **+/-1.4 cycles across a 128x span of burst length**, and rises monotonically
under load — contention growing with the probe's own duty cycle, which is physically
correct. A beat-counting bug, a mis-attributed burst, or a wrong window length would
essentially never produce this. It also *quantifies the headroom*: ~15 of those cycles are
the serial issue→latency→return loop, of which only ~2 are the probe's own FSM. Two
outstanding reads roughly halve `k`; deep pipelining removes it and approaches 787.5 MB/s
at any burst length.

## What it says

1. 🛑 **433 MB/s is not a ceiling, and it never was one.** The port sustains **663 MB/s
   with a PAK running** — 84% of theoretical. 433 is roughly what this port delivers at a
   ~20-beat burst on the idle-sequential curve (~25.6 beats on the loaded-scattered curve
   the gate actually uses); it is a point on a curve, not a limit.

2. 🛑 **Burst length is the entire story — a 16x spread.** 41.5 MB/s at burst 1 rising to
   663 MB/s at burst 128. Everything else in this measurement is a rounding error next to
   it.

3. **Scatter costs little, but read the number where it can actually show.** 0.4% at burst
   128 is close to tautological — a "scattered" 128-beat read is still 128 *sequential*
   beats after one row miss, so the miss amortises away exactly like the latency does. The
   real row-locality signal is at the short end: **7-8% at burst 1** (38.5 vs 41.5 loaded,
   44.9 vs 49.0 idle) and 5% at burst 8. So locality is a genuine but second-order cost,
   and **latency amortisation dominates it at every burst length**. The compositor's
   scattered per-sprite row fetches pay a few percent for being scattered and up to 16x for
   being *short*. *(Region-size honesty: the scattered pattern walks 512 KiB ~ 512 DDR3 rows
   against 8 banks, so a random start hits an open row ~1.5% of the time versus ~0.04% for a
   multi-MB sprite arena. The difference is immaterial at this precision, but the small
   region does flatter scatter slightly.)*

4. **A9 contention is small.** Loading a PAK and running the engine costs ~5% at long
   bursts (694.6 -> 663.1) and ~15% at burst 1. And this over-states it for Tier-B: under
   the offload the ARM stops compositing, so its DDR3 load falls rather than rises.

5. **The reader's bus occupancy is of order 1.4% — an UPPER BOUND, not a clean
   measurement, and NOT the independent confirmation an earlier revision claimed.**
   `blocked` (`tierb_bw_probe.sv:164`) is `ST_ISSUE & (raw_ddr_busy | a_active |
   ~bus_idle)`, so `c_stall` fuses the reader's occupancy with framework backpressure and
   with `a_owns`; it cannot separate them. Two things partially rescue it and one does not:
   the measured `ddrbusy%` was **0.0% in all 32 windows**, so the backpressure contamination
   is empirically nil; and `stall%` came out **1.4% at every one of the 16 steps**, which is
   what you expect if it is tracking a fixed external demand rather than an artefact of the
   probe's own duty cycle. But the 1.28% figure it was said to "confirm" is **itself an
   undercount** — 0.813 us counts 80 beats and omits the ~15-cycle issue+latency overhead
   per line burst (95 cycles = 0.965 us = **1.51%**), plus the per-frame ctrl/joy/audio ops.
   Two numbers each wrong in unquantified directions agreeing to 0.1 pp is coincidence, not
   corroboration. **The honest claim is that the reader costs somewhere around 1.4-1.5%,
   consistent with the derivation's range.** Measuring it properly needs a dedicated
   `a_owns | a_active` counter in the arbiter.

## Verdict on the gate: PASS, conditional on burst length

Against §14.4.5's port loads (all terms counted):

| | needs | first burst that clears it (scattered, PAK loaded) |
|---|---:|---|
| He-Man | ~122.6 MB/s *(disputed -- register item 14 puts it at 144.9)* | **4 beats / 32 B** (133.6) -- but **8 beats** if item 14 is right |
| worst PAK (Ninja) | ~362.7 MB/s | **32 beats / 256 B** (476.1); 16 beats gives 343.3 and does **not** clear it |

**So the bandwidth premise holds — but it converts into a hard constraint on the fetch
engine that this document did not previously state:**

> 🛑 **A row-at-a-time fetcher does not have the bandwidth. It must either coalesce to
> >= 32-beat (256 B) reads, or keep 2-4 reads outstanding.** Row-at-a-time single-
> outstanding lands at burst 1-8 = **38-227 MB/s**, failing the worst PAK by ~1.6x and
> clearing He-Man only barely. The natural fix is whole-sprite or multi-row prefetch into a
> staging buffer, with the byte-serial RLE decoder reading from that buffer rather than
> from DDR3.

**Four qualifications, because this constraint is softer than it looks:**

- **32 is a property of THIS PROBE, not of the port.** The probe is deliberately single-
  outstanding, so every burst pays the full round trip. The fitted overhead is ~15 cycles
  per burst idle, ~21 loaded (see the model below); **2-4 outstanding reads roughly halve to
  eliminate it**, and a deeply pipelined fetcher approaches 787.5 MB/s at *any* burst
  length. Burst length is one of two levers, not the only one.
- **The threshold is not resolved — the sweep is powers of two.** Interpolating the loaded-
  scattered curve puts the 362.7 MB/s crossing at **~18-20 beats**, not 32. 32 is the next
  measured point above it, chosen for margin.
- **No repeatability data.** One snapshot per condition, quoted to 4 significant figures,
  from a probe that re-sweeps every 5.5 s. The 16-beat point misses by 5.4%, which is
  inside a run-to-run variation nobody has characterised.
- **The demand side is this project's own open item.** 14.5/T5 records that Ninja's overdraw
  counter sits at 63.6% of `UINT32_MAX` and *"a wrap reports a low overdraw -- plausible and
  wrong in the dangerous direction"*, and review item E-3 lists the He-Man figure as still
  open. Both sides of this comparison are provisional. It also assumes ~100% fetch
  utilisation, which holds for whole-sprite prefetch and fails for heavily clipped sprites.

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

## Repeatability (three independent snapshots of the loaded condition)

An earlier revision reported a single snapshot to four significant figures with no variance,
which was a fair criticism. Three separate sweeps of the same condition (probe core, He-Man
mounted, engine at its title screen), taken minutes apart — sweep #8, sweep #10 with the
engine verified live, and sweep #60 after a round of user gameplay:

| burst | 1 | 2 | 4 | 8 | 16 | 32 | 64 | 128 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| sequential, min | 41.5 | 80.2 | 143.8 | 239.3 | 357.6 | 483.3 | 591.2 | 663.1 |
| sequential, max | 41.8 | 81.4 | 146.8 | 241.3 | 360.9 | 487.9 | 593.8 | 669.5 |
| scattered, min | 38.5 | 74.1 | 133.6 | 226.9 | **343.3** | 476.1 | 586.0 | 660.7 |
| scattered, max | 40.3 | 75.9 | 136.6 | 231.2 | **355.0** | 484.7 | 589.2 | 665.4 |

Spread is **under 1% almost everywhere**; the worst cell is scattered-16 at **3.4%**.

A separate 150-second trace then sampled scattered-128 every 6 s for **25 consecutive
samples**: range **659.8 - 664.4 MB/s**, spread **0.7%**, with the frame rate pinned at
101-105 and the audio ring at 55-62 KB throughout. The port's delivered throughput is
stable to well under a percent over minutes.

🛑 **This settles the one place it mattered.** The reviewer's objection was that the 16-beat
point misses the 362.7 MB/s requirement by only 5.4%, "inside a run-to-run variation nobody
has characterised." It is now characterised: scattered-16 spans 343.3-355.0, and **362.7 sits
above all three runs** — short by 2.2% even at the most favourable observed value. So the
conclusion that 16 beats does not clear the worst PAK survives the variance, and the
interpolated ~18-20 beat threshold is where the crossing actually falls.

## Harness limitations (none of which affected this run)

Recorded so the next person to build on this knows where the thin ice is.

- 🛑 **A swallow would permanently desync the arbiter.** On beat timeout the probe abandons
  the burst (`tierb_bw_probe.sv:283-290`) but never tells the arbiter, so `b_owns` sticks at
  1, `a_busy` stays high, and **the video reader is starved forever** — frozen display, and
  every subsequent `c_bursts` is fiction. It did not fire (0 swallows in 32 windows, and the
  failure would have been unmistakable), which makes it corroborating evidence rather than a
  defect in the result. Fix before any reuse: an arbiter-side `b_owns` watchdog, or an
  `abort` output from the probe.
- **`a_busy` omits `a_owns`** (`tierb_ddr_arb.sv:109`), so after a reader read-timeout a
  reader *write* can be presented, advanced past, and silently dropped. Latent — every
  ordinary reader path clears `a_owns` on the same edge as its state change — and
  harness-only. Note the asymmetry: port B's `b_busy` *does* include `a_owns`.
- **The mux polarity is inconsistent**: `ddr_rd`/`ddr_we` prioritise on `a_go`, while
  `ddr_addr`/`burstcnt`/`din`/`be` select on `b_go`. Correct today only because the two
  `_go` terms are mutually exclusive.
- **`ddr_addr` and `ddr_din` are not in the reset block**, so they are X in simulation until
  the first write. Harmless in hardware (`ddr_rd`/`ddr_we` reset low).
- 🛑 **STILL OPEN: no measurement under active gameplay.** `read_bw_probe.sh`'s own
  instructions say to run it during gameplay; every run so far was taken at a PAK title
  screen or pause menu (both ~102-104 fps on this build). An attempt to auto-capture
  gameplay failed because the trigger was calibrated on `#FPS_BUCKETS.md`'s documented
  34-48 fps He-Man figure -- which predates the 2026-07-27 affinity inversion and the
  2026-07-28 16-bit vscreen, and may itself now be stale. **Two things ride on closing
  this:** the "A9 contention ~5%" claim, and whether that 34-48 anchor -- which 14.4.5's
  entire per-PAK model is normalised against -- still holds.

  *(Weak prior that it will not move much: `ddrbusy%` measured **0.0% in every window of
  every run**, i.e. the framework never once asserted backpressure, and the only ARM effect
  observed anywhere is the ~5% gap between no-PAK and PAK-loaded. But that is a prior, not
  a measurement, and assuming it is exactly the mistake that produced the unverified
  "engine running" claim this document already had to correct once.)*
- **"beats/burst equalled the requested length" is not a second confirmation of "0
  swallows"** — the FSM can only close a window from `ST_ISSUE` with nothing in flight, so
  the two are the same fact by construction.
