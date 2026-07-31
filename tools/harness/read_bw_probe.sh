#!/bin/sh
# ===========================================================================
# read_bw_probe.sh -- decode the Tier-B f2h DDR3 bandwidth probe result block
#
# TEMPORARY DIAG.  Runs ON THE MiSTer.  Pairs with fpga/rtl/tierb_bw_probe.sv.
#
# The probe publishes 49 qwords at 0x3A0F0000 and republishes them every
# sweep (~5.5 s).  Run this while a PAK is in active gameplay -- the whole
# point is achieved bandwidth UNDER contention, so measuring at a menu or on
# a black screen understates the reader's load and overstates the answer.
#
# Usage:  sh /tmp/read_bw_probe.sh
# ===========================================================================

BASE=0x3A0F0000
CLK_HZ=98437500
WINDOW=16777216

rd() {   # rd <byte-addr> -> decimal 32-bit
	v=$(devmem "$1" 32 2>/dev/null)
	[ -z "$v" ] && v=0x0
	printf '%d' "$v"
}

hdr_lo=$(rd $BASE)
hdr_hi=$(rd $((BASE + 4)))

if [ "$hdr_lo" != "1413633841" ]; then   # 0x54425731
	echo "NO PROBE DATA at $BASE (magic=$(printf '0x%08X' "$hdr_lo"))."
	echo "Either this RBF has no probe, or the sweep has not finished settling"
	echo "(1.4 s after core load) -- wait a few seconds and re-run."
	exit 1
fi

echo "Tier-B f2h DDR3 read-bandwidth probe -- sweep #$hdr_hi"
echo "window ${WINDOW} cycles @ ${CLK_HZ} Hz = $(awk -v w=$WINDOW -v c=$CLK_HZ \
      'BEGIN{printf "%.1f", w/c*1000}') ms per step"
echo
echo "pattern     burst   MB/s    beats/burst   bus-util%   stall%   ddrbusy%   swallow"
echo "---------------------------------------------------------------------------------"

k=0
while [ $k -lt 16 ]; do
	a=$((BASE + 8 + k * 24))
	beats=$(rd $a)
	bursts=$(rd $((a + 4)))
	busy=$(rd $((a + 8)))
	stall=$(rd $((a + 12)))
	cfg_lo=$(rd $((a + 16)))
	cfg_hi=$(rd $((a + 20)))

	awk -v beats="$beats" -v bursts="$bursts" -v busy="$busy" -v stall="$stall" \
	    -v cfg="$cfg_lo" -v W=$WINDOW -v C=$CLK_HZ 'BEGIN {
		swallow = int(cfg / 65536)
		lg      = int(cfg / 256) % 8
		pat     = int(cfg / 4096) % 2
		burst   = 1
		for (i = 0; i < lg; i++) burst = burst * 2
		mbs     = beats * 8 * (C / W) / 1000000
		bpb     = (bursts > 0) ? beats / bursts : 0
		printf "%-11s %5d  %7.1f  %11.2f  %9.1f  %7.1f  %9.1f  %8d\n",
		       (pat ? "scattered" : "sequential"), burst, mbs, bpb,
		       100.0 * beats / W, 100.0 * stall / W, 100.0 * busy / W, swallow
	}'
	k=$((k + 1))
done

echo
echo "MB/s = beats * 8 B * (CLK_HZ / WINDOW).  bus-util% is the probe's share of"
echo "all cycles; the video reader's own 8.59 MB/s and the A9's traffic are the"
echo "rest.  beats/burst below the requested burst length means bursts were"
echo "truncated -- check swallow."
