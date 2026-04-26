# MiSTer OpenBOR (Build 4086)

Hybrid ARM+FPGA OpenBOR beat-em-up engine for MiSTer FPGA with native video and audio output. Runs the ~300-game community PAK collections. Inspired by [SumolX](https://github.com/SumolX/MiSTer_OpenBOR)'s original MiSTer OpenBOR port.

## Features

- **OpenBOR Build 4086** — the most compatible build for community PAK packs (~200+ games)
- **Native FPGA video output** — 320×240 @ 59.92Hz with exact Genesis H40 pixel clock (6.712 MHz from NTSC colorburst crystal). CRT image width matches NES/SNES/Genesis exactly (47.68 µs active time)
- **Native FPGA audio output** — 48 kHz stereo via DDR3 ring buffer, no ALSA
- **CRT support** — scanlines, shadow masks, and analog video output for CRT displays
- **MiSTer OSD integration** — load PAK files from the file browser
- **4-player support** — connect up to 4 controllers, add players by pressing START
- **Custom pause menu** — Continue / Options / Reset Pak / Quit
- **Auto-launch** — OpenBOR starts automatically when the core is loaded

## Quick Install

1. Copy `Scripts/Install_OpenBOR.sh` to `/media/fat/Scripts/` on your MiSTer SD card
2. From the MiSTer main menu, go to Scripts and run **Install_OpenBOR**
3. Place your `.pak` game modules in `games/OpenBOR_4086/Paks/`
4. Load **OpenBOR_4086** from the console menu to play

The install script downloads and installs everything: the FPGA core, ARM binary, daemon, and documentation.

## Manual Install

Extract the release zip to the root of your MiSTer SD card (`/media/fat/`):

```
/media/fat/
├── _Other/
│   └── OpenBOR_4086_YYYYMMDD.rbf          FPGA core (dated build)
├── docs/
│   └── OpenBOR_4086/
│       └── README.md                      Documentation
├── games/
│   └── OpenBOR_4086/
│       ├── OpenBOR                        ARM binary (engine)
│       ├── openbor_4086_daemon.sh         Auto-launch daemon
│       └── Paks/                          Place your .pak game modules here
├── logs/
│   └── OpenBOR_4086/                      Debug logs
├── saves/
│   └── OpenBOR_4086/                      Game saves (created automatically)
└── Scripts/
    └── Install_OpenBOR.sh                 Install script
```

## Game Modules (PAK Files)

Place your OpenBOR PAK files in `/media/fat/games/OpenBOR_4086/Paks/`.

Build 4086 is the most popular build for the large community PAK collections (~300 games on Archive.org, Retrobat, Batocera, Launchbox). Older PAKs built for 3366–3979 are backward-compatible.

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

This core runs OpenBOR v3.0 Build 4086 from [DCurrent/openbor](https://github.com/DCurrent/openbor) (commit af23dc9c). Cross-compiled for MiSTer's ARM Cortex-A9 with SDL 1.2.15 and static linking. Video output goes through a patched SDL dummy driver that writes RGB565 directly to DDR3 for the FPGA to read.

## Credits

- **SumolX** — Created the [first OpenBOR port for MiSTer](https://github.com/SumolX/MiSTer_OpenBOR)
- **OpenBOR Team** — Senile Team, ChronoCrash community, DCurrent, Plombo, Utunnels, White Dragon. Visit [chronocrash.com](https://www.chronocrash.com)
- **Sorgelig & MiSTer Community** — MiSTer FPGA framework

## License

GPL-3.0. See LICENSE. OpenBOR itself is BSD-3-Clause.
