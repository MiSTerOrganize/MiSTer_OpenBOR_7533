# PAK native-resolution census — all 450 PAKs

Generated 2026-05-30 by an **engine-exact** `video.txt` parser that replicates v7533
`openbor.c:48609-48806` line by line (LAST `video` directive wins, mirroring the engine's
`while (pos < size)` loop; custom `video WxH` -> literal dims, mode 255; preset `video N`
(0-6) -> switch table; no `video.txt` or unreadable -> mode 0 = 320x240). Confidence ~99%;
the residual 1% is edge cases that would need each PAK actually loaded to settle.

**Why this file exists:** it is the authoritative library-wide input for any change whose
correctness depends on native resolution — most immediately the Tier-B band geometry
(`tierb_phase1_architecture.md` section 8), where it caught a design error that would have
shipped broken on 8 PAKs. Keep it. Regenerate it if the PAK library changes.

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
| 480x272 | 94 | preset 1 | Avengers UBF, Pocket Dimensional Clash 2, Justice League Legacy, ... |
| 432x243 | 1 | custom | Mortal Kombat Outworld Assassins |
| 400x300 | 1 | custom | Gunman |
| 384x224 | 1 | custom | Street Fighter '89 - The Final Fight [Demo] (W>320, H<240) |
| 336x240 | 1 | custom | Guardians of the Hood II |
| 320x240 | 286 | 158 with no `video.txt` + 128 `video 0` | **A Tale of Vengeance**, **TMNT Rescue Palooza**, ... |
| 256x224 | 2 | custom | Legend of the Double Dragon, Ultimate Double Dragon |
| 240x224 | 1 | custom | Final Double Dragon |
| 240x200 | 1 | custom | TMNT - Fall of the Foot Clan DX [Demo] |
| | **450** | | |

**Bounds that matter to any renderer:** max width **1600**, max height **900**, max area
**1,440,000 px** (both from Lust Rush). 290 PAKs (64%) are at or below 320x240 in both
dimensions; 160 exceed the 320x240 default in at least one dimension.

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

## Full PAK lists for the non-default tiers

**1600x900** — Lust Rush

**960x540** — Faderhead's Fist Full of Fuck You · Monster Girl Dimensions · Samurai
Warriors · Streets of Rage 4 - Silent Storm [Demo]

**960x480 / 960x475** — Bad Ass Babes Episode One · Dragon Ball Z Tournament (475) ·
He-Man and the Masters of the Universe

**800x480** — Kung Fu Masters and the Wrath of the Gods
**720x480** — Urban Lockdown
**640x640** — Ruins of the Deep · Secrets of the Deep
**640x360** — Bearz · Rocko's Modern Life
**500x650** — Xelam
**480x360** — Final Fight - Revival of Rage
**432x243** — Mortal Kombat Outworld Assassins
**400x300** — Gunman
**384x224** — Street Fighter '89 - The Final Fight [Demo]
**336x240** — Guardians of the Hood II
**256x224** — Legend of the Double Dragon · Ultimate Double Dragon
**240x224** — Final Double Dragon
**240x200** — TMNT - Fall of the Foot Clan DX [Demo]

**640x480 (46)** — Aliens Clash · Barshen Bash · Barshen Below · Barshen Border · Barshen
Breakout · Bird Bunch · Blue Bullet Bintang · Braller · BroomStickBot · Cherry On Top ·
Coin Champion · Cyber Robot · Double Dragon Reloaded Alternate · Double Dragon Revival ·
Eerie Erratic Expedition · Egi The Egg · Escape From Guha · Final Fight Special Edition ·
Fire Leaf Water · Fruits Frenzy · Fruits Survivors · Go Bat · Happy Heart · Honeybee
Harvest · Immortal Monster · Langit Lands · Last Life · Lift · Minetroid · Nightmare on Elm
Street - Dreams of Rage · Pew Pew Dadu · Plump on the Stump · Project Moon [Demo] · Puzzle
Puss · Raider & Reaper · Raiders Rush · Rainbow · Robo Magi · Robz Rush · Scorer Horror ·
Snail N Turtle · Steampunk Raiders · Tabib · Tanks Vs Tanks · Teenage Mutant Ninja Turtles
(NOT TMNT-RP) · Way of Martial Arts

**480x272 (94)** — A Saga de Ryu · Art Of Figting - Trouble In South Town · Art of Fighting
- Beats of Rage Remix III · Avengers - United Battle Force · Bad School Girls ·
BattleManiacs Bare Knuckle · Beat 'Em Up Ultimate Alliance · Bishojo Dimensional Chaos ·
Briga de Rua · Briga de Rua 2 - Vanessa · Briga de Rua 3 - Killer Instinct · Burning Fox,
The · Capcom Pocket Brawl [Beta] · City of Kaos, The · Cosmic Damage · Demon's Hand [Demo]
· Dragon Ball Z - Attack of Saiyans · Dungeons & Dragons - Knights & Dragons Final Cut ·
Dungeons & Dragons - Knights & Dragons The Endless Quest · Escape of the Ages · Evil Dead ·
Fatal Fury Final · Fighting Street · Final Fight - Heroes · Final Fight Alpha · Final Fight
Boss · Final Fight Gold Champion Edition · Fire Hearts · Fists of Legendary Heroes · GI Joe
- Attack On Cobra Island · Garou - Rage of the Wolves · Golden Axe Legend · Golden Axe
Returns · Gudule · Gudule - Fist in ya Face! · Hiryu No Ken [Demo] · Hokuto no Ken Fury
Road · Ikari Warriors 2010 + Mutation Nation · Justice League Legacy · Justice League
United · King of Fighters Zillion, The - Another Road · King of Fighters, The - Beat 'Em Up
Plus · Legend of Korr - Path of Destruction · M@skaku, The - Wonder Momo '09 · Martial
Masters - New Legend · Marvel - Infinity War · Marvel First Alliance · Marvel First
Alliance 2 · Marvel War of the Gems · Marvel vs Capcom · Masters Of The Universe - Eternian
Battle · Metal Slug Beat Em Up · Monster Jam [Demo] · Mortal Kombat - The Chosen One ·
Ninja - Stealth Assassins · Ogres Mayhem · POW 2010 · Pocket Dimensional Clash 2 · Pokemon
Rumble 2D · Rage Force Briga De Rua · Red Earth · Rescue Command - Against the Amazon Girls
· Rescue Command Ep. 1 - Escaping from Bad Girls Island · Rescue Command Ep. 2 Attack on
Amazon Island · Resident Evil Survive · Retro Gamer Adventure · S.C.U. - Special Criminal
Unit · Sega Brawlers Megamix · Simpsons, The - Treehouse of Horror · Snestalgia · Sonic
Adventure - Revolution · Sonic Defense · Sonic Super Jam · Street Fighter Taiwan · Street
Fighter Vs. The King of Fighters · Streets of Rage 2X · Streets of Rage Furia Massiva ·
Streets of Rage Legacy · Streets of Rage X2 Megamix · Streets of Vendetta - Cracoland War ·
Super Fightin' Spirit · Super Final Fight Gold · Super Final Fight Gold Plus LNS · Super
Mario Brawl · Super Universe Brawl · Symphonia Battalion - Well Fight Together · Tekken -
The Devils Rage · The Suffering In Me [Beta Demo] · Touhou Madness Wrath · Touhou Shooter ·
Ultimate Super Mega Beatdown · Vermilion Sword - The Legend Of Calibur · X-Men Hunter for
Mutants · XuanYuan Sword
