/*
 * pak_groupscan.c -- OpenBOR PAK static entity-ceiling scanner (A9 / any host).
 *
 * Answers "which PAKs can actually produce a high live-entity count?" WITHOUT
 * playing them, by reading the one value the engine actually enforces:
 *
 *     update_ents():  while (count_ents(TYPE_ENEMY) < groupmax && ...) spawn
 *
 * so a level section's `group <min> <max>` is a HARD ceiling on concurrent
 * enemies. A PAK whose sections all cap at 3-8 structurally cannot fill an
 * O(N^2) entity loop; a PAK that leaves sections at 999/9999 can.
 *
 * WHY A DEDICATED TOOL: PAKs here are 400 MB - 1.4 GB and there are 450 of
 * them, so downloading them is impossible and grepping them on-device is
 * forbidden (an unbounded on-device grep once starved sshd for 30+ minutes).
 * This reads ONLY the packfile directory (tail) plus the DATA/LEVELS TXT
 * byte ranges -- typically ~0.5-2 MB per PAK regardless of PAK size -- via
 * targeted seeks. Bounded by construction, one output line per PAK.
 *
 * Packfile format (upstream engine/source/gamelib/packfile.c):
 *   last 4 bytes  = directory offset
 *   directory     = repeating { u32 pns_len, u32 filestart, u32 filesize,
 *                               char name[pns_len-12] }
 *
 * Output (one line per PAK, tab-separated):
 *   <levels> <sections> <uncapped> <maxcap> <medcap> <spawns> <maxburst> <name>
 *     levels    = DATA/LEVELS TXT files found
 *     sections  = `group` directives seen
 *     uncapped  = sections with groupmax >= 100 (no practical enemy ceiling)
 *     maxcap    = largest groupmax seen
 *     medcap    = median groupmax (the typical ceiling)
 *     spawns    = total `spawn` entries
 *     maxburst  = most `at` triggers inside one 320 px window in any single
 *                 level (an UPPER BOUND only -- it ignores kills and counts
 *                 obstacle/item spawns too; measured JLL peak was 18 against a
 *                 predicted 100, so treat it as "can cluster", never as a count)
 *
 * Usage: pak_groupscan <paks_dir> [first_index] [count]
 *   The index window lets the caller run it in batches so no single invocation
 *   is long-lived.
 *
 * Build (armhf): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 \
 *                  -mfpu=neon -mfloat-abi=hard pak_groupscan.c -o pak_groupscan
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <dirent.h>
#include <stdint.h>

#define MAX_LEVELS   1024
#define MAX_SPAWNS   8192
#define MAX_SECTIONS 4096
#define MAX_LVL_SIZE (4u * 1024u * 1024u)

typedef struct { uint32_t start, size; } range_t;

static int cmp_int(const void *a, const void *b)
{
    int x = *(const int *)a, y = *(const int *)b;
    return (x > y) - (x < y);
}

/* case-insensitive substring */
static int has(const char *hay, const char *needle)
{
    size_t n = strlen(needle);
    for(; *hay; hay++)
        if(strncasecmp(hay, needle, n) == 0) return 1;
    return 0;
}

static int scan_pak(const char *path, const char *display)
{
    FILE *f = fopen(path, "rb");
    uint32_t dir_off = 0, pos;
    long fsize;
    range_t levels[MAX_LEVELS];
    int nlevels = 0;
    int caps[MAX_SECTIONS]; int ncaps = 0;
    int uncapped = 0, maxcap = 0, total_spawns = 0, maxburst = 0;
    char *buf = NULL;
    int i;

    if(!f) { printf("ERR\t-\t-\t-\t-\t-\t-\t%s\n", display); return 1; }

    if(fseek(f, 0, SEEK_END) != 0) { fclose(f); printf("ERR\t-\t-\t-\t-\t-\t-\t%s\n", display); return 1; }
    fsize = ftell(f);
    if(fsize < 8) { fclose(f); printf("ERR\t-\t-\t-\t-\t-\t-\t%s\n", display); return 1; }

    if(fseek(f, fsize - 4, SEEK_SET) != 0 ||
       fread(&dir_off, 4, 1, f) != 1 ||
       (long)dir_off > fsize - 4)
    { fclose(f); printf("BADDIR\t-\t-\t-\t-\t-\t-\t%s\n", display); return 1; }

    /* --- walk the directory, collecting DATA/LEVELS TXT ranges --- */
    pos = dir_off;
    if(fseek(f, pos, SEEK_SET) != 0) { fclose(f); printf("BADDIR\t-\t-\t-\t-\t-\t-\t%s\n", display); return 1; }
    while((long)pos + 12 <= fsize - 4 && nlevels < MAX_LEVELS)
    {
        uint32_t pns, start, size;
        char name[4096];
        if(fread(&pns, 4, 1, f) != 1) break;
        if(pns < 12 || pns > 4096) break;
        if(fread(&start, 4, 1, f) != 1) break;
        if(fread(&size, 4, 1, f) != 1) break;
        {
            uint32_t nlen = pns - 12;
            if(nlen >= sizeof(name)) break;
            if(nlen && fread(name, 1, nlen, f) != nlen) break;
            name[nlen] = 0;
        }
        if(has(name, "LEVELS") && has(name, ".TXT") &&
           size > 0 && size < MAX_LVL_SIZE &&
           (long)start + (long)size <= fsize)
        {
            levels[nlevels].start = start;
            levels[nlevels].size  = size;
            nlevels++;
        }
        pos += pns;
    }

    /* --- read each level file and parse group/spawn/at --- */
    buf = (char *)malloc(MAX_LVL_SIZE + 1);
    if(!buf) { fclose(f); printf("ERR\t-\t-\t-\t-\t-\t-\t%s\n", display); return 1; }

    for(i = 0; i < nlevels; i++)
    {
        int ats[MAX_SPAWNS]; int nats = 0;
        char *line, *save;
        size_t got;

        if(fseek(f, levels[i].start, SEEK_SET) != 0) continue;
        got = fread(buf, 1, levels[i].size, f);
        if(got == 0) continue;
        buf[got] = 0;

        for(line = strtok_r(buf, "\r\n", &save); line; line = strtok_r(NULL, "\r\n", &save))
        {
            char *p = line;
            while(*p == ' ' || *p == '\t') p++;
            if(!*p || *p == '#') continue;

            if(strncasecmp(p, "group", 5) == 0 && (p[5] == ' ' || p[5] == '\t'))
            {
                int mn, mx;
                if(sscanf(p + 5, "%d %d", &mn, &mx) == 2)
                {
                    if(mx < 1) mx = 1;
                    if(ncaps < MAX_SECTIONS) caps[ncaps++] = mx;
                    if(mx > maxcap) maxcap = mx;
                    if(mx >= 100) uncapped++;
                }
            }
            else if(strncasecmp(p, "spawn", 5) == 0 && (p[5] == ' ' || p[5] == '\t'))
            {
                total_spawns++;
            }
            else if(strncasecmp(p, "at", 2) == 0 && (p[2] == ' ' || p[2] == '\t'))
            {
                int a;
                if(sscanf(p + 2, "%d", &a) == 1 && nats < MAX_SPAWNS) ats[nats++] = a;
            }
        }

        /* densest 320 px trigger window in this level (upper bound only) */
        if(nats > 1)
        {
            int j, k, best = 0;
            qsort(ats, nats, sizeof(int), cmp_int);
            for(j = 0, k = 0; j < nats; j++)
            {
                while(k < nats && ats[k] < ats[j] + 320) k++;
                if(k - j > best) best = k - j;
            }
            if(best > maxburst) maxburst = best;
        }
    }

    free(buf);
    fclose(f);

    {
        int medcap = 0;
        if(ncaps)
        {
            qsort(caps, ncaps, sizeof(int), cmp_int);
            medcap = caps[ncaps / 2];
        }
        printf("%d\t%d\t%d\t%d\t%d\t%d\t%d\t%s\n",
               nlevels, ncaps, uncapped, maxcap, medcap, total_spawns, maxburst, display);
    }
    return 0;
}

int main(int argc, char **argv)
{
    const char *dir = (argc > 1) ? argv[1] : ".";
    int first = (argc > 2) ? atoi(argv[2]) : 0;
    int count = (argc > 3) ? atoi(argv[3]) : 1000000;
    DIR *d = opendir(dir);
    struct dirent *de;
    int idx = 0, done = 0;
    char path[4096];

    if(!d) { fprintf(stderr, "cannot open %s\n", dir); return 1; }
    printf("#levels\tsections\tuncapped\tmaxcap\tmedcap\tspawns\tmaxburst\tpak\n");
    while((de = readdir(d)) != NULL)
    {
        const char *n = de->d_name;
        size_t l = strlen(n);
        if(l < 5 || strcasecmp(n + l - 4, ".pak") != 0) continue;
        if(idx++ < first) continue;
        if(done >= count) break;
        snprintf(path, sizeof(path), "%s/%s", dir, n);
        scan_pak(path, n);
        fflush(stdout);
        done++;
    }
    closedir(d);
    fprintf(stderr, "scanned %d PAKs (from index %d)\n", done, first);
    return 0;
}
