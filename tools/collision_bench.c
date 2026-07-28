/*
 * collision_bench.c -- OpenBOR_7533 entity-collision kernel micro-benchmark (A9).
 *
 * Measures the per-frame cost + scaling of the "arrange" fps bucket on the real
 * Cortex-A9, WITHOUT loading a PAK or playing. The bottleneck is the O(N^2)
 * pass arrange_ents() -> ent_post_update() -> check_entity_collision_for(): for
 * EACH existing entity it loops over ALL entities, so cost grows ~N^2. On heavy
 * PAKs this is ~153 ms/frame. Isolating it here makes the optimization push
 * (a spatial grid) a tight edit -> build -> ssh -> number loop.
 *
 * Kernel replicated faithfully (no engine headers -- inlined like blend_bench):
 *   check_entity_collision_for (openbor.c v7533 line 35523 pristine / Step-14 patched):
 *     for each entity ent (with a collision_entity animation):
 *       for i in 0..ent_max:
 *         target = ent_list[i]
 *         B cull : skip if !exists || target==ent || no collision_entity
 *         E cull : skip if |dx|>256 || |dz|>256   (cheap rect, Step 14)
 *         on survivor: check_entity_collision(ent,target) (line 35291):
 *           max_collisons^2 nested loop (=1x1 typical), per instance:
 *             z-depth arithmetic + diff(z1,z2)>zdist z-cull
 *             direction-dependent box-corner computation
 *             4 AABB overlap rejection tests
 *           first overlap -> set collided pointers, return.
 *
 * ---------------------------------------------------------------------------
 * THREE VARIANTS, measured head-to-head + proven behaviourally IDENTICAL
 * ---------------------------------------------------------------------------
 *   shipped    : the kernel exactly as it ships today (Step-14 B+E cull, 256px).
 *   tight      : same index-order linear scan, but the fixed 256 px rect cull
 *                is replaced by a per-PAIR conservative bound derived from the
 *                two entities' own hitbox extents. No grid.
 *   grid       : 1-D uniform bucket grid over x, rebuilt every pass (that cost
 *                is inside the timed region), 3-cell window, candidates
 *                traversed in ASCENDING ent_list index order via a 3-way merge
 *                so the early-return picks the exact same target as the linear
 *                scan. Falls back to the shipped scan when the level fits in
 *                <= 3 cells (window would be everything anyway).
 *   grid+tight : both. Measured because "fewer candidates AND fewer checks"
 *                sounds strictly better -- it is not; see FINDINGS below.
 *
 * FINDINGS -- MEASURED ON THE REAL A9 (2026-07-28, dev MiSTer, taskset 0x01,
 * MiSTer=1 / no hybrid core running / load 0.65 -> no contention):
 *
 *   scene   N=400   shipped   tight    grid     grid+tight
 *   screen          2.2988    1.7910   1.5498   1.5171   ms   (grid 1.48x)
 *   wide            6.5027    8.0739   1.6612   1.6385   ms   (grid 3.91x)
 *   dense           0.9443    0.8631   0.9567   0.8740   ms   (grid 0.99x)
 *
 *   - The bucket is dominated by the O(N^2) *visit* loop, NOT by
 *     check_entity_collision: that function early-outs at its own z-cull for
 *     almost every surviving pair, so a pair costs little more than a visit.
 *   - `grid` (fewer visits, shipped body unchanged) is the lever, and its win
 *     GROWS with entity count on a scrolling level: 2.61x @ N=50 -> 3.28x @ 100
 *     -> 3.65x @ 200 -> 3.91x @ 400. At N=400 that is 6.50 -> 1.66 ms, i.e.
 *     ~4.8 ms/frame returned (~29% of a 16.7 ms frame).
 *   - `tight` is a NET LOSS on the A9 exactly as on x86 (0.78-0.84x on the wide
 *     scene): the extra per-visit arithmetic costs more than the checks it
 *     avoids. **QEMU said the opposite (1.4-1.7x win) -- it models neither
 *     cache nor branch prediction. Do not trust QEMU for this kind of call.**
 *   - `grid+tight` is NOT meaningfully better than `grid` alone (3.97x vs 3.91x
 *     wide, 1.52x vs 1.48x screen). Ship the grid alone; stacking buys ~1% for
 *     a per-entity extent array in the hot path. Do not stack them.
 *   - Still O(N^2) in a FIXED-size level (doubling N doubles density, so the
 *     window fills up too). The grid cuts the constant by the level-span /
 *     window-span ratio; it does not change the exponent.
 *   - The dense scene confirms the bypass works: 0.99x, i.e. no regression when
 *     the level is too small for bucketing to pay.
 *
 * EQUIVALENCE (why tight/grid are behaviour-preserving, not approximations):
 *   1. check_entity_collision() has NO side effects when it returns 0 -- the
 *      movex/movez resolve and execute_onentitycollision_script() both sit
 *      after `if(!collision_found) return 0;`. So *not calling* it on a pair
 *      that would not have collided is invisible to the engine.
 *   2. The tight bound is conservative for overlap. With box offsets x/width
 *      (far-corner offsets, not sizes) the x-extent from an entity's origin is
 *      <= ext_x = max(|x|,|width|); overlap therefore requires
 *          |pos.x_e - pos.x_t| <= ext_x_e + ext_x_t + |movex_e| + |movex_t|.
 *      The z-cull (diff(z1,z2) > zdist) similarly requires
 *          |pos.z_e - pos.z_t| <= ext_z_e + ext_z_t + |movez_e| + |movez_t|
 *      with ext_z = 2*max(|z_background|,|z_foreground|) (covers both the
 *      zdepth half-span and the z_background shift). Bounds are also capped at
 *      the shipped 256 px, so the cull is never WIDER than what ships.
 *   3. Ordering: the shipped scan returns the FIRST colliding target in
 *      ent_list index order. The grid therefore emits its candidates in
 *      ascending index order (per-cell runs are built ascending by a counting
 *      sort; the 3x3 neighbourhood is merged k-way). Same winner, same single
 *      set of side effects.
 *   The bench asserts this per entity, per scene, per N: collided-target index
 *   AND final movex/movez must match `shipped` exactly, or the run FAILS and
 *   exits non-zero.
 *
 * Scenes (all deterministic -- positions derived arithmetically from the index,
 * no Math.random): "screen" 320x224 = the original single-screen scene (keeps
 * continuity with the published A9 baseline); "wide" 1920x224 = a scrolling
 * level, which is what a heavy PAK with many live entities actually looks like;
 * "dense" 160x112 = the grid's worst case (everything inside one neighbourhood).
 *
 * NOTE (2026-07-28 measurement-fidelity fix): the previous revision counted the
 * survivor pairs with a duplicate B+E scan INSIDE the timed region, which
 * inflated the reported time by roughly the cost of one extra cull pass. Pair
 * counting is now done untimed, so `shipped` numbers here are lower than the
 * previously published 8.85 ms @ N=400 figure. Re-baseline on the A9.
 *
 * Not modelled: entitypushing (PUSH_FACTOR) and the onentitycollision script
 * callback -- the bench models the common non-pushing path. Both sit after the
 * collision_found gate, so they do not affect the equivalence argument; note
 * that a pushing PAK *reads* ent->collided_entity inside the resolve, which is
 * why the outer loop order and the ascending candidate order must be preserved
 * (they are).
 *
 * 🛑 BEFORE PORTING THE GRID INTO THE ENGINE, note the pushing caveat: the cell
 * size is sized from each entity's |movex| AT PASS START, which is sound here
 * only because the non-pushing resolve can only ever set movex/movez to 0 (the
 * bound can shrink, never grow). With entitypushing the resolve ADDS
 * PUSH_FACTOR to movex/movez mid-pass, so an engine port must add a margin for
 * the largest PUSH_FACTOR in play (or size the cell from position + extent
 * only and let the in-loop 256 px test do the rest).
 *
 * Run pinned to core 0 (the memory-fast render core): taskset 0x01 ./collision_bench
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/collision_bench.c -o collision_bench -lrt
 * Local     : gcc -O2 -o collision_bench tools/collision_bench.c
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

#define MAX_ENTS 400          /* largest N tested */
#define MAX_COLLISONS 1       /* typical instance count per animpos (engine: 1) */
#define SHIPPED_CULL 256      /* the Step-14 rect cull, in level px */

/* --- minimal synthetic hitbox (the fields check_entity_collision touches) --- */
typedef struct {
    int x, y;                 /* box origin offsets */
    int width, height;        /* box far-corner offsets */
    int z_background, z_foreground;
} s_hitbox;

/* --- minimal synthetic entity (only fields the collision loop reads) --- */
typedef struct {
    int   exists;
    int   has_collision;      /* stands in for animation->collision_entity != NULL */
    float pos_x, pos_y, pos_z;
    int   movex, movez;
    int   direction;          /* 0 = right, 1 = left (DIRECTION_LEFT) */
    s_hitbox hb[MAX_COLLISONS];
    /* outputs the engine writes back: */
    void *collided_entity;
} entity;

#define DIRECTION_LEFT 1

static entity  ent_store[MAX_ENTS];
static entity *ent_list[MAX_ENTS];
static int     ent_max;

static inline int idiff(int a, int b){ int d = a - b; return d < 0 ? -d : d; }
static inline int iabs_(int a){ return a < 0 ? -a : a; }

/* faithful replica of check_entity_collision (openbor.c:35291).
 * returns 1 on overlap, 0 otherwise. side-effects on movex/movez mirror the
 * non-pushing branch (the common case) and, as in the engine, are applied ONLY
 * when a collision is found. */
static int check_entity_collision(entity *ent, entity *target)
{
    s_hitbox *coords_col_entity_ent;
    s_hitbox *coords_col_entity_target;
    int x1, x2, y1, y2, z1, z2;
    int i_ent, i_tgt;
    int col_entity_ent_pos_x = 0, col_entity_ent_pos_y = 0,
        col_entity_ent_size_x = 0, col_entity_ent_size_y = 0,
        col_entity_target_pos_x = 0, col_entity_target_pos_y = 0,
        col_entity_target_size_x = 0, col_entity_target_size_y = 0;
    int zdist = 0;
    int zdepth1 = 0, zdepth2 = 0;
    int collision_found = 0;

    if(ent == target || !target->has_collision || !ent->has_collision)
        return 0;

    for(i_ent = 0; i_ent < MAX_COLLISONS; i_ent++)
    {
        coords_col_entity_ent = &ent->hb[i_ent];

        for(i_tgt = 0; i_tgt < MAX_COLLISONS; i_tgt++)
        {
            coords_col_entity_target = &target->hb[i_tgt];

            z1 = (int)ent->pos_z + ent->movez;
            z2 = (int)target->pos_z + target->movez;
            zdist = 0;

            if(coords_col_entity_ent->z_foreground > coords_col_entity_ent->z_background)
            {
                zdepth1 = (coords_col_entity_ent->z_foreground - coords_col_entity_ent->z_background) / 2;
                z1 += coords_col_entity_ent->z_background + zdepth1;
                zdist += zdepth1;
            }
            else if(coords_col_entity_ent->z_background)
            {
                zdepth1 = coords_col_entity_ent->z_background;
                zdist += coords_col_entity_ent->z_background;
            }

            if(coords_col_entity_target->z_foreground > coords_col_entity_target->z_background)
            {
                zdepth2 = (coords_col_entity_target->z_foreground - coords_col_entity_target->z_background) / 2;
                z2 += coords_col_entity_target->z_background + zdepth2;
                zdist += zdepth2;
            }
            else if(coords_col_entity_target->z_background)
            {
                zdepth2 = coords_col_entity_target->z_background;
                zdist += coords_col_entity_target->z_background;
            }

            if(idiff(z1, z2) > zdist)
                continue;

            x1 = (int)ent->pos_x + ent->movex;
            z1 = (int)ent->pos_z + ent->movez;
            y1 = (int)z1 - (int)ent->pos_y;
            x2 = (int)target->pos_x + target->movex;
            z2 = (int)target->pos_z + target->movez;
            y2 = (int)z2 - (int)target->pos_y;

            if(ent->direction == DIRECTION_LEFT)
            {
                col_entity_ent_pos_x  = x1 - coords_col_entity_ent->width;
                col_entity_ent_size_x = x1 - coords_col_entity_ent->x;
            }
            else
            {
                col_entity_ent_pos_x  = x1 + coords_col_entity_ent->x;
                col_entity_ent_size_x = x1 + coords_col_entity_ent->width;
            }
            col_entity_ent_pos_y  = y1 + coords_col_entity_ent->y;
            col_entity_ent_size_y = y1 + coords_col_entity_ent->height;

            if(target->direction == DIRECTION_LEFT)
            {
                col_entity_target_pos_x  = x2 - coords_col_entity_target->width;
                col_entity_target_size_x = x2 - coords_col_entity_target->x;
            }
            else
            {
                col_entity_target_pos_x  = x2 + coords_col_entity_target->x;
                col_entity_target_size_x = x2 + coords_col_entity_target->width;
            }
            col_entity_target_pos_y  = y2 + coords_col_entity_target->y;
            col_entity_target_size_y = y2 + coords_col_entity_target->height;

            /* 4 AABB overlap rejection tests (openbor.c:35411-35426) */
            if(col_entity_ent_pos_x > col_entity_target_size_x)    continue;
            if(col_entity_target_pos_x > col_entity_ent_size_x)    continue;
            if(col_entity_ent_pos_y > col_entity_target_size_y)    continue;
            if(col_entity_target_pos_y > col_entity_ent_size_y)    continue;

            collision_found = 1;
            break;
        }
        if(collision_found)
            break;
    }

    if(!collision_found)
        return 0;

    /* non-pushing axis-resolve (the common path) -- mirrors openbor.c:35448+ */
    if(col_entity_ent_pos_x <= col_entity_target_pos_x){ if(ent->movex > 0) ent->movex = 0; }
    else                                               { if(ent->movex < 0) ent->movex = 0; }

    if(z1 - zdepth1 <= z2 + zdepth2 && z1 - zdepth1 >= z2){ if(ent->movez < 0) ent->movez = 0; }
    else if(z1 + zdepth1 >= z2 - zdepth2 && z1 + zdepth1 <= z2){ if(ent->movez > 0) ent->movez = 0; }

    return 1;
}

/* ==========================================================================
 * variant 1: SHIPPED -- check_entity_collision_for exactly as it ships
 * (openbor.c:35523 + Step-14 B+E cull)
 * ========================================================================== */
static void cec_for_shipped(entity *ent)
{
    if(ent && ent->has_collision)
    {
        int i;
        int ent_x = (int)ent->pos_x;
        int ent_z = (int)ent->pos_z;
        for(i = 0; i < ent_max; i++)
        {
            entity *target = ent_list[i];
            if(target->exists && target != ent && target->has_collision)   /* B */
            {
                int dx = (int)target->pos_x - ent_x;                       /* E */
                int dz = (int)target->pos_z - ent_z;
                if(dx > SHIPPED_CULL || dx < -SHIPPED_CULL ||
                   dz > SHIPPED_CULL || dz < -SHIPPED_CULL) continue;
                if(check_entity_collision(ent, target))
                {
                    ent->collided_entity = target;
                    target->collided_entity = ent;
                    return;
                }
            }
        }
    }
    ent->collided_entity = NULL;
}

/* ==========================================================================
 * per-entity conservative extents (one O(N) pass; in the engine these come
 * from ent->animation->collision_entity[animpos] and would live in a scratch
 * array indexed the same way as ent_list)
 * ========================================================================== */
static int   ext_x_of[MAX_ENTS];   /* max(|x|,|width|)                       */
static int   ext_z_of[MAX_ENTS];   /* 2*max(|z_background|,|z_foreground|)   */
static int   amvx_of[MAX_ENTS];    /* |movex| at pass start                  */
static int   amvz_of[MAX_ENTS];    /* |movez| at pass start                  */

static void compute_extents(void)
{
    int i, c;
    for(i = 0; i < ent_max; i++)
    {
        entity *e = ent_list[i];
        int ex = 0, ez = 0;
        for(c = 0; c < MAX_COLLISONS; c++)
        {
            s_hitbox *hb = &e->hb[c];
            int ax = iabs_(hb->x), aw = iabs_(hb->width);
            int zb = iabs_(hb->z_background), zf = iabs_(hb->z_foreground);
            if(aw > ax) ax = aw;
            if(zf > zb) zb = zf;
            if(ax > ex) ex = ax;
            if(zb > ez) ez = zb;
        }
        ext_x_of[i] = ex;
        ext_z_of[i] = 2 * ez;
        amvx_of[i]  = iabs_(e->movex);
        amvz_of[i]  = iabs_(e->movez);
    }
}

/* conservative pair test: 1 = the pair CAN still overlap (must be checked). */
static inline int pair_possible(int ie, int it, int dx, int dz)
{
    int rx = ext_x_of[ie] + ext_x_of[it] + amvx_of[ie] + amvx_of[it];
    int rz = ext_z_of[ie] + ext_z_of[it] + amvz_of[ie] + amvz_of[it];
    if(rx > SHIPPED_CULL) rx = SHIPPED_CULL;   /* never wider than what ships */
    if(rz > SHIPPED_CULL) rz = SHIPPED_CULL;
    if(dx > rx || dx < -rx) return 0;
    if(dz > rz || dz < -rz) return 0;
    return 1;
}

/* ==========================================================================
 * variant 2: TIGHT -- same index-order linear scan, per-pair extent cull
 * ========================================================================== */
static void cec_for_tight(entity *ent, int ie)
{
    if(ent && ent->has_collision)
    {
        int i;
        int ent_x = (int)ent->pos_x;
        int ent_z = (int)ent->pos_z;
        for(i = 0; i < ent_max; i++)
        {
            entity *target = ent_list[i];
            if(target->exists && target != ent && target->has_collision)   /* B */
            {
                int dx = (int)target->pos_x - ent_x;
                int dz = (int)target->pos_z - ent_z;
                if(!pair_possible(ie, i, dx, dz)) continue;                /* E' */
                if(check_entity_collision(ent, target))
                {
                    ent->collided_entity = target;
                    target->collided_entity = ent;
                    return;
                }
            }
        }
    }
    ent->collided_entity = NULL;
}

/* ==========================================================================
 * variant 3: GRID -- 1-D uniform bucket grid over x, rebuilt every pass
 * ==========================================================================
 * WHY 1-D, AND WHY THE PER-CANDIDATE BODY IS THE SHIPPED ONE:
 *   The measured cost of this bucket is dominated by the O(N^2) *visit* loop,
 *   not by check_entity_collision (which early-outs at its own z-cull for
 *   nearly every pair). So the only thing worth optimizing is "visit fewer
 *   entities", and every added instruction per candidate eats the win. A first
 *   attempt used a 2-D (x,z) grid with a 9-run merge and a per-pair extent
 *   cull; it was SLOWER than shipped, because 9 merge comparisons + extent
 *   arithmetic per candidate cost more than the ~2 ns tight loop they replaced.
 *   OpenBOR levels scroll in x and are only ~one screen deep in z, so the z
 *   axis has very little to bucket: dropping it takes the merge from 9 runs to
 *   3 and leaves the z rejection to the shipped 256 px test, which is already
 *   two compares inside the loop.
 *
 * Cell size = 2 * max(ext_x + |movex|) over the live entities, so the pair
 * bound (ext_e + ext_t + |mv_e| + |mv_t|) is at most one cell and the 3-cell
 * window [cx-1, cx+1] provably contains every entity that could collide.
 * Capped at 2*256 so the window is never wider than what the shipped cull
 * already admits.
 *
 * Per-cell runs are ascending in ent_list index (counting-sort scatter over
 * i ascending); the 3 runs are merged so candidates are emitted in ascending
 * index order -- that is what makes the early-return pick the exact same
 * target the shipped linear scan picks.
 */
#define MAX_CELLS 8192

static int   g_nx;
static int   g_cell_x;              /* cell size in level px (>=1)            */
static int   g_min_x;               /* grid origin                            */
static int   g_bypass;              /* 1 = window would be the whole level    */
static int   g_start[MAX_CELLS + 1];
static int   g_order[MAX_ENTS];
static int   g_cell_of[MAX_ENTS];   /* -1 = not in grid (B-culled)            */

#define IDX_END 0x7fffffff

static void grid_build(void)
{
    int i, c, ncells;
    int min_x = 1 << 30, max_x = -(1 << 30);
    int rx = 1;

    /* pass 1: x bounds + worst-case interaction radius (O(N), no per-entity
     * arrays needed -- only the max matters) */
    for(i = 0; i < ent_max; i++)
    {
        entity *e = ent_list[i];
        int x, r, cidx, ex = 0;
        if(!e->exists || !e->has_collision) continue;
        x = (int)e->pos_x;
        if(x < min_x) min_x = x;
        if(x > max_x) max_x = x;
        for(cidx = 0; cidx < MAX_COLLISONS; cidx++)
        {
            int ax = iabs_(e->hb[cidx].x), aw = iabs_(e->hb[cidx].width);
            if(aw > ax) ax = aw;
            if(ax > ex) ex = ax;
        }
        r = ex + iabs_(e->movex);
        if(r > rx) rx = r;
    }
    if(min_x > max_x) { min_x = max_x = 0; }

    g_cell_x = 2 * rx;
    if(g_cell_x > 2 * SHIPPED_CULL) g_cell_x = 2 * SHIPPED_CULL;
    if(g_cell_x < 1) g_cell_x = 1;

    g_min_x = min_x;
    for(;;)
    {
        g_nx = (max_x - min_x) / g_cell_x + 1;
        if(g_nx <= MAX_CELLS) break;
        g_cell_x *= 2;                     /* coarsen until it fits (safe: a  */
    }                                      /* coarser grid only widens the    */
    ncells = g_nx;                         /* candidate set, never narrows it)*/

    /* Degenerate level (fits in <= 3 cells): the 3-cell window IS every
     * entity, so bucketing can only add cost. Fall back to the shipped scan --
     * this is what makes the grid never-worse-than-shipped on single-screen
     * levels, at the price of one compare per pass. */
    g_bypass = (g_nx <= 3);
    if(g_bypass) return;

    for(c = 0; c <= ncells; c++) g_start[c] = 0;

    /* pass 2: histogram */
    for(i = 0; i < ent_max; i++)
    {
        entity *e = ent_list[i];
        if(!e->exists || !e->has_collision) { g_cell_of[i] = -1; continue; }
        g_cell_of[i] = ((int)e->pos_x - g_min_x) / g_cell_x;
        g_start[g_cell_of[i] + 1]++;
    }

    /* pass 3: prefix sum */
    for(c = 0; c < ncells; c++) g_start[c + 1] += g_start[c];

    /* pass 4: scatter, i ascending -> each cell's run is ascending in index */
    {
        static int cursor[MAX_CELLS];
        for(c = 0; c < ncells; c++) cursor[c] = g_start[c];
        for(i = 0; i < ent_max; i++)
        {
            int cell = g_cell_of[i];
            if(cell < 0) continue;
            g_order[cursor[cell]++] = i;
        }
    }
}

/* ie = ent's ent_list index; use_tight = apply the per-pair extent bound
 * instead of the shipped 256 px test on each candidate. */
static void cec_for_grid_impl(entity *ent, int ie, int use_tight)
{
    if(ent && ent->has_collision)
    {
        int ent_x = (int)ent->pos_x;
        int ent_z = (int)ent->pos_z;
        int cx  = (ent_x - g_min_x) / g_cell_x;
        int cx0 = cx - 1, cx1 = cx + 1;
        /* three ascending runs, heads kept in registers: reloading all three
         * from g_order[] on every emitted candidate was costing ~4x the
         * shipped loop it replaces, which is what ate the first version's win.
         * Only the run that advanced is refreshed. */
        int h0, e0, v0, h1, e1, v1, h2, e2, v2;

        if(cx0 < 0) cx0 = 0;
        if(cx1 >= g_nx) cx1 = g_nx - 1;

        h0 = g_start[cx0];  e0 = g_start[cx0 + 1];
        v0 = (h0 < e0) ? g_order[h0] : IDX_END;
        if(cx0 + 1 <= cx1) { h1 = g_start[cx0 + 1]; e1 = g_start[cx0 + 2];
                             v1 = (h1 < e1) ? g_order[h1] : IDX_END; }
        else               { h1 = e1 = 0; v1 = IDX_END; }
        if(cx0 + 2 <= cx1) { h2 = g_start[cx0 + 2]; e2 = g_start[cx0 + 3];
                             v2 = (h2 < e2) ? g_order[h2] : IDX_END; }
        else               { h2 = e2 = 0; v2 = IDX_END; }

        /* 3-way merge: emit candidates in ascending ent_list index order,
         * then run the SHIPPED per-candidate body verbatim. */
        for(;;)
        {
            int best;
            entity *target;
            int dx, dz;

            if(v0 <= v1)
            {
                if(v0 <= v2) { best = v0; h0++; v0 = (h0 < e0) ? g_order[h0] : IDX_END; }
                else         { best = v2; h2++; v2 = (h2 < e2) ? g_order[h2] : IDX_END; }
            }
            else
            {
                if(v1 <= v2) { best = v1; h1++; v1 = (h1 < e1) ? g_order[h1] : IDX_END; }
                else         { best = v2; h2++; v2 = (h2 < e2) ? g_order[h2] : IDX_END; }
            }
            if(best == IDX_END) break;

            target = ent_list[best];
            if(target == ent) continue;             /* B: exists + has_collision
                                                       already hold by build   */
            dx = (int)target->pos_x - ent_x;
            dz = (int)target->pos_z - ent_z;
            if(use_tight)
            {
                if(!pair_possible(ie, best, dx, dz)) continue;   /* E' */
            }
            else
            {
                if(dx > SHIPPED_CULL || dx < -SHIPPED_CULL ||    /* E  */
                   dz > SHIPPED_CULL || dz < -SHIPPED_CULL) continue;
            }
            if(check_entity_collision(ent, target))
            {
                ent->collided_entity = target;
                target->collided_entity = ent;
                return;
            }
        }
    }
    ent->collided_entity = NULL;
}

static void cec_for_grid(entity *ent)            { cec_for_grid_impl(ent, 0, 0); }
static void cec_for_gridt(entity *ent, int ie)   { cec_for_grid_impl(ent, ie, 1); }

/* ==========================================================================
 * one full arrange pass per variant (this is what gets timed)
 * ========================================================================== */
typedef enum { V_SHIPPED = 0, V_TIGHT, V_GRID, V_GRIDT, V_COUNT } variant_t;
static const char *v_name[V_COUNT] = { "shipped", "tight", "grid", "grid+tight" };

static void arrange_pass(variant_t v)
{
    int i;
    if(v == V_TIGHT || v == V_GRIDT) compute_extents();
    if(v == V_GRID  || v == V_GRIDT) grid_build();

    for(i = 0; i < ent_max; i++)
    {
        entity *ent = ent_list[i];
        if(!ent->exists) continue;
        switch(v)
        {
            case V_SHIPPED: cec_for_shipped(ent);     break;
            case V_TIGHT:   cec_for_tight(ent, i);    break;
            case V_GRID:    if(g_bypass) cec_for_shipped(ent);
                            else         cec_for_grid(ent);
                            break;
            default:        if(g_bypass) cec_for_tight(ent, i);
                            else         cec_for_gridt(ent, i);
                            break;
        }
    }
}

/* untimed: static candidate counts (early-return NOT modelled -- these say how
 * much work each traversal is asked to look at, not how much it ends up doing).
 * candS = what the shipped linear scan walks; candG = what the 3-cell window
 * walks. */
static void count_candidates(long *candS, long *candG)
{
    int i, j, c;
    long ns = 0, ng = 0;
    for(i = 0; i < ent_max; i++)
    {
        entity *ent = ent_list[i];
        if(!ent->exists || !ent->has_collision) continue;
        for(j = 0; j < ent_max; j++)
        {
            entity *t = ent_list[j];
            if(t->exists && t != ent && t->has_collision) ns++;
        }
        if(g_bypass) continue;              /* window == everything; ng set below */
        {
            int cx  = ((int)ent->pos_x - g_min_x) / g_cell_x;
            int cx0 = cx - 1, cx1 = cx + 1;
            if(cx0 < 0) cx0 = 0;
            if(cx1 >= g_nx) cx1 = g_nx - 1;
            for(c = cx0; c <= cx1; c++) ng += g_start[c + 1] - g_start[c];
            ng--;                                   /* the entity itself */
        }
    }
    if(g_bypass) ng = ns;                   /* bypass walks the shipped set */
    *candS = ns; *candG = ng;
}

/* untimed: how many pairs survive the shipped B+E cull (for ns/pair) */
static long count_shipped_pairs(void)
{
    int i, j;
    long pairs = 0;
    for(i = 0; i < ent_max; i++)
    {
        entity *ent = ent_list[i];
        int ent_x, ent_z;
        if(!ent->exists || !ent->has_collision) continue;
        ent_x = (int)ent->pos_x; ent_z = (int)ent->pos_z;
        for(j = 0; j < ent_max; j++)
        {
            entity *t = ent_list[j];
            int dx, dz;
            if(!(t->exists && t != ent && t->has_collision)) continue;
            dx = (int)t->pos_x - ent_x; dz = (int)t->pos_z - ent_z;
            if(dx > SHIPPED_CULL || dx < -SHIPPED_CULL ||
               dz > SHIPPED_CULL || dz < -SHIPPED_CULL) continue;
            pairs++;
        }
    }
    return pairs;
}

/* untimed: how many pairs survive the TIGHT bound (shows the cull's reach) */
static long count_tight_pairs(void)
{
    int i, j;
    long pairs = 0;
    compute_extents();
    for(i = 0; i < ent_max; i++)
    {
        entity *ent = ent_list[i];
        int ent_x, ent_z;
        if(!ent->exists || !ent->has_collision) continue;
        ent_x = (int)ent->pos_x; ent_z = (int)ent->pos_z;
        for(j = 0; j < ent_max; j++)
        {
            entity *t = ent_list[j];
            if(!(t->exists && t != ent && t->has_collision)) continue;
            if(!pair_possible(i, j, (int)t->pos_x - ent_x, (int)t->pos_z - ent_z)) continue;
            pairs++;
        }
    }
    return pairs;
}

/* ==========================================================================
 * deterministic synthetic scene + result signature (equivalence proof)
 * ========================================================================== */
static void build_scene(int n, int span_x, int span_z)
{
    int i, c;
    ent_max = n;
    for(i = 0; i < n; i++)
    {
        entity *e = &ent_store[i];
        memset(e, 0, sizeof(*e));
        e->exists = 1;
        e->has_collision = ((i % 10) != 7);                 /* ~10% non-collidable */
        /* spread positions; primes keep it well-mixed yet fully deterministic */
        e->pos_x = (float)((i * 53) % span_x);
        e->pos_z = (float)((i * 29) % span_z);
        e->pos_y = (float)((i * 17) % 48);
        e->movex = ((i * 7) % 5) - 2;                       /* -2..2 */
        e->movez = ((i * 11) % 5) - 2;                      /* -2..2 */
        e->direction = (i & 1) ? DIRECTION_LEFT : 0;
        for(c = 0; c < MAX_COLLISONS; c++)
        {
            s_hitbox *hb = &e->hb[c];
            hb->x = 4;
            hb->y = 4;
            hb->width  = 24 + (i % 8);                      /* varied extents */
            hb->height = 40 + (i % 12);
            hb->z_background = 0;
            hb->z_foreground = 8 + (i % 6);
        }
        ent_list[i] = e;
    }
}

typedef struct { int collided; int movex, movez; } sig_t;
static sig_t sig_a[MAX_ENTS], sig_b[MAX_ENTS];

static void capture_sig(sig_t *s)
{
    int i, j;
    for(i = 0; i < ent_max; i++)
    {
        entity *e = ent_list[i];
        s[i].collided = -1;
        for(j = 0; j < ent_max; j++)
            if((void *)ent_list[j] == e->collided_entity) { s[i].collided = j; break; }
        s[i].movex = e->movex;
        s[i].movez = e->movez;
    }
}

/* returns index of first mismatch, or -1 if identical */
static int sig_cmp(const sig_t *a, const sig_t *b, int n)
{
    int i;
    for(i = 0; i < n; i++)
        if(a[i].collided != b[i].collided || a[i].movex != b[i].movex || a[i].movez != b[i].movez)
            return i;
    return -1;
}

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (double)t.tv_sec * 1e9 + (double)t.tv_nsec; }

/* ========================================================================== */

typedef struct { const char *name; int span_x, span_z; } scene_t;

int main(void)
{
    scene_t scenes[] = {
        { "screen", 320,  224 },   /* single screen -- the original baseline  */
        { "wide",  1920,  224 },   /* scrolling level -- realistic heavy PAK  */
        { "dense",  160,  112 }    /* worst case: one neighbourhood holds all */
    };
    int Ns[] = { 50, 100, 200, 400 };
    int nScenes = (int)(sizeof(scenes) / sizeof(scenes[0]));
    int nN      = (int)(sizeof(Ns) / sizeof(Ns[0]));
    int RUNS = 11;         /* min-of-N for clean timing */
    int s, k, r, v;
    int failures = 0;

    printf("== collision_bench (A9) -- arrange fps bucket: check_entity_collision_for ==\n");
    printf("(run pinned to core 0: taskset 0x01 ./collision_bench ; min of %d runs)\n", RUNS);
    printf("variants: shipped = Step-14 B+E 256px cull | tight = per-pair extent cull\n");
    printf("          grid = 1-D x bucket grid rebuilt every pass, 3-cell window, ascending\n");
    printf("          candidate order (same winner as the linear scan) | grid+tight = both\n");
    printf("NOTE: pair counting is now UNTIMED (it used to run inside the timed region),\n");
    printf("      so 'shipped' reads lower than the previously published 8.85 ms @ N=400.\n\n");

    for(s = 0; s < nScenes; s++)
    {
        printf("-- scene: %s (%d x %d px) --\n", scenes[s].name, scenes[s].span_x, scenes[s].span_z);
        printf("%-5s %8s %8s %8s %8s  %8s %8s %8s %8s  %6s %6s %6s %6s\n",
               "N", "candS", "candG", "chkS", "chkT",
               "ship_ms", "tght_ms", "grid_ms", "grdT_ms",
               "x_t", "x_g", "x_gt", "equiv");

        for(k = 0; k < nN; k++)
        {
            int n = Ns[k];
            long pairsE, pairsT, candS, candG;
            double best[V_COUNT];

            build_scene(n, scenes[s].span_x, scenes[s].span_z);
            pairsE = count_shipped_pairs();
            build_scene(n, scenes[s].span_x, scenes[s].span_z);
            pairsT = count_tight_pairs();
            build_scene(n, scenes[s].span_x, scenes[s].span_z);
            grid_build();
            count_candidates(&candS, &candG);

            /* --- equivalence: every variant must reproduce 'shipped' exactly --- */
            build_scene(n, scenes[s].span_x, scenes[s].span_z);
            arrange_pass(V_SHIPPED);
            capture_sig(sig_a);

            {
                int equiv_ok = 1;
                for(v = V_TIGHT; v < V_COUNT; v++)
                {
                    int bad;
                    build_scene(n, scenes[s].span_x, scenes[s].span_z);
                    arrange_pass((variant_t)v);
                    capture_sig(sig_b);
                    bad = sig_cmp(sig_a, sig_b, n);
                    if(bad >= 0)
                    {
                        equiv_ok = 0; failures++;
                        printf("   !! MISMATCH scene=%s N=%d variant=%s ent=%d "
                               "(shipped collided=%d mx=%d mz=%d | %s collided=%d mx=%d mz=%d)\n",
                               scenes[s].name, n, v_name[v], bad,
                               sig_a[bad].collided, sig_a[bad].movex, sig_a[bad].movez,
                               v_name[v],
                               sig_b[bad].collided, sig_b[bad].movex, sig_b[bad].movez);
                    }
                }

                /* --- timing --- */
                for(v = 0; v < V_COUNT; v++)
                {
                    best[v] = 1e30;
                    for(r = 0; r < RUNS; r++)
                    {
                        double t0, dt;
                        build_scene(n, scenes[s].span_x, scenes[s].span_z);
                        t0 = now_ns();
                        arrange_pass((variant_t)v);
                        dt = now_ns() - t0;
                        if(dt < best[v]) best[v] = dt;
                    }
                }

                printf("%-5d %8ld %8ld %8ld %8ld  %8.4f %8.4f %8.4f %8.4f  %5.2fx %5.2fx %5.2fx %6s\n",
                       n, candS, candG, pairsE, pairsT,
                       best[V_SHIPPED] / 1e6, best[V_TIGHT] / 1e6,
                       best[V_GRID] / 1e6,    best[V_GRIDT] / 1e6,
                       best[V_TIGHT] > 0 ? best[V_SHIPPED] / best[V_TIGHT] : 0.0,
                       best[V_GRID]  > 0 ? best[V_SHIPPED] / best[V_GRID]  : 0.0,
                       best[V_GRIDT] > 0 ? best[V_SHIPPED] / best[V_GRIDT] : 0.0,
                       equiv_ok ? "PASS" : "FAIL");
            }
        }
        printf("\n");
    }

    printf("candS/candG = entities the shipped scan vs the 3-cell window walk (static, no\n");
    printf("  early-return): the grid's lever. chkS/chkT = pairs reaching check_entity_collision\n");
    printf("  under the 256px vs the per-pair extent cull: the tight cull's lever.\n");
    printf("equiv = every entity's collided target + final movex/movez identical to shipped.\n");

    if(failures)
    {
        printf("\nRESULT: FAIL -- %d variant/scene combination(s) diverged from shipped.\n", failures);
        return 1;
    }
    printf("\nRESULT: PASS -- tight and grid are behaviourally identical to shipped.\n");
    return 0;
}
