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
#define NV_HEIGHT  240

/// Initialize DDR3 native video writer. Maps /dev/mem at 0x3A000000.
bool NativeVideoWriter_Init(void);

/// Release DDR3 mapping.
void NativeVideoWriter_Shutdown(void);

/// Write one frame to DDR3 double-buffer, converting to RGB565.
/// @param pixels   Source pixel data from SDL_Surface->pixels
/// @param width    Surface width (must be <= 320)
/// @param height   Surface height (must be <= 240)
/// @param pitch    Source row stride in BYTES (SDL_Surface->pitch)
/// @param bpp      Bits per pixel (8, 16, or 32)
/// @param palette  Palette data for 8bpp (SDL_Color array), NULL otherwise
void NativeVideoWriter_WriteFrame(const void* pixels, int width, int height,
                                  int pitch, int bpp, const void* palette);

/// True if DDR3 writer is initialized and ready.
bool NativeVideoWriter_IsActive(void);

/// Read joystick state for player 0-3 from DDR3 (written by FPGA).
uint32_t NativeVideoWriter_ReadJoystick(int player);

/// Request the next WriteFrame call to dump per-pixel debug samples
/// to stderr. Used by control_patch's Select-button trigger so the
/// user can grab a snapshot of whatever frame is currently showing.
void NativeVideoWriter_RequestDebugDump(void);

/// Check if FPGA has loaded a cart file (returns file size, 0 if none).
uint32_t NativeVideoWriter_CheckCart(void);

/// Read cart data from DDR3 into buffer. Returns bytes read.
uint32_t NativeVideoWriter_ReadCart(void* buf, uint32_t max_size);

/// Acknowledge cart receipt (clears FPGA cart control word).
void NativeVideoWriter_AckCart(void);

#endif
