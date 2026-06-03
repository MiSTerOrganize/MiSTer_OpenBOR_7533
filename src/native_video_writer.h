//
//  Native Video DDR3 Writer — OpenBOR MiSTer
//
//  API for writing frames from ARM to DDR3 for FPGA native video output.
//  Also provides joystick reading and cart loading via DDR3 shared memory.
//
//  STEP 60 / Option Y (2026-06-01): variable-res write. ARM no longer
//  squishes frames to a fixed 320×224 framebuffer. Instead, frames are
//  written at their NATIVE source resolution (up to 1920×1080). FPGA
//  reads the dimensions from a DIM word at offset 0x04 and does the
//  edge-aware downscale-to-display on its side. Preserves source detail
//  through to the FPGA — supports future aspect-ratio modes (Original /
//  Full Screen / ARC1 / ARC2 per feature matrix row 14) and pixel-perfect
//  scaling options.
//
//  Copyright (C) 2026 MiSTer Organize — GPL-3.0
//

#ifndef NATIVE_VIDEO_WRITER_H
#define NATIVE_VIDEO_WRITER_H

#include <stdbool.h>
#include <stdint.h>

// Display target (Sega CD V28 NTSC active area). FPGA downscales TO these
// dimensions from the native-res source frame written by ARM.
#define NV_TARGET_WIDTH  320
#define NV_TARGET_HEIGHT 224

// Max native source res ARM is allowed to write. Covers all current PAKs
// (Lust Rush 1600×900 fits) + future-proofs.
#define NV_MAX_WIDTH  1920
#define NV_MAX_HEIGHT 1080

// Legacy aliases (kept for any caller that still references them).
#define NV_WIDTH   NV_TARGET_WIDTH
#define NV_HEIGHT  NV_TARGET_HEIGHT

/// Initialize DDR3 native video writer. Maps /dev/mem at 0x3A000000.
bool NativeVideoWriter_Init(void);

/// Release DDR3 mapping.
void NativeVideoWriter_Shutdown(void);

/// Write one frame to DDR3 double-buffer at NATIVE source resolution.
/// FPGA reads dimensions from the DIM ctrl word and downscales to display.
/// @param pixels   Source pixel data from SDL_Surface->pixels
/// @param width    Surface width (1 .. NV_MAX_WIDTH)
/// @param height   Surface height (1 .. NV_MAX_HEIGHT)
/// @param pitch    Source row stride in BYTES (SDL_Surface->pitch)
/// @param bpp      Bits per pixel (8, 16, or 32)
/// @param palette  Palette data for 8bpp (256 entries × 3 bytes RGB), NULL otherwise
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

/// Check if FPGA has loaded a cart file (returns file size, 0 if none).
uint32_t NativeVideoWriter_CheckCart(void);

/// Read cart data from DDR3 into buffer. Returns bytes read.
uint32_t NativeVideoWriter_ReadCart(void* buf, uint32_t max_size);

/// Acknowledge cart receipt (clears FPGA cart control word).
void NativeVideoWriter_AckCart(void);

#endif
