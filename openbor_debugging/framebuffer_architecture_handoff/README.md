# Framebuffer architecture handoff bundle

Created 2026-06-06 at the end of a multi-hour debug session that abandoned the Option Y custom downscale architecture in favor of the canonical AO486-style FB+ASCAL pattern.

## Files in this folder

| File | Purpose |
|---|---|
| **RESUME_IN_NEW_CHAT.md** | Paste-this prompt for the new Claude Code chat. Contains all context the new session needs: current branch, dev MiSTer state, design doc location, reference repos, scope of strict rules. |
| **README.md** | This file. |

## Quick state summary

- **Branch**: `framebuffer-architecture` (from `v3.1-pre-polyphase` tag, commit `c6089d7`)
- **Dev MiSTer**: running v3.0 stable RBF (`f877251a`) + v3.0 ARM binary (`9655acb3`). Batman + all canonical PAKs work normally.
- **Design doc**: `MiSTer_OpenBOR_7533/docs/dev/framebuffer_architecture.md` (529 lines, ~25KB).
- **Abandoned work**: `stash@{0}` (Phase 7l–7q CDC sync experiments, hardcoded 240 hotfixes — all wrong direction).
- **Old Option Y RTL**: GONE from working tree (`openbor_video_downscale.sv` doesn't exist on this branch).

## How to resume

1. Open new Claude Code chat in `MiSTer_OpenBOR_7533` directory.
2. Paste contents of `RESUME_IN_NEW_CHAT.md` as the first message.
3. Claude reads the design doc, proposes Phase-1 implementation plan, waits for review.
4. After plan is approved, RTL work begins.

## What to avoid in the new chat

Do NOT reference:
- "Phase 7l", "Phase 7m", ..., "Phase 7q" — abandoned diag/CDC work
- VGA-color test results (BLUE, CYAN, RED, BLACK from earlier diag cycles) — those were debugging Option Y, not relevant to FB pattern
- `src_h_latched`, `dim_word[15]`, multi-bit CDC sync hypothesis — wrong rabbit hole
- "Hardcoded 240" — was a diagnostic hack, not part of the new design
- "Reader's CTRL/DIM read timing out" — was a downstream symptom of the wrong architecture choice

DO reference:
- `docs/dev/framebuffer_architecture.md` (the design doc)
- The ao486 / Main_MiSTer / Template_MiSTer reference repos
- Strict project rules from CLAUDE.md (auto-loads)
- Memory entries via `[[name]]` linkage

## Recovery

If anything goes wrong in the new chat, the dev MiSTer remains stable. Use:
- `git checkout v3.1-pre-polyphase` to restore branch
- WinSCP to push `OpenBOR_7533_20260517_V30_STABLE.rbf` (at `openbor_debugging/phase10_pending_deploy/`) back if RBF deploy issues arise
- `stash@{1}` is unrelated v3.10 audit work, can ignore
- `stash@{0}` contains abandoned Phase 7l-7q work, do not unstash
