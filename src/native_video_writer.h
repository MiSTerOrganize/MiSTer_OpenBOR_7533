//
//  Native Video DDR3 Writer — OpenBOR MiSTer
//
//  API for writing frames from ARM to DDR3 for FPGA native video output.
//  Also provides joystick reading and cart loading via DDR3 shared memory.
//
//  Copyright (C) 2026 MiSTer Organize — GPL-3.0
//

#ifndef NATIVE_VIDEO_WRITER_H
#define NATIVE_VIDEO_WRITER_H

#include <stdbool.h>
#include <stdint.h>

#define NV_WIDTH   320
#define NV_HEIGHT  224   /* Sega CD V28 NTSC active area */

/// Initialize DDR3 native video writer. Maps /dev/mem at 0x3A000000.
bool NativeVideoWriter_Init(void);

/// Release DDR3 mapping.
void NativeVideoWriter_Shutdown(void);

/// Write one frame to DDR3 triple-buffer, converting to RGB565.
/// @param pixels   Source pixel data from SDL_Surface->pixels
/// @param width    Surface width (must be <= 320)
/// @param height   Surface height (must be <= 224)
/// @param pitch    Source row stride in BYTES (SDL_Surface->pitch)
/// @param bpp      Bits per pixel (8, 16, or 32)
/// @param palette  Palette data for 8bpp (SDL_Color array), NULL otherwise
/// Show a short line at the top of the screen for `seconds` (0 = default 4).
/// Uppercased and word-wrapped, up to 3 lines, drawn POST-downscale so it is
/// not squished with the game image on a PAK that renders above 320x224.
///
/// For anything the player must KNOW — a refused replay, a version mismatch,
/// take-over, playback finishing. Those all used to go only to OpenBorLog.txt,
/// which is unreadable from a couch. Keep the log line too: this is the
/// headline, the log is the detail.
void NativeVideoWriter_Notice(const char* msg, int seconds);

/* Draw the fps read-out + any pending notice into a frame about to be
 * published. EVERY publisher of a DDR3 frame must call this immediately before
 * its publish barrier, not just WriteFrame -- the SDL dummy driver's
 * mister_present writes the same three buffers, and when it skipped the overlays
 * the published frames alternated between having a notice and not, which is the
 * flicker reported 2026-08-04. Call exactly once per published frame: the
 * notice countdown advances here. */
void NativeVideoWriter_DrawOverlays(volatile uint16_t* dst);

void NativeVideoWriter_WriteFrame(const void* pixels, int width, int height,
                                  int pitch, int bpp, const void* palette);

/// True if DDR3 writer is initialized and ready.
bool NativeVideoWriter_IsActive(void);

/// Keepalive tick — increments frame counter pointing at the last-written
/// buffer. Called by a 150ms-interval thread elsewhere (typically the SDL
/// dummy driver's mister_keepalive_fn) to prevent the FPGA's 30-vblank
/// staleness blank-out during idle (wait-for-PAK, pause menu, etc.).
/// Shares state with NativeVideoWriter_WriteFrame — using a separate
/// keepalive counter caused jitter (loading bar bug 2026-05-22).
void NativeVideoWriter_KeepaliveTick(void);

/* Hand a finished buffer to the keepalive (see the .c). Call after the buffer
 * is fully drawn and before publishing it. */
void NativeVideoWriter_NotePublished(int buf);

/// Read joystick state for player 0-3 from DDR3 (written by FPGA).
uint32_t NativeVideoWriter_ReadJoystick(int player);

/// Check if FPGA has loaded a cart file (returns file size, 0 if none).
uint32_t NativeVideoWriter_CheckCart(void);

/// Read cart data from DDR3 into buffer. Returns bytes read.
uint32_t NativeVideoWriter_ReadCart(void* buf, uint32_t max_size);

/// Acknowledge cart receipt (clears FPGA cart control word).
void NativeVideoWriter_AckCart(void);

#endif
