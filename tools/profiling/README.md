# FULL_PROFILING backup snapshot

These are the **full fps-diagnostic** versions of the engine-patch sources,
captured 2026-06-13 right before the diagnostics were stripped for the clean
render->core-0 ship build. They contain every profiling probe used this session:

- `[FPS]` per-frame profile (entity/render/script timers)
- SUB-PROFILE v8 `[SUB]` (entity-internal), v9 `[OTH]` (outer loop), v11 `[SPQ]`
- `[BLD]`/`[BAL]` blend-time + alpha histogram
- `[A15]` exact-alpha -> blend-fp probe
- `[X8P16]`/`[X8P32]` hot-path probes
- `[VCP]` vcopy-internal timing (native_video_writer.c)

NOTE the gate-relevant detail: these carry the TEMPORARY/DIAG/REVERT marker
strings, so they live HERE (under tools/, not grepped by the CI commit-back
gate) rather than in the working sources.

## To re-enable profiling
Copy each back over its working counterpart, rebuild (CI), deploy. The
render->core-0 affinity + the `[LOAD]` phase breakdown are kept in the CLEAN
working sources, so they're present in both profiling and ship builds.

    cp tools/profiling/apply_patches.FULL_PROFILING.py        .github/scripts/apply_patches.py
    cp tools/profiling/native_video_writer.FULL_PROFILING.c   src/native_video_writer.c
    cp tools/profiling/sblaster_patch.FULL_PROFILING.c        patches/sblaster_patch.c
