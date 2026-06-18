/*
 * entity_bench.c -- OpenBOR_7533 per-frame entity-tick cost-shape bench (A9).
 *
 * *** COST-SHAPE MODEL, not a verbatim port. *** The per-frame entity tick
 * (openbor.c update_ents(), ~line 29818) walks ent_list[0..ent_max] and for each
 * live entity runs five sub-buckets the in-engine [SUB] profiler measures:
 *   script  -> execute_updateentity_script(self)
 *   ai      -> check_ai()
 *   anim    -> update_animation()
 *   coll    -> check_attack()        (collision detection)
 *   arrange -> ent_post_update() / arrange_ents()
 * Each of those calls deep into the engine (ScriptVariant VM, animation tables,
 * attack-box lists, the ent_list[] sort) and CANNOT be lifted into a self-
 * contained bench. So this models the DOMINANT COST SHAPE of each bucket with
 * representative per-entity work (struct field reads/writes + arithmetic + a
 * small bounded inner loop for coll/arrange). The point is the SHAPE: how the
 * per-frame tick cost scales with entity count N (50/100/200/400), and which
 * sub-bucket dominates -- NOT the literal engine kernel.
 *
 * Cross-check the ABSOLUTE numbers against the in-engine [SUB] script/ai/anim/
 * coll/arr buckets; this is the PAK-free confirmation tool for that breakdown.
 *
 * Run pinned to the memory-fast render core:  taskset 0x01 ./entity_bench [N...]
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/entity_bench.c -o entity_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

/* ---- a trimmed entity stand-in: only the fields the cost-shape touches ---- */
/* Mirrors the SHAPE of the engine's entity struct (pos/velocity, anim cursor,
 * attack boxes), padded so the per-entity stride resembles the real ~KB entity
 * and the cache behaviour of walking ent_list[] is representative. */
typedef struct {
    /* hot fields used by the sub-bucket models */
    float x, y, z;            /* position */
    float vx, vy, vz;         /* velocity */
    int   exists;
    int   animnum, animpos, animframes;   /* animation cursor */
    int   health, oldhealth;
    int   nboxes;             /* live attack/collision boxes this frame */
    int   boxx[8], boxy[8], boxw[8], boxh[8]; /* attack-box list (bounded loop) */
    int   ai_target;          /* index of ai target entity */
    /* pad to a realistic entity footprint (engine entity is ~KB) */
    unsigned char pad[768];
} ent_t;

/* deterministic fill -- no random, so every run/variant is byte-identical */
static void seed_ents(ent_t *e, int N){
    int i, k;
    memset(e, 0, (size_t)N * sizeof(ent_t));
    for(i=0;i<N;i++){
        e[i].exists      = 1;
        e[i].x           = (float)((i*97)  % 960);
        e[i].y           = (float)((i*61)  % 480);
        e[i].z           = (float)((i*29)  % 240);
        e[i].vx          = (float)(((i*13)%7) - 3);
        e[i].vy          = 0.0f;
        e[i].vz          = (float)(((i*17)%5) - 2);
        e[i].animnum     = (i*7)  & 63;
        e[i].animpos     = (i*3)  % 12;
        e[i].animframes  = 8 + ((i*5) % 16);
        e[i].health      = 100;
        e[i].oldhealth   = 100;
        e[i].nboxes      = 1 + ((i*11) % 4);   /* 1..4 boxes/entity */
        for(k=0;k<8;k++){ e[i].boxx[k]=(i+k)*3; e[i].boxy[k]=(i+k)*5; e[i].boxw[k]=8+k; e[i].boxh[k]=12+k; }
        e[i].ai_target   = (i*53) % N;
    }
}

/* sink so -O2 cannot delete the work */
static volatile long g_sink;

/* === cost-shape model of [SUB].script ===
 * NOT execute_updateentity_script(). The script VM cost shape is "touch a handful
 * of entity fields + branch + small arithmetic per scripted entity". */
static long sub_script(ent_t *e, int N){
    int i; long acc=0;
    for(i=0;i<N;i++){
        if(!e[i].exists) continue;
        /* a few field reads + a branch + arithmetic, like a tiny script body */
        int h = e[i].health;
        if(h > 0){
            e[i].animpos = (e[i].animpos + 1) % e[i].animframes;
            acc += (long)e[i].animpos + e[i].animnum;
        }
        if(e[i].z > 200.0f) e[i].vz = -e[i].vz;   /* a conditional like an AI/script flip */
        acc ^= (long)e[i].x;
    }
    return acc;
}

/* === cost-shape model of [SUB].ai ===
 * NOT check_ai(). Shape: read the target entity, compute a heading toward it,
 * update velocity. One indirect entity read per actor (the cache cost that makes
 * ai scale worse than a flat loop). */
static long sub_ai(ent_t *e, int N){
    int i; long acc=0;
    for(i=0;i<N;i++){
        if(!e[i].exists) continue;
        ent_t *t = &e[e[i].ai_target];   /* indirect read of another entity */
        float dx = t->x - e[i].x;
        float dz = t->z - e[i].z;
        e[i].vx = (dx > 0.0f) ? 1.5f : (dx < 0.0f ? -1.5f : 0.0f);
        e[i].vz = (dz > 0.0f) ? 1.0f : (dz < 0.0f ? -1.0f : 0.0f);
        acc += (long)dx + (long)dz;
    }
    return acc;
}

/* === cost-shape model of [SUB].anim ===
 * NOT update_animation(). Shape: advance the animation cursor, wrap at frame
 * count, occasionally restart -- pure per-entity arithmetic, cheap. */
static long sub_anim(ent_t *e, int N){
    int i; long acc=0;
    for(i=0;i<N;i++){
        if(!e[i].exists) continue;
        int p = e[i].animpos + 1;
        if(p >= e[i].animframes){ p = 0; e[i].animnum = (e[i].animnum + 1) & 63; }
        e[i].animpos = p;
        acc += p;
    }
    return acc;
}

/* === cost-shape model of [SUB].coll ===
 * NOT check_attack(). Shape: for each attacker box, scan nearby entities' boxes
 * for an AABB overlap. This is the bounded inner loop that makes collision the
 * super-linear sub-bucket (boxes x candidate-entities). Bounded candidate window
 * keeps it O(N * boxes * W), the real engine's near-neighbour shape. */
#define COLL_WINDOW 8
static long sub_coll(ent_t *e, int N){
    int i, b, j, c; long hits=0;
    for(i=0;i<N;i++){
        if(!e[i].exists) continue;
        for(b=0;b<e[i].nboxes;b++){
            int ax=e[i].boxx[b], ay=e[i].boxy[b], aw=e[i].boxw[b], ah=e[i].boxh[b];
            /* test against a bounded window of following entities (near-neighbours) */
            for(c=1;c<=COLL_WINDOW;c++){
                j = i + c; if(j>=N) break;
                if(!e[j].exists) continue;
                int vb = e[j].nboxes, k;
                for(k=0;k<vb;k++){
                    int bx=e[j].boxx[k], by=e[j].boxy[k], bw=e[j].boxw[k], bh=e[j].boxh[k];
                    if(ax < bx+bw && ax+aw > bx && ay < by+bh && ay+ah > by) hits++;  /* AABB overlap */
                }
            }
        }
    }
    return hits;
}

/* === cost-shape model of [SUB].arrange ===
 * NOT arrange_ents()/ent_post_update(). Shape: a partial bubble pass that pulls
 * live entities forward + sorts by z (the engine compacts ent_list[] and orders
 * by depth each frame). One O(N) compaction pass + a bounded neighbour swap. */
static long sub_arrange(ent_t *e, int N){
    int i; long acc=0;
    /* per-entity post update: apply velocity to position (like ent_post_update) */
    for(i=0;i<N;i++){
        if(!e[i].exists) continue;
        e[i].x += e[i].vx;
        e[i].y += e[i].vy;
        e[i].z += e[i].vz;
        acc += (long)e[i].x;
    }
    /* one bounded ordering pass over adjacent entities (depth sort shape) */
    for(i=0;i+1<N;i++){
        if(e[i].z > e[i+1].z){
            float tz=e[i].z; e[i].z=e[i+1].z; e[i+1].z=tz;   /* swap z only (cost shape) */
            acc++;
        }
    }
    return acc;
}

/* time one full frame (all 5 sub-buckets) over N entities, min-of-7 */
#define REP 7
static double time_bucket(long (*fn)(ent_t*,int), ent_t *e, int N){
    int r, f; double best=1e30;
    const int FRAMES=200;   /* many frames per timed rep for a stable read */
    for(r=0;r<REP;r++){
        seed_ents(e, N);
        double t0=now_ns();
        for(f=0;f<FRAMES;f++) g_sink += fn(e, N);
        double dt=now_ns()-t0;
        if(dt<best) best=dt;
    }
    return best/(double)FRAMES;   /* ns per frame for this bucket */
}

int main(int argc,char**argv){
    /* entity counts to sweep (default 50/100/200/400) */
    int counts[8]; int ncounts=0;
    if(argc>1){ int i; for(i=1;i<argc && ncounts<8;i++) counts[ncounts++]=atoi(argv[i]); }
    else { counts[0]=50; counts[1]=100; counts[2]=200; counts[3]=400; ncounts=4; }

    int maxN=0, i; for(i=0;i<ncounts;i++) if(counts[i]>maxN) maxN=counts[i];
    ent_t *e = (ent_t*)malloc((size_t)maxN * sizeof(ent_t));
    if(!e){ printf("alloc failed\n"); return 1; }

    printf("== entity_bench (OpenBOR_7533, A9) -- COST-SHAPE of per-frame [SUB] entity tick ==\n");
    printf("   (script/ai/anim/coll/arrange are cost-shape models, NOT the literal engine kernels;\n");
    printf("    cross-check absolute ns against the in-engine [SUB] buckets. entity stride=%ld bytes)\n\n",
           (long)sizeof(ent_t));
    printf("%-6s %9s %9s %9s %9s %9s %11s %11s\n",
           "N","script","ai","anim","coll","arrange","total/fr","ns/entity");
    printf("%-6s %9s %9s %9s %9s %9s %11s %11s\n",
           "------","(ns/fr)","(ns/fr)","(ns/fr)","(ns/fr)","(ns/fr)","(ns)","(ns)");

    for(i=0;i<ncounts;i++){
        int N=counts[i];
        double s = time_bucket(sub_script,  e, N);
        double a = time_bucket(sub_ai,      e, N);
        double n = time_bucket(sub_anim,    e, N);
        double c = time_bucket(sub_coll,    e, N);
        double r = time_bucket(sub_arrange, e, N);
        double tot = s+a+n+c+r;
        printf("%-6d %9.0f %9.0f %9.0f %9.0f %9.0f %11.0f %11.2f\n",
               N, s, a, n, c, r, tot, tot/(double)N);
    }
    printf("\nReading: 'total/fr' is the per-frame entity-tick cost-shape; at 59.92 Hz the frame\n");
    printf("budget is ~16.69 ms (16690000 ns). 'coll' should grow fastest with N (bounded near-\n");
    printf("neighbour AABB window) -- if it dominates, the real check_attack() likely does too.\n");
    free(e);
    return 0;
}
