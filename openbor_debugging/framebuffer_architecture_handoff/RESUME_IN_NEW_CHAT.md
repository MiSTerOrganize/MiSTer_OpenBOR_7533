# Resume prompt for new chat — FB+ASCAL architecture for OpenBOR_7533

Paste this verbatim into the new Claude Code chat.

---

I'm rewriting MiSTer_OpenBOR_7533's video pipeline. We abandoned a custom DDR3 reader + edge-aware downscale ("Option Y") because it couldn't handle variable-resolution PAKs cleanly. Switching to the **AO486 framebuffer pattern**: ARM writes pixels straight to DDR3, sys/ framework's ASCAL scaler reads them and generates HDMI on the fly. Handles arbitrary input dimensions (Batman 320×240, PDC2 480×272, He-Man 960×480, Lust Rush 1920×1080) without per-PAK math on our side.

**Current state**:
- **Local repo**: on branch `framebuffer-architecture`, checked out from tag `v3.1-pre-polyphase` (commit `c6089d7` Step 70). Old Option Y RTL files are GONE from working tree (`openbor_video_downscale.sv` doesn't exist on this branch). Reader/timing/top still exist as v3.0-era — those will be REPLACED.
- **Dev MiSTer**: running stable v3.0 RBF (md5 `f877251a`) + v3.0 ARM binary (md5 `9655acb3`). Batman + all canonical PAKs work normally via legacy 320×224 squish. User uses this for normal play while I develop.
- **Abandoned work**: in `stash@{0}` (Phase 7l–7q CDC sync experiments, hardcoded 240 hotfixes — all wrong direction, do not reference).

**Design doc already written** at `docs/dev/framebuffer_architecture.md` (529 lines, ~25KB). It covers: FB_* signal reference (FB_EN/FB_FORMAT/FB_BASE/FB_WIDTH/FB_HEIGHT/FB_STRIDE/FB_VBL/FB_LL/FB_FORCE_BLANK), DDR3 memory layout (recommended FB region at `0x2A000000` to avoid ASCAL's triple-buffer at `0x20000000-0x21FFFFFF` and Main_MiSTer's LFB at `0x22000000+32MB`), FB_FORMAT encoding (bits[2:0] = bpp, bit[3] = 565/1555, bit[4] = RGB/BGR), ao486.sv canonical wiring snippets, variable-res handling (ASCAL re-detects on vblank, `swblack` emits 3 black frames during transition), and a 6-phase migration plan.

**Your task right now**: read `docs/dev/framebuffer_architecture.md` carefully, then propose a **Phase-1 implementation plan** — which files to touch first, in what order, with specific code outlines. That gives us a checklist to validate against ao486 patterns BEFORE any compile cycles. No RTL writes yet — plan first.

**Reference repos** (browse via WebFetch if needed):
- `MiSTer-devel/ao486_MiSTer` — the canonical FB-using core. Look at `ao486.sv` top-level for FB_* wiring.
- `MiSTer-devel/Main_MiSTer` (the framework) — `sys/sys_top.v` for FB protocol, `sys/ascal.sv` for the scaler internals.
- `MiSTer-devel/Template_MiSTer` — `sys/emu_ports.vh` lines 42-65 for the canonical FB_* port declarations (gated by `MISTER_FB` define).

**Strict project rules in scope** (CLAUDE.md will reload — these are not new, just flagging the load-bearing ones for this work):
- 🛑 **Never modify user-supplied PAKs**. All fixes go in engine source (apply_patches.py) / ARM binary / RBF / handler.
- 🛑 **ARM binaries are built via GitHub Actions ONLY** — no local Docker, no `--no-verify`. Push to per-core CI, CI builds + commits binary back to main.
- 🛑 **Never push test RBFs or ARM binaries to main during iterative debugging** — keep test artifacts LOCAL via WinSCP scripted deploy. CI gate's diagnostic-marker detection (`TEMPORARY DIAG` etc.) prevents shipping by accident.
- 🛑 **NEVER spawn Master_Daemon from a deploy/cleanup script** (it's a boot-time singleton).
- 🛑 **NEVER direct-SSH from this Claude Code shell** — always WinSCP scripted (PowerShell heredoc with backslash local paths, forward-slash remote paths).
- 🛑 **Never push test/in-progress RBFs to main** — they auto-deploy via update_all to community users.
- 🛑 **Per-core CI commit-back MUST auto-trigger MiSTer_Frontier DB rebuild** (existing in build.yml, don't break it).
- 🛑 **Iterative full audits IN A LOOP for major architectural changes** — and this IS a major architectural change. Run audits until ZERO bugs AND ZERO concerns.

**Companion rules I expect to apply** (existing memory entries — they'll load via auto-memory): `[[fpga-debug-tools-catalog]]`, `[[vga-color-state-visualizer-for-fpga-debug]]`, `[[signaltap-ring-buffer-via-ddr3-for-mister-debug]]`, `[[tag-pre-risky-work-commit]]`, `[[session-continuity-bundle]]`, `[[no-false-found-it]]`, `[[seed-lottery-try-non-adjacent-values]]`.

After you've read the design doc, present the Phase-1 plan as a numbered list with: (a) what file gets touched, (b) what RTL/code goes in it (high-level — not full code yet), (c) how I'd verify each step before moving to the next. Keep the plan tight — under 800 words. I'll review before you write any code.
