# OpenBOR (build 7533, OpenBOR 4.0) project-level timing constraints.
#
# Background: same hybrid-architecture pattern as PICO-8 / OpenBOR_4086.
# The user PLL (emu|pll|pll_inst) produces clk_sys (~100 MHz, DDR3)
# and clk_pix (53.693 MHz, exact Genesis MCLK / 8). A separate
# pll_audio drives the audio output domain. CDC paths between these
# domains are correctly handled by 2-FF synchronizers and dcfifos in
# the reader, but without explicit asynchronous-group declarations
# Quartus tries to time them as synchronous and reports phantom
# -2 to -3 ns setup failures. The bitstream may still work by silicon
# luck, but any RTL change or recompile risks tripping past the lucky
# margin into actual data corruption.
#
# Fix: declare each user PLL output (and pll_audio) as its own
# asynchronous clock group. Same pattern that fixed PICO-8 v1.0
# (clk_pix slack -4.4 ns -> +35.6 ns).

set_clock_groups -asynchronous \
    -group [get_clocks {emu|pll|pll_inst|altera_pll_i|general[0].gpll~PLL_OUTPUT_COUNTER|divclk}] \
    -group [get_clocks {emu|pll|pll_inst|altera_pll_i|general[2].gpll~PLL_OUTPUT_COUNTER|divclk}] \
    -group [get_clocks {pll_audio|pll_audio_inst|altera_pll_i|general[0].gpll~PLL_OUTPUT_COUNTER|divclk}]

# Multicycle path constraints (MegaCD/Saturn pattern) were tested
# 2026-06-02 and slightly HURT us (+0.063 -> +0.029 ns on pll_hdmi).
# Hq2x/ascal weren't our bottleneck; the constraint moved placement
# away from where it was helping pll_hdmi. PSX/N64 don't use these
# constraints either, so our SDC is still valid framework practice.
# Removed for SEED bumping experiments. May re-add if structural
# changes shift the bottleneck onto Hq2x/ascal paths.

# Step 60 / Option Y (2026-06-02): tried adding pll_hdmi as a 4th async
# clock group to fix the intra-divider setup-slack failure that emerged
# after the polyphase downscale module landed. The fix DIDN'T HELP --
# the failing path is INTRA-pll_hdmi (counter[0].output_counter|divclk
# internal divider), not a cross-domain path. async-group only affects
# inter-domain timing. Reverted. Documented for future reference: when
# pll_hdmi intra-divider fails under heavy user-RTL placement pressure,
# the actual fix is reducing ALM utilization or adding LOGIC_LOCK_REGION
# floorplan constraints to keep user logic away from the pll_hdmi area.
