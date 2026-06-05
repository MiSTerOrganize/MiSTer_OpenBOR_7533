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

/// Write one frame to DDR3 double-buffer, converting to RGB565.
/// @param pixels   Source pixel data from SDL_Surface->pixels
/// @param width    Surface width (must be <= 320)
/// @param height   Surface height (must be <= 224)
/// @param pitch    Source row stride in BYTES (SDL_Surface->pitch)
/// @param bpp      Bits per pixel (8, 16, or 32)
/// @param palette  Palette data for 8bpp (SDL_Color array), NULL otherwise
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

/// Read joystick state for player 0-3 from DDR3 (written by FPGA).
uint32_t NativeVideoWriter_ReadJoystick(int player);

/* Phase 7a (2026-06-05): CheckCart / ReadCart / AckCart removed as
 * dead code. OpenBOR loads PAKs via filesystem (.s0 path -> fopen)
 * not via DDR3 ioctl streaming. */

#endif
