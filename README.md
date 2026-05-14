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

- **Native FPGA video output** — 320×240 @ 59.92 Hz with exact Genesis H40 pixel clock (6.712 MHz from NTSC colorburst crystal). CRT image width matches NES/SNES/Genesis exactly (47.68 µs active time)
- **Native FPGA audio output** — 48 kHz stereo via DDR3 ring buffer, no ALSA
- **CRT support** — scanlines, shadow masks, and analog video output for CRT displays
- **MiSTer OSD integration** — load PAK files from the file browser
- **4-player support** — connect up to 4 controllers, add players by pressing START
- **Custom pause menu** — Continue / Options / Reset Pak / Quit
- **Auto-launch** — OpenBOR starts automatically when the core is loaded
- **Sub-native PAKs scale automatically** (7533) — PAKs with native resolutions higher than 320×240 (Pocket Dimensional Clash 2 at 480×272, He-Man at 960×480, Avengers UBF at 480×272, etc.) are bilinear-downscaled into the 320×240 CRT-correct envelope with letterboxing where needed. Genesis H40 timing stays intact for CRT users.

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
    └── Install_OpenBOR.sh                 Install script
```

Saves and savestates are kept separate between the two engine builds because the on-disk format isn't guaranteed compatible across the OpenBOR 3.x → 4.0 boundary.

## Controls

| Button          | Action                  |
|-----------------|-------------------------|
| A               | Jump                    |
| B               | Attack (primary)        |
| X               | Special / pause back    |
| Y               | Attack2                 |
| D-pad / Analog  | Move                    |
| Start           | Pause / add player      |
| Menu button     | MiSTer OSD menu         |

All 4 players use the same button layout. Remap buttons from the MiSTer OSD (press F12 or the OSD button on your IO board).

## Pause Menu

Press START during gameplay:

- **Continue** — resume gameplay
- **Options** — adjust Music Volume and SFX Volume with D-pad left/right
- **Reset Pak** — restart the current PAK fresh
- **Quit** — exit to PAK browser

Navigate with D-pad up/down. Press A to confirm, X to go back.

## FPGA Technical Details

Both builds share the same FPGA core, identical timing.

- Resolution: 320×240 active, 420×262 total (exact Genesis H40)
- Refresh: 59.92 Hz (exact Genesis NTSC)
- Pixel clock: 53.693 MHz CLK_VIDEO / 8 = 6.712 MHz (exact Genesis H40, NTSC colorburst-derived)
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
