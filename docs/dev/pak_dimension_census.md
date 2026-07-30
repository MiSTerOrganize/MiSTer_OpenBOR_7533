# PAK native-resolution census — all 450 PAKs

**VERIFIED 2026-07-29** by `tools/harness/pak_videoscan.py` run over the local 450-PAK
library. That scanner is engine-exact: it reads each PAK's packfile directory
(`gamelib/packfile.h` `pnamestruct`, offset in the last 4 bytes), extracts `data/video.txt`,
and parses it with v7533's own rules — `openbor.c` `ParseArgs` tokenizing (`#`, CR, LF, NUL
end the line; quotes protect), the `while (pos < size)` loop where the **LAST `video`
directive wins**, `strchr(value, 'x')` for the custom `WxH` form (**lowercase x only**),
`atoi` for the preset form, and the `switch (videoMode)` table at `openbor.c:48692`.

```
  preset 0 = 320x240   1 = 480x272   2 = 640x480   3 = 720x480
         4 = 800x480   5 = 800x600   6 = 960x540   255 = custom WxH
  no video.txt / unreadable -> mode 0 = 320x240 ;  mode 7+ -> engine SHUTS DOWN
```

Result: **450/450 PAKs resolved, 20 distinct resolutions, 0 invalid modes.**
Raw per-PAK data: `tools/harness/pak_videoscan_2026-07-29.tsv`.

**Why this file exists:** it is the authoritative library-wide input for any change whose
correctness depends on native resolution — most immediately the Tier-B band geometry
(`tierb_phase1_architecture.md` section 8), where it caught a design error that would have
shipped broken on 8 PAKs. Regenerate with `pak_videoscan.py` if the library changes. Run it
LOCALLY only, never on the MiSTer.

## Supersedes the 2026-05-30 checklist — 5 corrections

This replaces `step27_lite_pak_test_list.txt` (2026-05-30), which self-reported ~99%
confidence. It was right on 445/450; the verified scan corrects **5**:

| PAK | checklist | verified | why |
|---|---|---|---|
| God of War | 320x240 | **480x272** | `video     1#400x240#1` — the **comment contains an `x`**. A parser that looks for `'x'` before stripping the `#` comment sees `1#400x240#1`, matches the `x`, and yields a nonsense `1x240`. The engine tokenizes `#` FIRST, so the value is just `1` -> mode 1. |
| Escape From Cartoon Hell | 320x240 | **480x272** | `video 1#0 = ...` — comment directly abutting the value |
| Shiva & Lisa 3 | 320x240 | **480x272** | same `1#0` form |
| The Beast Within - Divine Comedy | 320x240 | **480x272** | same `1#0` form |
| **Ultimate Super Mega Beatdown** | 480x272 | **320x240** | cart is authored `video = 1`. `GET_ARG(1)` is then **`"="`**, `strchr("=", 'x')` is NULL, `atoi("=")` is **0** -> mode 0. The engine silently renders it at 320x240; the author's intent was 480x272 but that is not what runs. |

(A sixth apparent difference is not one: the real filename is `Beat 'Em Up  Ultimate
Alliance` with a **double space**, written single-spaced in the checklist.)

**Two implementation traps to carry forward:** strip the `#` comment BEFORE testing for the
`x` separator, and note that `video = 1` is mode 0, not mode 1. Both are engine behaviour we
must match exactly, not defects to fix.

## Distinct resolutions

| W x H | PAKs | source form | notable |
|---:|---:|---|---|
| 1600x900 | 1 | custom | Lust Rush — **widest in the library** |
| 960x540 | 4 | preset 6 | Faderhead's Fist Full of Fuck You, Monster Girl Dimensions, Samurai Warriors, SoR4 Silent Storm [Demo] |
| 960x480 | 2 | custom | **He-Man and the Masters of the Universe** (Tier-B primary target), Bad Ass Babes Episode One |
| 960x475 | 1 | custom | Dragon Ball Z Tournament |
| 800x480 | 1 | preset 4 | Kung Fu Masters and the Wrath of the Gods |
| 720x480 | 1 | preset 3 | Urban Lockdown |
| 640x640 | 2 | custom | Ruins of the Deep, Secrets of the Deep |
| 640x480 | 46 | preset 2 | Aliens Clash, Double Dragon Reloaded Alternate, Final Fight Special Edition, ... |
| 640x360 | 2 | custom | Bearz, Rocko's Modern Life |
| 500x650 | 1 | custom | Xelam — **tallest in the library** |
| 480x360 | 1 | custom | Final Fight - Revival of Rage |
| 480x272 | 97 | preset 1 | Avengers UBF, Pocket Dimensional Clash 2, Justice League Legacy, ... |
| 432x243 | 1 | custom | Mortal Kombat Outworld Assassins |
| 400x300 | 1 | custom | Gunman |
| 384x224 | 1 | custom | Street Fighter '89 - The Final Fight [Demo] (W>320, H<240) |
| 336x240 | 1 | custom | Guardians of the Hood II |
| 320x240 | 283 | no `video.txt`, `video 0`, or a malformed directive | **A Tale of Vengeance**, **TMNT Rescue Palooza**, ... |
| 256x224 | 2 | custom | Legend of the Double Dragon, Ultimate Double Dragon |
| 240x224 | 1 | custom | Final Double Dragon |
| 240x200 | 1 | custom | TMNT - Fall of the Foot Clan DX [Demo] |
| | **450** | | |

**Bounds that matter to any renderer:** max width **1600**, max height **900**, max area
**1,440,000 px** (both from Lust Rush). 287 PAKs (64%) are at or below 320x240 in both
dimensions; **163** exceed the 320x240 default in at least one dimension.

## Heights, and why they matter for banding

Distinct heights: 900, 650, 640, 540, 480, 475, 360, 300, 272, 243, 240, 224, 200.

Several are **coprime or near-coprime with the 224-line output**, which is what invalidated
the first Tier-B band rule:

| H | gcd(H, 224) | H / gcd |
|---:|---:|---:|
| 475 | 1 | **475** |
| 243 | 1 | **243** |
| 650 | 2 | 325 |
| 900 | 4 | 225 |
| 540 | 4 | 135 |
| 300 | 4 | 75 |
| 360 | 8 | 45 |
| 200 | 8 | 25 |
| 640 | 32 | 20 |
| 272 | 16 | 17 |
| 480 | 32 | 15 |
| 240 | 16 | 15 |
| 224 | 224 | 1 |

A rule requiring a band to be a multiple of `H / gcd(H, 224)` is therefore **unusable**:
for 960x475 and 432x243 the minimum legal band is the entire frame. See
`tierb_phase1_architecture.md` section 8 for the corrected, fully general rule.

## Full PAK lists (generated from the verified scan)

Every resolution except the 283-PAK 320x240 default, which is everything not listed here.

**1600x900 (1)** — Lust Rush

**960x540 (4)** — Faderhead's Fist Full of Fuck You · Monster Girl Dimensions · Samurai Warriors · Streets of Rage 4 - Silent Storm [Demo]

**960x480 (2)** — Bad Ass Babes Episode One · He-Man and the Masters of the Universe

**960x475 (1)** — Dragon Ball Z Tournament

**640x640 (2)** — Ruins of the Deep · Secrets of the Deep

**800x480 (1)** — Kung Fu Masters and the Wrath of the Gods

**720x480 (1)** — Urban Lockdown

**500x650 (1)** — Xelam

**640x480 (46)** — Aliens Clash · Barshen Bash · Barshen Below · Barshen Border · Barshen Breakout · Bird Bunch · Blue Bullet Bintang · Braller · BroomStickBot · Cherry On Top · Coin Champion · Cyber Robot · Double Dragon Reloaded Alternate · Double Dragon Revival · Eerie Erratic Expedition · Egi The Egg · Escape From Guha · Final Fight Special Edition · Fire Leaf Water · Fruits Frenzy · Fruits Survivors · Go Bat · Happy Heart · Honeybee Harvest · Immortal Monster · Langit Lands · Last Life · Lift · Minetroid · Nightmare on Elm Street - Dreams of Rage · Pew Pew Dadu · Plump on the Stump · Project Moon [Demo] · Puzzle Puss · Raider & Reaper · Raiders Rush · Rainbow · Robo Magi · Robz Rush · Scorer Horror · Snail N Turtle · Steampunk Raiders · Tabib · Tanks Vs Tanks · Teenage Mutant Ninja Turtles · Way of Martial Arts

**640x360 (2)** — Bearz · Rocko's Modern Life

**480x360 (1)** — Final Fight - Revival of Rage

**480x272 (97)** — A Saga de Ryu · Art Of Figting - Trouble In South Town · Art of Fighting - Beats of Rage Remix III · Avengers - United Battle Force · Bad School Girls · BattleManiacs Bare Knuckle · Beat 'Em Up  Ultimate Alliance · Bishojo Dimensional Chaos · Briga de Rua · Briga de Rua 2 - Vanessa · Briga de Rua 3 - Killer Instinct · Burning Fox, The · Capcom Pocket Brawl [Beta] · City of Kaos, The · Cosmic Damage · Demon's Hand [Demo] · Dragon Ball Z - Attack of Saiyans · Dungeons & Dragons - Knights & Dragons Final Cut · Dungeons & Dragons - Knights & Dragons The Endless Quest · Escape From Cartoon Hell · Escape of the Ages · Evil Dead · Fatal Fury Final · Fighting Street · Final Fight - Heroes · Final Fight Alpha · Final Fight Boss · Final Fight Gold Champion Edition · Fire Hearts · Fists of Legendary Heroes · GI Joe - Attack On Cobra Island · Garou - Rage of the Wolves · God of War · Golden Axe Legend · Golden Axe Returns · Gudule · Gudule - Fist in ya Face! · Hiryu No Ken [Demo] · Hokuto no Ken Fury Road · Ikari Warriors 2010 + Mutation Nation · Justice League Legacy · Justice League United · King of Fighters Zillion, The  - Another Road · King of Fighters, The - Beat 'Em Up Plus · Legend of Korr - Path of Destruction · M@skaku, The - Wonder Momo '09 · Martial Masters - New Legend · Marvel - Infinity War · Marvel First Alliance · Marvel First Alliance 2 · Marvel War of the Gems · Marvel vs Capcom · Masters Of The Universe - Eternian Battle · Metal Slug Beat Em Up · Monster Jam [Demo] · Mortal Kombat - The Chosen One · Ninja - Stealth Assassins · Ogres Mayhem · POW 2010 · Pocket Dimensional Clash 2 · Pokemon Rumble 2D · Rage Force Briga De Rua · Red Earth · Rescue Command - Against the Amazon Girls · Rescue Command Ep. 1 - Escaping from Bad Girls Island · Rescue Command Ep. 2 Attack on Amazon Island · Resident Evil Survive · Retro Gamer Adventure · S.C.U. - Special Criminal Unit · Sega Brawlers Megamix · Shiva & Lisa 3 · Simpsons, The - Treehouse of Horror · Snestalgia · Sonic Adventure - Revolution · Sonic Defense · Sonic Super Jam · Street Fighter Taiwan · Street Fighter Vs. The King of Fighters · Streets of Rage 2X · Streets of Rage Furia Massiva · Streets of Rage Legacy · Streets of Rage X2 Megamix · Streets of Vendetta - Cracoland War · Super Fightin' Spirit · Super Final Fight Gold · Super Final Fight Gold Plus LNS · Super Mario Brawl · Super Universe Brawl · Symphonia Battalion - Well Fight Together · Tekken - The Devils Rage · The Beast Within - Divine Comedy · The Suffering In Me [Beta Demo] · Touhou Madness Wrath · Touhou Shooter · Vermilion Sword - The Legend Of Calibur · X-Men Hunter for Mutants · XuanYuan Sword

**400x300 (1)** — Gunman

**432x243 (1)** — Mortal Kombat Outworld Assassins

**384x224 (1)** — Street Fighter '89 - The Final Fight [Demo]

**336x240 (1)** — Guardians of the Hood II

**256x224 (2)** — Legend of the Double Dragon · Ultimate Double Dragon

**240x224 (1)** — Final Double Dragon

**240x200 (1)** — Teenage Mutant Ninja Turtles - Fall of the Foot Clan DX [Demo]

## Tier-B band geometry, verified against every resolution

Derived by the corrected rule in `tierb_phase1_architecture.md` section 8.2 (a band is a
whole number of OUTPUT rows; `R` chosen per PAK from `BAND_BUDGET_PX = 32,768`):

| W x H | PAKs | R (out rows/band) | src lines | band px | bands/frame |
|---:|---:|---:|---:|---:|---:|
| 1600x900 | 1 | 4 | 17 | 27,200 | 56 |
| 960x540 | 4 | 14 | 34 | 32,640 | 16 |
| 960x480 | 2 | 15 | 33 | 31,680 | 15 |
| 960x475 | 1 | 16 | 34 | 32,640 | 14 |
| 640x640 | 2 | 17 | 49 | 31,360 | 14 |
| 800x480 | 1 | 18 | 39 | 31,200 | 13 |
| 720x480 | 1 | 21 | 45 | 32,400 | 11 |
| 500x650 | 1 | 22 | 64 | 32,000 | 11 |
| 640x480 | 46 | 23 | 50 | 32,000 | 10 |
| 640x360 | 2 | 31 | 50 | 32,000 | 8 |
| 480x360 | 1 | 42 | 68 | 32,640 | 6 |
| 480x272 | 97 | 56 | 68 | 32,640 | 4 |
| 400x300 | 1 | 60 | 81 | 32,400 | 4 |
| 432x243 | 1 | 69 | 75 | 32,400 | 4 |
| 384x224 | 1 | 85 | 85 | 32,640 | 3 |
| 336x240 | 1 | 90 | 96 | 32,256 | 3 |
| 320x240 | 283 | 95 | 102 | 32,640 | 3 |
| 256x224 | 2 | 128 | 128 | 32,768 | 2 |
| 240x224 | 1 | 136 | 136 | 32,640 | 2 |
| 240x200 | 1 | 153 | 136 | 32,640 | 2 |

Peak band occupancy **32,768 px** (64 KB, 102 M10K double-buffered = 21% of the 486 free).
**All 450 PAKs fit**, including the 1600-wide outlier, which simply takes fewer output rows
per band. Max width 1600, max height 900 (both Lust Rush).
