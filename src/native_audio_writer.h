//
//  Native Audio DDR3 Writer -- OpenBOR MiSTer
//
//  Pushes 48 kHz stereo S16 PCM samples into a DDR3 ring buffer read by
//  the FPGA native audio path. No ALSA, no Linux sound kernel.
//
//  Memory map (matches openbor_video_reader.sv after Option Y Phase 4):
//    0x3A000030  audio_wr_ptr  (32-bit byte offset into ring; ARM writes)
//    0x3A000038  audio_rd_ptr  (32-bit byte offset into ring; FPGA writes)
//    0x3A880000  audio ring    (65,536 bytes = 16,384 stereo frames)
//
//  Moved from old 0x3A0D0000 to 0x3A880000 as part of Option Y Phase 4's
//  variable-res memory map expansion (BUF1 at 0x3A400000 4MB-aligned;
//  cart data at 0x3A800000; audio ring past cart data).
//
//  Copyright (C) 2026 MiSTer Organize -- GPL-3.0
//

#ifndef NATIVE_AUDIO_WRITER_H
#define NATIVE_AUDIO_WRITER_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

#define NA_SAMPLE_RATE 48000
#define NA_CHANNELS    2
#define NA_BYTES_PER_FRAME  4   /* 2 ch * int16 */

/// Initialize DDR3 audio writer. Maps /dev/mem at 0x3A000030 region.
/// Returns true on success. Safe to call multiple times.
bool NativeAudioWriter_Init(void);

/// Release DDR3 audio mapping.
void NativeAudioWriter_Shutdown(void);

/// True once Init() has succeeded.
bool NativeAudioWriter_IsActive(void);

/// Submit stereo S16 frames to the DDR3 ring.
///
/// Returns the number of frames actually written. If the ring is too
/// full, older unread samples are NOT overwritten -- the tail of the
/// requested batch is silently dropped. (Never blocks, never sleeps.)
///
/// Safe to call from the SDL audio callback thread.
size_t NativeAudioWriter_Submit(const int16_t *frames, size_t frame_count);

/// Free space in the ring, in stereo frames. Useful for flow control.
size_t NativeAudioWriter_FreeFrames(void);

#endif
