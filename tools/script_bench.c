/*
 * script_bench.c -- OpenBOR script lex + symbol-resolve cost on the A9.
 *
 * *** COST-SHAPE MODEL, not a verbatim port. *** The script path is the LARGEST
 * load cost (~41% of JL Legacy: applex 11.4s lexer + script 9.7s compile +
 * parseloop), but OpenBOR's Interpreter (openborscript.c: Interpreter_ParseText
 * lexer + Interpreter_CompileInstructions) is deeply entangled with the engine's
 * List, ScriptVariant and symbol tables -- it can't be lifted into a self-
 * contained bench. This models its DOMINANT cost shape: a char-by-char tokenize
 * scan over animation_script text + a 256-bucket string-hashed symbol-table
 * insert/lookup per identifier (mirroring OpenBOR's List 256-bucket strhash, the
 * same structure the loadsprite-hash + List_FindByName work uses).
 *
 * Use it to answer "is the lexer char-scan-bound or symbol-hash-bound, and would
 * a faster tokenizer/hash help?" -- then cross-check the ABSOLUTE number against
 * the in-engine [LOAD] applex/script buckets (which measure the real Interpreter).
 * The load-floor analysis already found this path is the floor (List hashed,
 * exact-dedup maxed); this bench is the PAK-free confirmation tool for that.
 *
 * Build (CI): arm-linux-gnueabihf-gcc -O2 -static -mcpu=cortex-a9 -mfpu=neon
 *             -mfloat-abi=hard tools/script_bench.c -o script_bench -lrt
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>

static double now_ns(void){ struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (double)t.tv_sec*1e9+(double)t.tv_nsec; }

/* 256-bucket string-hashed symbol table (mirrors OpenBOR List strhash shape). */
#define NBUCK 256
typedef struct sym { char name[32]; struct sym *next; } sym;
static sym *buckets[NBUCK];
static sym *pool; static int pool_n, pool_cap;

static unsigned djb2(const char *s,int len){            /* lowercased, like the List hash */
    unsigned h=5381; int i; for(i=0;i<len;i++){ char c=s[i]; if(c>='A'&&c<='Z')c+=32; h=((h<<5)+h)^(unsigned char)c; } return h;
}
static void sym_reset(void){ memset(buckets,0,sizeof buckets); pool_n=0; }
/* insert-if-absent + return whether it was a hit (lookup). */
static int sym_touch(const char *s,int len){
    if(len>31) len=31;
    unsigned b = djb2(s,len) & (NBUCK-1);
    sym *p; for(p=buckets[b]; p; p=p->next)
        if((int)strlen(p->name)==len && memcmp(p->name,s,len)==0) return 1;   /* hit */
    sym *n=&pool[pool_n++]; memcpy(n->name,s,len); n->name[len]=0; n->next=buckets[b]; buckets[b]=n; /* miss -> insert */
    return 0;
}

int main(void){
    /* synthesize ~256KB of animation_script-like text: function defs with a mix
     * of fresh + repeated identifiers, numbers, operators (the tokenizer's diet). */
    long CAP = 256*1024; char *txt = malloc(CAP+64); long n=0;
    int fn=0;
    static const char *kw[8]={"void","int","self","if","while","return","spawn","entity"};
    while(n < CAP-128){
        n += sprintf(txt+n, "%s func_%d(%s a, int b){ ", kw[fn&7], fn, kw[(fn+1)&7]);
        int s; for(s=0;s<6;s++) n += sprintf(txt+n, "%s.x_%d = a + %d * b_%d; ", kw[(fn+s)&7], s, s*7, s&3);
        n += sprintf(txt+n, "if(a > 0){ return spawn_%d; } }\n", fn&15);
        fn++;
    }
    long total = n;
    pool_cap = (int)(total/4)+16; pool = malloc((size_t)pool_cap*sizeof(sym));

    printf("== script_bench (OpenBOR, A9) -- COST-SHAPE: tokenize + 256-bucket symbol-hash over %ldKB script ==\n", total/1024);

    long tokens=0, idents=0, hits=0;
    /* warm + count */
    {
        sym_reset(); long i=0;
        while(i<total){
            char c=txt[i];
            if(c==' '||c=='\t'||c=='\n'){ i++; continue; }
            if((c>='A'&&c<='Z')||(c>='a'&&c<='z')||c=='_'){
                long j=i+1; while(j<total){ char d=txt[j]; if((d>='A'&&d<='Z')||(d>='a'&&d<='z')||(d>='0'&&d<='9')||d=='_') j++; else break; }
                idents++; if(sym_touch(txt+i,(int)(j-i))) hits++; tokens++; i=j;
            } else if(c>='0'&&c<='9'){
                long j=i+1; while(j<total && txt[j]>='0'&&txt[j]<='9') j++; tokens++; i=j;
            } else { tokens++; i++; }   /* operator / punctuation */
        }
    }

    int REP=20,r; double best=1e30;
    for(r=0;r<5;r++){
        double t0=now_ns(); int k;
        for(k=0;k<REP;k++){
            sym_reset(); long i=0;
            while(i<total){
                char c=txt[i];
                if(c==' '||c=='\t'||c=='\n'){ i++; continue; }
                if((c>='A'&&c<='Z')||(c>='a'&&c<='z')||c=='_'){
                    long j=i+1; while(j<total){ char d=txt[j]; if((d>='A'&&d<='Z')||(d>='a'&&d<='z')||(d>='0'&&d<='9')||d=='_') j++; else break; }
                    sym_touch(txt+i,(int)(j-i)); i=j;
                } else if(c>='0'&&c<='9'){ long j=i+1; while(j<total && txt[j]>='0'&&txt[j]<='9') j++; i=j; }
                else i++;
            }
        }
        double dt=now_ns()-t0; if(dt<best)best=dt;
    }
    double per = best/REP;   /* one full lex+hash pass */
    printf("tokens %ld (idents %ld, %ld hits / %ld inserts)\n", tokens, idents, hits, idents-hits);
    printf("lex+hash: %.3f ns/char, %.2f ns/token  =  %.3f ms for this %ldKB script\n",
           per/total, per/tokens, per/1e6, total/1024);
    printf("(COST-SHAPE only -- cross-check the absolute ms against the in-engine [LOAD] applex+script buckets;\n");
    printf(" a big PAK's combined scripts are MBs, so scale linearly by total script bytes.)\n");
    free(txt); free(pool); return 0;
}
