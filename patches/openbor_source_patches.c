/*
 * MiSTer_OpenBOR — Source Patches for OpenBOR Build 3979
 *
 * Two patches to apply to your OpenBOR 3979 source tree
 * (rofl0r/openbor SVN branch, commit 3b0a718).
 *
 * PATCH 1: SDL Video Intercept (sdl/video.c)
 * PATCH 2: Save Directory Redirect (source/utils.c)
 *
 * Copyright (C) 2026 MiSTer Organize — GPL-3.0
 */


/* ======================================================================
 * PATCH 1: SDL Video Intercept — sdl/video.c
 *
 * Redirects video output from SDL_Flip to DDR3 for FPGA native video.
 *
 * Step 1: Add this include at the top of sdl/video.c, after the other
 *         #include lines (around line 30):
 *
 *   #ifdef MISTER_NATIVE_VIDEO
 *   #include "native_video_writer.h"
 *   #endif
 *
 * Step 2: Find the SDL_Flip block at line 382-387:
 *
 *   #if SDL2
 *       SDL_BlitSurface(screen, NULL, SDL_GetWindowSurface(window), NULL);
 *       SDL_UpdateWindowSurface(window);
 *   #else
 *       SDL_Flip(screen);
 *   #endif
 *
 * Replace the entire block with:
 *
 *   #ifdef MISTER_NATIVE_VIDEO
 *       NativeVideoWriter_WriteFrame(screen->pixels, screen->w, screen->h,
 *           screen->format->BitsPerPixel,
 *           screen->format->palette ? screen->format->palette->colors : NULL);
 *   #elif SDL2
 *       SDL_BlitSurface(screen, NULL, SDL_GetWindowSurface(window), NULL);
 *       SDL_UpdateWindowSurface(window);
 *   #else
 *       SDL_Flip(screen);
 *   #endif
 *
 * That's it. When compiled with -DMISTER_NATIVE_VIDEO, OpenBOR writes
 * frames to DDR3 instead of calling SDL_Flip. When compiled without
 * the flag, the original SDL path is used unchanged.
 * ====================================================================== */


/* ======================================================================
 * PATCH 2: Save Directory Redirect — source/utils.c
 *
 * Redirects saves to /media/fat/saves/OpenBOR/ instead of <rootDir>/Saves/
 *
 * Find the COPY_ROOT_PATH macro for the Linux/default case (around line 80):
 *
 *   #define COPY_ROOT_PATH(buf, name) strcpy(buf, rootDir); strncat(buf, name, strlen(name)); strncat(buf, "/", 1);
 *
 * Replace it with:
 *
 *   #ifdef MISTER_NATIVE_VIDEO
 *   #define COPY_ROOT_PATH(buf, name) \
 *       do { \
 *           if (strcmp(name, "Saves") == 0 || strcmp(name, "Saves/") == 0) { \
 *               strcpy(buf, "/media/fat/saves/OpenBOR/"); \
 *           } else { \
 *               strcpy(buf, rootDir); strncat(buf, name, strlen(name)); strncat(buf, "/", 1); \
 *           } \
 *       } while(0)
 *   #else
 *   #define COPY_ROOT_PATH(buf, name) strcpy(buf, rootDir); strncat(buf, name, strlen(name)); strncat(buf, "/", 1);
 *   #endif
 *
 * When compiled with -DMISTER_NATIVE_VIDEO, any getBasePath() call with
 * "Saves" redirects to /media/fat/saves/OpenBOR_4086/. All other paths
 * (Paks, Logs) use the original rootDir behavior.
 *
 * The saves directory is created automatically by OpenBOR if it doesn't
 * exist (dirExists with create=1 is called in the save functions).
 * ====================================================================== */
