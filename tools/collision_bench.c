/*
 * collision_bench.c -- OpenBOR_7533 entity-collision kernel micro-benchmark (A9).
 *
 * Measures the per-frame cost + scaling of the "arrange" fps bucket on the real
 * Cortex-A9, WITHOUT loading a PAK or playing. The bottleneck is the O(N^2)
 * pass arrange_ents() -> ent_post_update() -> check_entity_collision_for(): for
 * EACH existing entity it loops over ALL entities, so cost grows ~N^2. On heavy
 * PAKs this is ~153 ms/frame. Isolating it here makes the optimization push
 * (e.g. a spatial grid) a tight edit -> build -> ssh -> number loop.
 *
 * Kernel replicated faithfully (no engine headers -- inlined like blend_bench):
 *   check_entity_collision_for (openbor.c v7533 line 36165):
 *     for each entity ent (with a collision_entity animation):
 *       for i in 0..ent_max:
 *         target = ent_list[i]
 *         B cull : skip if !exists || target==ent || no collision_entity
 *         E cull : skip if |dx|>256 || |dz|>256   (cheap rect, line 36183-36185)
 *         on survivor: check_entity_collision(ent,target) (line 35933):
 *           max_collisons^2 nested loop (=1x1 typical), per instance:
 *             z-depth arithmetic + diff(z1,z2)>zdist z-cull
 *             direction-dependent box-corner computation
 *             4 AABB overlap rejection tests
 *           first overlap -> set collided pointers, return.
 *
 * Synthetic ent_list[] of N entities at DETERMINISTIC pseudo-random positions
 * on a ~320x224 play area (positions derived arithmetically from index -- no
 * Math.random). Each runs the full O(N^2) arrange pass. Reported per N so the
 * quadratic curve is visible: ns/pair should be ~flat; total ~quadruples as N
 * doubles.
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
#define PLAY_W 320
#define PLAY_H 224            /* used as the z (depth) range as well */

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

/* faithful replica of check_entity_collision (openbor.c:35933).
 * returns 1 on overlap, 0 otherwise. side-effects on movex/movez mirror the
 * non-pushing branch (the common case). */
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

            /* 4 AABB overlap rejection tests (openbor.c:36053-36068) */
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

    /* non-pushing axis-resolve (the common path) -- mirrors openbor.c:36090+ */
    if(col_entity_ent_pos_x <= col_entity_target_pos_x){ if(ent->movex > 0) ent->movex = 0; }
    else                                               { if(ent->movex < 0) ent->movex = 0; }

    if(z1 - zdepth1 <= z2 + zdepth2 && z1 - zdepth1 >= z2){ if(ent->movez < 0) ent->movez = 0; }
    else if(z1 + zdepth1 >= z2 - zdepth2 && z1 + zdepth1 <= z2){ if(ent->movez > 0) ent->movez = 0; }

    return 1;
}

/* faithful replica of check_entity_collision_for (openbor.c:36165), incl. the
 * B+E cull. */
static void check_entity_collision_for(entity *ent)
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
                if(dx > 256 || dx < -256 || dz > 256 || dz < -256) continue;
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

/* one full arrange pass: for each existing entity, run the collision-for loop.
 * returns the number of B+E survivor pairs that reached check_entity_collision
 * (the meaningful "pair" count for ns/pair). */
static long arrange_pass(void)
{
    int i, j;
    long pairs = 0;
    for(i = 0; i < ent_max; i++)
    {
        entity *ent = ent_list[i];
        if(!ent->exists) continue;
        /* count survivors for reporting (cheap; same culls as the kernel) */
        if(ent->has_collision)
        {
            int ent_x = (int)ent->pos_x, ent_z = (int)ent->pos_z;
            for(j = 0; j < ent_max; j++)
            {
                entity *t = ent_list[j];
                if(t->exists && t != ent && t->has_collision)
                {
                    int dx = (int)t->pos_x - ent_x, dz = (int)t->pos_z - ent_z;
                    if(dx > 256 || dx < -256 || dz > 256 || dz < -256) continue;
                    pairs++;
                }
            }
        }
        check_entity_collision_for(ent);
    }
    return pairs;
}

/* deterministic synthetic scene: positions arithmetically spread over the play
 * area; small varied hitboxes; ~10% non-collidable to exercise the B cull. */
static void build_scene(int n)
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
        e->pos_x = (float)((i * 53) % PLAY_W);
        e->pos_z = (float)((i * 29) % PLAY_H);
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

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return (double)t.tv_sec * 1e9 + (double)t.tv_nsec; }

int main(void)
{
    int Ns[] = { 50, 100, 200, 400 };
    int nN = (int)(sizeof(Ns) / sizeof(Ns[0]));
    int RUNS = 7;          /* min-of-N for clean timing */
    int k, r;

    printf("== collision_bench (A9) -- arrange fps bucket: check_entity_collision_for O(N^2) ==\n");
    printf("(run pinned to core 0: taskset 0x01 ./collision_bench ; min of %d runs)\n", RUNS);
    printf("%-6s %10s %12s %12s %12s %12s\n",
           "N", "pairs", "total_ms", "ns/entity", "ns/pair", "x_vs_prev");

    double prev_total = 0.0;
    for(k = 0; k < nN; k++)
    {
        int n = Ns[k];
        build_scene(n);

        /* warm caches + capture survivor-pair count (constant per scene) */
        long pairs = arrange_pass();

        double best = 1e30;
        for(r = 0; r < RUNS; r++)
        {
            build_scene(n);                 /* reset movex/movez each run */
            double t0 = now_ns();
            arrange_pass();
            double dt = now_ns() - t0;
            if(dt < best) best = dt;
        }

        double total_ms  = best / 1e6;
        double ns_entity = best / (double)n;
        double ns_pair   = pairs ? best / (double)pairs : 0.0;
        double x_prev    = prev_total > 0.0 ? total_ms / prev_total : 0.0;

        printf("%-6d %10ld %12.4f %12.2f %12.2f %12.2fx\n",
               n, pairs, total_ms, ns_entity, ns_pair, x_prev);
        prev_total = total_ms;
    }

    printf("\nNotes: ns/pair ~flat across N confirms per-pair cost is constant;\n");
    printf("       total_ms ~quadrupling as N doubles confirms the O(N^2) shape.\n");
    return 0;
}
