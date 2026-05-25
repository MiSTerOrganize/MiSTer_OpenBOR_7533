# MiSTer OpenBOR

Hybrid ARM+FPGA OpenBOR beat-em-up engine for MiSTer FPGA. Ships **two engine builds** (4086 and 7533) that share a single PAK library, the same controller mappings, and identical FPGA video/audio output. Pick which build to load at runtime by selecting the matching RBF in `_Other/`. Inspired by [SumolX](https://github.com/SumolX/MiSTer_OpenBOR)'s original MiSTer OpenBOR port.

## Which build do I load?

Both builds run on the same FPGA core, take the same controllers, output the same Genesis-H40-exact video, and load PAKs from the same `games/OpenBOR/Paks/` folder. The only difference is the OpenBOR engine version inside each ARM binary.

| Build | Best for | PAK era | Engine |
|---|---|---|---|
| **OpenBOR_4086** | Legacy community PAK collections (~300 games on Archive.org / Retrobat / Batocera / Launchbox) | OpenBOR 3.x — Builds 3366 to 4086 | OpenBOR v3.0 Build 4086 (commit `af23dc9c`), SDL 1.2.15 |
| **OpenBOR_7533** | Modern PAK collections — TMNT: Rescue-Palooza, Final Fight LNS Ultimate, Avengers UBF v2.7+, Zvitor / RVGM sets, Pocket Dimensional Clash 2, He-Man and the Masters of the Universe | OpenBOR 4.0 — Builds 6000+ era, also runs older PAKs backward-compatibly | OpenBOR 4.0 Build 7533 (tag `v7533`), SDL 2.0.8 |

If a PAK won't run on one build, reload the other RBF and try again — your `Paks/` folder is shared, no file moves needed.

## Features

- **Native FPGA video output** — 320×224 @ 59.92 Hz with exact Sega CD NTSC pixel clock (6.712 MHz from NTSC colorburst crystal). H40+V28 mode — CRT image width matches NES/SNES/Genesis exactly (47.68 µs active time)
- **Direct DDR3 write frame path** — engine's `video_copy_screen` writes pixel data directly to the FPGA's video ring buffer at 0x3A000000, bypassing SDL's renderer/surface chain entirely (saved ~15 ms/frame on Cortex-A9; lifted native fps from ~29 to ~85-100 on a powerful frame-present path 2026-05-22)
- **PAK load-time hash-map cache** (v2.9, 2026-05-24) — `loadsprite()` cache lookup replaced from O(N) linear scan to O(1) hash table (262144 buckets, separate chaining, DJB2 hash with inline case-folding). Phase 1 + Phase 1.1 tunes shipped. Validated load-time reductions on heavy PAKs:
  - Justice League Legacy: 213 s → **69.1 s (-68%)** ⭐
  - Double Dragon Reloaded Alt: 73 s → **35 s (-52%)**
  - A Tale of Vengeance: ~12 s → **1.87 s (-80%+)** ⭐
  - TMNT Rescue Palooza / Avengers UBF / He-Man / PDC2: 25-50% reductions
  - `[LOAD] PAK loaded in N ms` printf retained at end of `load_models()` for power-user tracking — `grep '\[LOAD\]' /media/fat/logs/OpenBOR_7533/OpenBorLog.txt`
- **Native FPGA audio output** — 48 kHz stereo via DDR3 ring buffer, no ALSA. Audio kernel: **nearest-neighbor (zero-order hold)** at engine + wrapper (matches upstream OpenBOR `engine/source/gamelib/soundmix.c` FIX_TO_INT shift-truncation kernel at all three sample-read sites — music + 8-bit voice + 16-bit voice; wrapper at `patches/sblaster_patch.c::audio_thread_fn` mirrors the engine character — both stages NN).
- **CRT support** — scanlines, shadow masks, and analog video output for CRT displays
- **MiSTer OSD integration** — load PAK files from the file browser
- **4-player support** — connect up to 4 controllers, add players by pressing START
- **Custom pause menu** — Continue / Options / Reset Pak / Quit. Music and sound effects pause cleanly on menu entry, resume on Continue (audio-tail leak fixed 2026-05-22)
- **Auto-launch** — OpenBOR starts automatically when the core is loaded
- **Sub-native PAKs scale automatically** — PAKs with native resolutions other than 320×224 (320×240 4086-era PAKs, 480×272 PSP-widescreen PAKs like Pocket Dimensional Clash 2, 960×480 He-Man, 480×272 Avengers UBF, etc.) are anisotropic-nearest-neighbor-squished into the 320×224 Sega CD V28 NTSC active area edge-to-edge. NN matches engine render character (engine renders pixel-exact, wrapper preserves it; bilinear was ~4× more CPU for marginal benefit). Aspect distortion is intentional — matches Sega CD displayed area.

## Quick Install

The recommended path is via the **MiSTer Frontier** combined database, which auto-deploys both OpenBOR builds (and any other Frontier core you opt into) every time you run `update_all`.

Add this to `/media/fat/downloader.ini` on your MiSTer's SD card:

```ini
[MiSTerOrganize/MiSTer_Frontier]
db_url = https://raw.githubusercontent.com/MiSTerOrganize/MiSTer_Frontier/db/db.json.zip
filter = openbor-4086 openbor-7533
```

The `filter` line picks both OpenBOR builds. Drop the filter line entirely if you want every Frontier core, or pick just one build — see the [Frontier README](https://github.com/MiSTerOrganize/MiSTer_Frontier#choosing-what-to-install-filters) for the full filter list.

| Filter | Result |
|---|---|
| `openbor-4086 openbor-7533` | Both builds + shared handler/docs |
| `openbor-4086` | 4086 only (with shared infra) |
| `openbor-7533` | 7533 only (with shared infra) |

After editing `downloader.ini`:

1. Run `update_all` from MiSTer's Scripts menu — installs the FPGA cores, ARM binaries, unified handler, and docs
2. Run `Scripts/Install_MiSTer_Frontier.sh` once — registers the Master Daemon that auto-launches the engine. Idempotent
3. Place your `.pak` game modules in `/media/fat/games/OpenBOR/Paks/`
4. Load either **OpenBOR_4086** or **OpenBOR_7533** from the MiSTer console menu — the engine launches automatically

**Inspecting the manifest:** [DB Inspector for MiSTer_Frontier](https://theypsilon.github.io/DB-Inspector_MiSTer/?database-url=https%3A%2F%2Fraw.githubusercontent.com%2FMiSTerOrganize%2FMiSTer_Frontier%2Fdb%2Fdb.json.zip) — every file, hash, size, and tag visible in the browser. Useful for verifying which files a given filter would install before you run `update_all`.

## Manual Install

Extract the release zip to the root of your MiSTer SD card (`/media/fat/`):

```
/media/fat/
├── _Other/
│   ├── OpenBOR_4086_YYYYMMDD.rbf          FPGA core (4086 build, dated)
│   └── OpenBOR_7533_YYYYMMDD.rbf          FPGA core (7533 build, dated)
├── docs/
│   └── OpenBOR/
│       └── README.md                      This file
├── games/
│   └── OpenBOR/                           Shared folder for BOTH builds
│       ├── OpenBOR_4086                   ARM binary (4086 engine)
│       ├── OpenBOR_7533                   ARM binary (7533 engine)
│       ├── _handler.sh                    Master_Daemon dispatcher
│       └── Paks/                          Place your .pak game modules here
├── logs/
│   ├── OpenBOR_4086/                      4086 engine logs (handler + engine + script)
│   └── OpenBOR_7533/                      7533 engine logs (handler + engine + script)
├── saves/
│   ├── OpenBOR_4086/                      4086 engine saves
│   └── OpenBOR_7533/                      7533 engine saves
├── savestates/
│   ├── OpenBOR_4086/                      4086 savestates
│   └── OpenBOR_7533/                      7533 savestates
└── Scripts/
    └── Install_MiSTer_Frontier.sh         Install script (shipped by MiSTer_Frontier — unified across all Frontier cores)
```

Saves and savestates are kept separate between the two engine builds because the on-disk format isn't guaranteed compatible across the OpenBOR 3.x → 4.0 boundary.

## Supported Features

Both `OpenBOR_4086` and `OpenBOR_7533` cores have identical support across these dimensions:

| Feature | OpenBOR_4086 | OpenBOR_7533 |
|---|---|---|
| Saves (`<pak>.sav` engine progress) | ✅ `/media/fat/saves/OpenBOR_4086/` | ✅ `/media/fat/saves/OpenBOR_7533/` |
| Savestates (`<pak>.scr` engine snapshot) | ✅ `/media/fat/savestates/OpenBOR_4086/` | ✅ `/media/fat/savestates/OpenBOR_7533/` |
| Logs (with auto-prune N=10) | ✅ `/media/fat/logs/OpenBOR_4086/` | ✅ `/media/fat/logs/OpenBOR_7533/` |
| Configs (`<pak>.cfg` + `default.cfg` + `<pak>.hi`) | ✅ `/media/fat/config/` (shared across sister cores) | ✅ shared with 4086 |
| MGLs (`_Other/*.mgl` one-click launchers) | ⚠ architectural support, not user-tested yet | ⚠ same |
| Gameplay Recordings / TAS (`<pak>.inp`) | ✅ engine-native Record Game / Play Recording | ✅ same |
| Gamepad (up to 4P, Start adds player) | ✅ | ✅ |
| Keyboard | ❌ no (SDL keyboard not wired through dummy driver) | ❌ no |
| Mouse | ❌ no (no native engine mouse support) | ❌ no |
| Screen Positioning (CRT) H ±3 / V ±3 | ✅ | ✅ |
| Online Network Play | ❌ | ❌ |
| Multiplayer | ✅ up to **4 players** (Start adds player) | ✅ up to **4 players** |
| Light Gun | ❌ | ❌ |
| Aspect Ratio (Original / Full Screen / Custom1 / Custom2) | ❌ planned (roadmap #1) — fixed 4:3 in `fpga/OpenBOR.sv` lines 232-233 | ❌ planned (roadmap #1) — same as 4086 |
| Vertical Crop (216p 5x for clean 1080p integer scale) | ❌ planned (roadmap #2) — V_ACTIVE=224 already, 4-line trim each side | ❌ planned (roadmap #2) — same |
| Crop Offset (±12, paired with V-Crop) | ❌ planned (roadmap #2 pair) | ❌ planned (roadmap #2 pair) |
| Scale Mode (Normal / V-Int / HV-Int integer scaling) | ❌ planned (roadmap #3) | ❌ planned (roadmap #3) |
| Swap Joysticks (P1↔P2) | ❌ planned (roadmap #4) — co-op QoL | ❌ planned (roadmap #4) |
| Pause when OSD open | ❌ planned (roadmap #5) — universal QoL | ❌ planned (roadmap #5) |
| Stereo Mix (None / 25 / 50 / 100% channel cross-bleed) | ❌ planned (roadmap #6) — engine is true stereo | ❌ planned (roadmap #6) — same |

> **Roadmap note** — rows above marked "planned (roadmap #N)" are tracked in the mainstream-core parity roadmap: 6 LOW-difficulty OSD features common to ≥6 of 11 surveyed mainstream MiSTer cores (NES/Genesis/SNES/GB/GBA/SMS/NeoGeo/N64/PSX/Saturn/MegaCD). Ship order: Aspect Ratio → V-Crop+Offset → Scale → Swap Joysticks → Pause-when-OSD → Stereo Mix. Implementation is sys/-framework status-bit wiring only — no new RTL, no engine-side work.

## Controls (Xbox wireless controller default mapping)

| Xbox wireless    | OpenBOR action          | Notes |
|------------------|-------------------------|-------|
| D-pad / Left stick | Movement (4-way)      | |
| **A** button     | Jump                    | |
| **B** button     | Attack (primary punch/kick) | |
| **Y** button     | Special / grab          | |
| **X** button     | Attack2 (secondary attack) | |
| **Menu / Start** | Start (insert coin / pause / add player) | |
| **Xbox Guide (center)** | MiSTer OSD       | core's OSD overlay — framework-level, not per-core |

CONF_STR: `J1,Attack,Jump,Special,Attack2,Start;` / `jn,A,B,X,Y,Start;`. MiSTer's `jn` extension uses SNES naming (`jn A`=Xbox B, `jn B`=Xbox A, `jn X`=Xbox Y, `jn Y`=Xbox X), so the defaults above pair `jn A` (Xbox B) → Attack, `jn B` (Xbox A) → Jump, `jn X` (Xbox Y) → Special, `jn Y` (Xbox X) → Attack2.

Both OpenBOR_4086 and OpenBOR_7533 use the IDENTICAL mapping — sister-core swap (4086 ↔ 7533) preserves your input config. All 4 players use the same button layout. Remap buttons from the MiSTer OSD (press F12 or the OSD button on your IO board).

## Pause Menu

Press START during gameplay:

- **Continue** — resume gameplay
- **Options** — adjust Music Volume and SFX Volume with D-pad left/right
- **Reset Pak** — restart the current PAK fresh
- **Quit** — exit to PAK browser

Navigate with D-pad up/down. Press A to confirm, X to go back.

## FPGA Technical Details

Both builds share the same FPGA core, identical timing.

- Resolution: 320×224 active, 420×262 total (exact Sega CD NTSC H40+V28)
- Refresh: 59.92 Hz (exact Sega CD NTSC)
- Pixel clock: 53.693 MHz CLK_VIDEO / 8 = 6.712 MHz (exact Sega CD NTSC, colorburst-derived)
- Pixel format: RGB565 (16 bits per pixel)
- Audio: 48 kHz stereo S16 PCM via DDR3 ring buffer → I2S/SPDIF/DAC
- Double-buffered video via DDR3

## Build Notes

**OpenBOR_4086** — OpenBOR v3.0 Build 4086 from [DCurrent/openbor](https://github.com/DCurrent/openbor) (commit `af23dc9c`). Cross-compiled for MiSTer's ARM Cortex-A9 with SDL 1.2.15 and static linking. Video output goes through a patched SDL dummy driver that writes RGB565 directly to DDR3 for the FPGA to read.

**OpenBOR_7533** — OpenBOR 4.0 Build 7533 from [DCurrent/openbor](https://github.com/DCurrent/openbor) (tag `v7533`). Cross-compiled for the same ARM Cortex-A9 with SDL 2.0.8 (pinned per upstream) and static linking. Video output goes through a patched SDL2 dummy framebuffer driver that writes RGB565 directly to DDR3. The patcher applies eleven targeted source modifications to v7533 (path redirects, R/B blend fix, missing script API names — `cheats`, `PLAYER_MIN_Z`, `dot`-as-`damage_on_landing` alias, etc.) so the engine boots cleanly into the dummy-driver pipeline.

## Credits

- **SumolX** — Created the [first OpenBOR port for MiSTer](https://github.com/SumolX/MiSTer_OpenBOR)
- **OpenBOR Team** — Senile Team, ChronoCrash community, DCurrent, Plombo, Utunnels, White Dragon. Visit [chronocrash.com](https://www.chronocrash.com)
- **Sorgelig & MiSTer Community** — MiSTer FPGA framework

## License

GPL-3.0. See LICENSE. OpenBOR itself is BSD-3-Clause.
