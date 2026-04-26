/*
 * MiSTer_OpenBOR — sdlport.c Patch
 *
 * Adds NativeVideoWriter initialization, save directory creation,
 * and OSD PAK loading support to OpenBOR's main() function.
 *
 * PATCH: In sdl/sdlport.c, replace the entire main() function
 * (line 52 through line 118) with the version below.
 *
 * Also add these includes at the top of the file, after the existing includes:
 *
 *   #ifdef MISTER_NATIVE_VIDEO
 *   #include "native_video_writer.h"
 *   #include "native_audio_writer.h"
 *   #include <sys/stat.h>
 *   #include <stdlib.h>
 *   #include <time.h>
 *   #include <unistd.h>
 *   #include <pthread.h>
 *   #include <signal.h>
 *   #include <execinfo.h>
 *   #endif
 *
 * Copyright (C) 2026 MiSTer Organize -- GPL-3.0
 */

#ifdef MISTER_NATIVE_VIDEO
/* Crash handler — prints fault address and backtrace to stderr
 * so we can see exactly where the segfault happens. */
static void mister_crash_handler(int sig, siginfo_t *info, void *ucontext)
{
    void *bt[30];
    int count;
    ucontext_t *uc = (ucontext_t *)ucontext;

    fprintf(stderr, "\n=== CRASH: signal %d at address %p ===\n", sig, info->si_addr);

    /* ARM: get PC, LR, SP from the signal context */
    fprintf(stderr, "  PC = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_pc);
    fprintf(stderr, "  LR = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_lr);
    fprintf(stderr, "  SP = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_sp);
    fprintf(stderr, "  R0 = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_r0);
    fprintf(stderr, "  R1 = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_r1);
    fprintf(stderr, "  R2 = 0x%08lx\n", (unsigned long)uc->uc_mcontext.arm_r2);

    /* Try backtrace — may be empty on ARM but worth trying */
    count = backtrace(bt, 30);
    if (count > 0) {
        fprintf(stderr, "Backtrace (%d frames):\n", count);
        backtrace_symbols_fd(bt, count, STDERR_FILENO);
    }

    /* Also print the PC offset from the binary base for addr2line */
    FILE *maps = fopen("/proc/self/maps", "r");
    if (maps) {
        char line[256];
        fprintf(stderr, "Maps (first 5):\n");
        int n = 0;
        while (fgets(line, sizeof(line), maps) && n < 5) {
            fprintf(stderr, "  %s", line);
            n++;
        }
        fclose(maps);
    }

    fprintf(stderr, "=== END CRASH ===\n");
    fflush(stderr);
    _exit(139);
}

/* PAK swap detection thread — polls .s0 for path changes during gameplay.
 * When user mounts a new PAK from OSD, .s0 updates instantly. This thread
 * detects the change and triggers a clean shutdown so the daemon restarts
 * OpenBOR with the new PAK. */
static volatile int mister_swap_requested = 0;
static char mister_loaded_path[256] = {0};

static void *mister_swap_thread(void *arg)
{
    (void)arg;
    char check_path[256];

    while (!mister_swap_requested) {
        sleep(1);
        FILE *f = fopen("/media/fat/config/OpenBOR_4086.s0", "r");
        if (!f) continue;
        check_path[0] = 0;
        if (fgets(check_path, sizeof(check_path), f)) {
            char *nl = strchr(check_path, '\n');
            if (nl) *nl = 0;
            char *cr = strchr(check_path, '\r');
            if (cr) *cr = 0;
        }
        fclose(f);

        if (strlen(check_path) > 0 && strlen(mister_loaded_path) > 0) {
            char full[256];
            snprintf(full, sizeof(full), "/media/fat/%s", check_path);
            if (strcmp(full, mister_loaded_path) != 0) {
                fprintf(stderr, "MiSTer: PAK swap detected: %s\n", full);
                mister_swap_requested = 1;
                borExit(1);
            }
        }
    }
    return NULL;
}
#endif

int main(int argc, char *argv[])
{
#ifndef SKIP_CODE
    char pakname[256];
#endif
#ifdef CUSTOM_SIGNAL_HANDLER
    struct sigaction sigact;
#endif

#ifdef DARWIN
    char resourcePath[PATH_MAX];
    CFBundleRef mainBundle;
    CFURLRef resourcesDirectoryURL;
    mainBundle = CFBundleGetMainBundle();
    resourcesDirectoryURL = CFBundleCopyResourcesDirectoryURL(mainBundle);
    if(!CFURLGetFileSystemRepresentation(resourcesDirectoryURL, true, (UInt8 *) resourcePath, PATH_MAX))
    {
        borExit(0);
    }
    CFRelease(resourcesDirectoryURL);
    chdir(resourcePath);
#elif WII
    fatInitDefault();
#endif

#ifdef CUSTOM_SIGNAL_HANDLER
    sigact.sa_sigaction = handleFatalSignal;
    sigact.sa_flags = SA_RESTART | SA_SIGINFO;

    if(sigaction(SIGSEGV, &sigact, NULL) != 0)
    {
        printf("Error setting signal handler for %d (%s)\n", SIGSEGV, strsignal(SIGSEGV));
        exit(EXIT_FAILURE);
    }
#endif

#ifdef MISTER_NATIVE_VIDEO
    /* Install crash handler FIRST — catches segfaults with backtrace */
    {
        struct sigaction sa;
        sa.sa_sigaction = mister_crash_handler;
        sa.sa_flags = SA_SIGINFO | SA_RESETHAND;
        sigemptyset(&sa.sa_mask);
        sigaction(SIGSEGV, &sa, NULL);
        sigaction(SIGBUS, &sa, NULL);
        sigaction(SIGABRT, &sa, NULL);
    }
    setenv("SDL_VIDEODRIVER", "dummy",  1);
    setenv("SDL_AUDIODRIVER", "dummy",  1);
#endif

    setSystemRam();
    initSDL();

#ifdef MISTER_NATIVE_VIDEO
    /* Initialize DDR3 native video writer */
    if (!NativeVideoWriter_Init()) {
        fprintf(stderr, "NativeVideoWriter: init failed, falling back to SDL\n");
    }

    /* Initialize DDR3 native audio writer. sblaster's SB_playstart
     * thread will refuse to start if this fails. */
    if (!NativeAudioWriter_Init()) {
        fprintf(stderr, "NativeAudioWriter: init failed, audio will be silent\n");
    }

    /* Create MiSTer directories */
    mkdir("/media/fat/saves", 0755);
    mkdir("/media/fat/saves/OpenBOR_4086", 0755);
    mkdir("/media/fat/savestates", 0755);
    mkdir("/media/fat/savestates/OpenBOR_4086", 0755);
    mkdir("/media/fat/config", 0755);
#endif

    packfile_mode(0);
#ifdef ANDROID
    dirExists(rootDir, 1);
    chdir(rootDir);
#endif
    dirExists(paksDir, 1);
#ifdef MISTER_NATIVE_VIDEO
    /* Saves redirected to /media/fat/saves/OpenBOR_4086/ in utils.c. */
    dirExists(logsDir, 1);
#else
    dirExists(savesDir, 1);
    dirExists(logsDir, 1);
    dirExists(screenShotsDir, 1);
#endif

#ifdef ANDROID
    if(dirExists("/mnt/usbdrive/OpenBOR/Paks", 0))
        strcpy(paksDir, "/mnt/usbdrive/OpenBOR/Paks");
    else if(dirExists("/usbdrive/OpenBOR/Paks", 0))
        strcpy(paksDir, "/usbdrive/OpenBOR/Paks");
    else if(dirExists("/mnt/extsdcard/OpenBOR/Paks", 0))
        strcpy(paksDir, "/mnt/extsdcard/OpenBOR/Paks");
#endif

#ifdef MISTER_NATIVE_VIDEO
    /* Cart cache lives on tmpfs (/tmp). This is critical for load
     * speed: OpenBOR reads the PAK every time it starts, so the cache
     * has to be in RAM -- reading a 100+ MB PAK off SD takes minutes.
     * /tmp survives the Reset Pak exit+relaunch cycle (same Linux
     * session), and is cleared on reboot which is the right behaviour
     * -- after a reboot the user will re-pick through MiSTer's OSD,
     * the cart will stream via ioctl into DDR3, and we cache it here
     * fresh. */
    #define MISTER_PAK_CACHE "/tmp/openbor_current.pak"
    #define MISTER_S0_PATH   "/media/fat/config/OpenBOR_4086.s0"
    {
        /* SC0 (mounted image + config) approach: user selects PAK
         * from OSD, MiSTer writes path to .s0 config INSTANTLY —
         * no ioctl streaming, no 2-minute wait, no RAM exhaustion.
         * ARM reads .s0 to get path, loads PAK from SD directly. */

        /* 1) Check for Reset Pak cache (in /tmp, survives exit+relaunch) */
        struct stat st;
        if (stat(MISTER_PAK_CACHE, &st) == 0 && st.st_size > 0) {
            strncpy(packfile, MISTER_PAK_CACHE, sizeof(packfile) - 1);
            packfile[sizeof(packfile) - 1] = 0;
            fprintf(stderr, "MiSTer: Reset Pak cache found: %s (%ld bytes)\n",
                    packfile, (long)st.st_size);
        }
        /* 2) Poll for .s0 (MiSTer creates it instantly when user picks from OSD) */
        else {
            char s0_path[256] = {0};

            fprintf(stderr, "MiSTer: waiting for OSD PAK selection (.s0)...\n");
            while (1) {
                FILE *f = fopen(MISTER_S0_PATH, "r");
                if (f) {
                    if (fgets(s0_path, sizeof(s0_path), f)) {
                        char *nl = strchr(s0_path, '\n');
                        if (nl) *nl = 0;
                        char *cr = strchr(s0_path, '\r');
                        if (cr) *cr = 0;
                    }
                    fclose(f);
                    if (strlen(s0_path) > 0) {
                        /* .s0 contains full path (not relative like .f0) */
                        snprintf(packfile, sizeof(packfile), "/media/fat/%s", s0_path);
                        fprintf(stderr, "MiSTer: OSD selected: %s\n", packfile);
                        break;
                    }
                }
                usleep(200000);  /* poll every 200ms */
            }
        }
    }
#else
    Menu();
#endif

#ifndef SKIP_CODE
    getPakName(pakname, -1);
    video_set_window_title(pakname);
#endif
#ifdef MISTER_NATIVE_VIDEO
    /* Save loaded path for swap detection and start watcher thread */
    strncpy(mister_loaded_path, packfile, sizeof(mister_loaded_path) - 1);
    pthread_t swap_tid;
    pthread_create(&swap_tid, NULL, mister_swap_thread, NULL);
#endif

    fprintf(stderr, "MiSTer: entering openborMain()...\n");
    openborMain(argc, argv);
    fprintf(stderr, "MiSTer: openborMain() returned normally\n");

#ifdef MISTER_NATIVE_VIDEO
    mister_swap_requested = 1;
    pthread_join(swap_tid, NULL);
#endif

#ifdef MISTER_NATIVE_VIDEO
    NativeVideoWriter_Shutdown();
    NativeAudioWriter_Shutdown();
#endif

    borExit(0);
    return 0;
}
