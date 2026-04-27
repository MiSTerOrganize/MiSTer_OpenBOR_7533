# MiSTer OpenBOR (4.0 Build 7533)

**Status: stable** — confirmed working on real MiSTer hardware with the modern OpenBOR PAK catalog (Pocket Dimensional Clash 2, He-Man and the Masters of the Universe, Avengers: United Battle Force, plus the broader 4086-era and earlier collections).

Hybrid ARM+FPGA OpenBOR beat-em-up engine for MiSTer FPGA with native video and audio output. Runs the modern OpenBOR 4.0 PAK collections (TMNT: Rescue-Palooza, Final Fight LNS Ultimate, Avengers UBF v2.7+, Zvitor / RVGM sets), and remains backward compatible with PAKs targeting earlier builds. Inspired by [SumolX](https://github.com/SumolX/MiSTer_OpenBOR)'s original MiSTer OpenBOR port.

For older PAKs that depend on legacy 3979/4086-era scripting quirks, see the sister repo [MiSTer_OpenBOR_4086](https://github.com/MiSTerOrganize/MiSTer_OpenBOR_4086). The two cores install side by side and keep separate `Paks/`, `saves/`, `savestates/`, and `config/` directories so they never collide. Hot-swap between PAKs (and between the two cores) works.

## Features

- **OpenBOR 4.0 Build 7533** — latest stable release (May 2025), unlocks ~6 years of fangames blocked on older ports
- **SDL 2.0** — modern game controller abstraction with stable controller IDs across replug
- **Native FPGA video output** — 320×240 @ 59.92 Hz with exact Genesis H40 pixel clock (6.712 MHz from NTSC colorburst crystal). CRT image width matches NES/SNES/Genesis exactly (47.68 µs active time)
- **Native FPGA audio output** — 48 kHz stereo via DDR3 ring buffer, no ALSA
- **CRT support** — scanlines, shadow masks, and analog video output for CRT displays
- **MiSTer OSD integration** — load PAK files from the file browser
- **4-player support** — connect up to 4 controllers, add players by pressing START
- **Custom pause menu** — Continue / Options / Reset Pak / Quit
- **Auto-launch** — OpenBOR starts automatically when the core is loaded

## Quick Install

1. Copy `Scripts/Install_OpenBOR.sh` to `/media/fat/Scripts/` on your MiSTer SD card
2. From the MiSTer main menu, go to Scripts and run **Install_OpenBOR**
3. Place your `.pak` game modules in `games/OpenBOR_7533/Paks/`
4. Load **OpenBOR_7533** from the console menu to play

The install script downloads and installs everything: the FPGA core, ARM binary, daemon, and documentation.

## Manual Install

Extract the release zip to the root of your MiSTer SD card (`/media/fat/`):

```
/media/fat/
├── _Other/
│   └── OpenBOR_7533_YYYYMMDD.rbf          FPGA core (dated build)
├── docs/
│   └── OpenBOR_7533/
│       └── README.md                       Documentation
├── games/
│   └── OpenBOR_7533/
│       ├── OpenBOR                         ARM binary (engine)
│       ├── openbor_7533_daemon.sh          Auto-launch daemon
│       └── Paks/                           Place your .pak game modules here
├── logs/
│   └── OpenBOR_7533/                       Debug logs
├── saves/
│   └── OpenBOR_7533/                       Game saves (created automatically)
└── Scripts/
    └── Install_OpenBOR.sh                  Install script
```

## Game Modules (PAK Files)

Place your OpenBOR PAK files in `/media/fat/games/OpenBOR_7533/Paks/`.

The PAK format is fully backward compatible — PAKs from 4086 and earlier collections continue to work. Modern PAKs that need post-4086 script commands (Build 6000+ era) require this build.

**Sub-native PAKs scale automatically:** PAKs with native resolutions higher than 320×240 (Pocket Dimensional Clash 2 at 480×272, He-Man at 960×480, Avengers UBF at 480×272, etc.) are bilinear-downscaled into the 320×240 CRT-correct envelope with letterboxing where needed. This keeps the Genesis H40 timing intact for CRT users.

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

- Resolution: 320×240 active, 420×262 total (exact Genesis H40)
- Refresh: 59.92 Hz (exact Genesis NTSC)
- Pixel clock: 53.693 MHz CLK_VIDEO / 8 = 6.712 MHz (exact Genesis H40, NTSC colorburst-derived)
- Pixel format: RGB565 (16 bits per pixel)
- Audio: 48 kHz stereo S16 PCM via DDR3 ring buffer → I2S/SPDIF/DAC
- Double-buffered video via DDR3

## OpenBOR Build Info

This core runs OpenBOR 4.0 Build 7533 from [DCurrent/openbor](https://github.com/DCurrent/openbor) (tag `v7533`). Cross-compiled for MiSTer's ARM Cortex-A9 with SDL 2.0.8 (pinned per upstream) and static linking. Video output goes through a patched SDL2 dummy framebuffer driver that writes RGB565 directly to DDR3 for the FPGA to read. The patcher applies eleven targeted source modifications to v7533 (path redirects, R/B blend fix, missing script API names — `cheats`, `PLAYER_MIN_Z`, `dot`-as-`damage_on_landing` alias, etc.) so the engine boots cleanly into the dummy-driver pipeline.

## Credits

- **SumolX** — Created the [first OpenBOR port for MiSTer](https://github.com/SumolX/MiSTer_OpenBOR)
- **OpenBOR Team** — Senile Team, ChronoCrash community, DCurrent, Plombo, Utunnels, White Dragon. Visit [chronocrash.com](https://www.chronocrash.com)
- **Sorgelig & MiSTer Community** — MiSTer FPGA framework

## License

GPL-3.0. See LICENSE. OpenBOR itself is BSD-3-Clause.
