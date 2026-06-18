/*
 * parseloop_bench.c -- OpenBOR_7533 model-command parse loop cost-shape (A9).
 *
 * *** COST-SHAPE MODEL, not a verbatim port. *** The in-engine [LOAD].parseloop
 * bucket measures load_cached_model()'s `while(pos < size)` walk over a model.txt:
 * for each line it tokenizes the line into args (ParseArgs) and dispatches the
 * leading keyword (getModelCommand -> a big command switch). That dispatch calls
 * deep into the engine (loadsprite, animation tables, palette pipeline, the
 * entire s_model fill) and CANNOT be lifted into a self-contained bench. So this
 * models the DOMINANT COST SHAPE of the parse loop itself: scan a synthetic
 * in-memory model.txt-like buffer char-by-char, split each line on whitespace
 * into an argv[], and do a representative keyword dispatch (string-compare the
 * leading token against a small command table, exactly the getModelCommand
 * shape). It does NOT execute any command body -- the point is how the TOKENIZE +
 * DISPATCH cost scales with line count (1k / 5k / 20k lines), the parse floor,
 * NOT the per-command engine work.
 *
 * Cross-check the ABSOLUTE ms against the in-engine [LOAD].parseloop bucket;
 * this is the PAK-free confirmation tool for "is the parse loop tokenize-bound
 * or dispatch-bound, and would a faster tokenizer/command-hash help?"
 *
 * Run pinned to the memory-fast render core:  taskset 0x01 ./parseloop_bench [L...]
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/parseloop_bench.c -o parseloop_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

/* representative model.txt command vocabulary (the getModelCommand switch shape).
 * Real OpenBOR has ~200 commands; the dispatch is a linear strcmp scan over the
 * MODEL_CMD table, so the cost shape is "strcmp the leading token against the
 * table until a hit". We use a representative subset; the per-line dispatch cost
 * is dominated by the average number of strcmp probes, same as the engine. */
static const char *cmd_table[] = {
    "name","type","health","speed","anim","frame","delay","offset","bbox",
    "attack","hitflash","sound","palette","alternatepal","remap","gfxshadow",
    "shadow","jumpframe","landframe","loop","range","move","subject_to_gravity",
    "no_adjust_base","icon","scroll","setlayer","spawnframe","summonframe",
    "blast","throwframe","jumpheight","grabforce","antigrab","script","onspawnscript"
};
#define NCMD (int)(sizeof(cmd_table)/sizeof(cmd_table[0]))

/* representative getModelCommand: linear strcmp scan, returns index or -1.
 * Cost shape matches the engine's MODEL_CMD lookup. */
static int get_model_command(const char *tok, int len){
    int i;
    for(i=0;i<NCMD;i++){
        const char *c = cmd_table[i];
        if((int)strlen(c)==len && memcmp(c,tok,len)==0) return i;
    }
    return -1;
}

/* sink so -O2 cannot delete the work */
static volatile long g_sink;

/* synthesize a model.txt-like buffer of L lines. Deterministic, no random.
 * Each line: "<command> <arg> <arg> ..." with a mix of recognized commands and
 * "anim"/"frame" bulk lines (which dominate real model.txt files). */
static char *make_model_txt(int L, long *out_bytes){
    /* generous capacity: avg line ~40 chars */
    long cap = (long)L * 64 + 256;
    char *txt = (char*)malloc((size_t)cap);
    if(!txt){ *out_bytes=0; return NULL; }
    long n=0; int i;
    for(i=0;i<L;i++){
        /* ~70% of lines are frame/anim bulk (the real model.txt majority) */
        int kind = i % 10;
        if(kind < 7){
            n += sprintf(txt+n, "frame data/chars/x/idle_%d.gif\n", i & 255);
        } else if(kind == 7){
            n += sprintf(txt+n, "anim %s\n", (i&1) ? "idle" : "walk");
        } else if(kind == 8){
            n += sprintf(txt+n, "offset %d %d\n", (i*3)%64, (i*5)%96);
        } else {
            /* a recognized header command with a couple of args */
            const char *c = cmd_table[i % NCMD];
            n += sprintf(txt+n, "%s %d %d\n", c, (i*7)%100, (i*11)%100);
        }
        if(n > cap-128) break;   /* safety */
    }
    *out_bytes = n;
    return txt;
}

/* the parse loop cost shape: walk the buffer line by line, split into args,
 * dispatch the leading token via get_model_command. Returns a sink value. */
static long run_parseloop(const char *txt, long size){
    long pos=0; long acc=0;
    while(pos < size){
        /* skip leading blank/newline (like the engine's line advance) */
        while(pos<size && (txt[pos]=='\n' || txt[pos]=='\r')) pos++;
        if(pos>=size) break;
        long line_start = pos;
        while(pos<size && txt[pos]!='\n') pos++;       /* find end of line */
        long line_end = pos;

        /* ParseArgs shape: split [line_start,line_end) on whitespace into args */
        long p = line_start; int argc_=0;
        long first_tok=-1, first_len=0;
        while(p < line_end){
            while(p<line_end && (txt[p]==' '||txt[p]=='\t')) p++;   /* skip ws */
            if(p>=line_end) break;
            long ts=p;
            while(p<line_end && txt[p]!=' ' && txt[p]!='\t') p++;   /* token */
            int tlen=(int)(p-ts);
            if(argc_==0){ first_tok=ts; first_len=tlen; }
            argc_++;
            acc += tlen;   /* touch every token so the scan isn't optimized away */
        }

        /* getModelCommand dispatch on the leading token (no command body run) */
        if(first_tok>=0){
            int cmd = get_model_command(txt+first_tok, first_len);
            acc += cmd + argc_;
        }
    }
    return acc;
}

int main(int argc,char**argv){
    /* line counts to sweep (default 1k / 5k / 20k) */
    int counts[8]; int ncounts=0;
    if(argc>1){ int i; for(i=1;i<argc && ncounts<8;i++) counts[ncounts++]=atoi(argv[i]); }
    else { counts[0]=1000; counts[1]=5000; counts[2]=20000; ncounts=3; }

    printf("== parseloop_bench (OpenBOR_7533, A9) -- COST-SHAPE of [LOAD].parseloop ==\n");
    printf("   (tokenize + getModelCommand dispatch are cost-shape models; NO command body is run.\n");
    printf("    command table = %d entries, linear strcmp dispatch -- cross-check absolute ms\n", NCMD);
    printf("    against the in-engine [LOAD].parseloop bucket.)\n\n");
    printf("%-8s %10s %10s %12s %12s %12s\n",
           "lines","bytes","ms/pass","ns/line","ns/byte","MB/s");
    printf("%-8s %10s %10s %12s %12s %12s\n",
           "--------","--------","--------","--------","--------","--------");

    int i;
    for(i=0;i<ncounts;i++){
        int L=counts[i];
        long bytes=0;
        char *txt = make_model_txt(L, &bytes);
        if(!txt){ printf("%-8d  alloc failed\n", L); continue; }

        /* warm */
        g_sink += run_parseloop(txt, bytes);

        const int REP=15; int r; double best=1e30;
        for(r=0;r<REP;r++){
            double t0=now_ns();
            g_sink += run_parseloop(txt, bytes);
            double dt=now_ns()-t0;
            if(dt<best) best=dt;
        }
        double ms   = best/1e6;
        double nsl  = best/(double)L;
        double nsb  = best/(double)bytes;
        double mbps = (double)bytes / (best/1e9) / (1024.0*1024.0);
        printf("%-8d %10ld %10.3f %12.2f %12.3f %12.1f\n", L, bytes, ms, nsl, nsb, mbps);
        free(txt);
    }
    printf("\nReading: cost should scale linearly with line count; a big PAK's combined model.txt\n");
    printf("files run to tens of thousands of lines, so scale by total lines. If ns/line is high\n");
    printf("relative to ns/byte*line-length, the strcmp dispatch dominates (a command hash would\n");
    printf("help); if ns/byte dominates, it's the char-scan tokenizer.\n");
    return 0;
}
