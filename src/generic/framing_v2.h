#ifndef __GENERIC_FRAMING_V2_H
#define __GENERIC_FRAMING_V2_H
// C-callable shim over lib/intentproto's stateless framing-v2 (BCH) codec.
// Used by the serial console-v2 de-frame (console_v2.c). See framing_v2.cpp.

#include <stdint.h>

#define FV2_FLAG 0x80        // seq-byte bit marking a v2 frame
#define FV2_OVERHEAD 7       // len, seq, 4 BCH parity, sync
// A v2 frame is a v1 frame with a 4-byte BCH trailer instead of the 2-byte
// CRC, so its max length is MESSAGE_MAX + 2.
#define FV2_MAX (64 + 2)

// Encode payload -> a BCH v2 frame in out (>= len + FV2_OVERHEAD); returns
// the frame length, or 0 on error.
uint32_t fv2_encode(uint8_t *out, const uint8_t *payload, uint32_t len,
                    uint8_t seq);
// Decode (and BCH-correct in place) one complete v2 frame. Returns payload
// length with *payload/*seq set, or <0 if uncorrectable/malformed.
int32_t fv2_decode(uint8_t *frame, uint32_t len, const uint8_t **payload,
                   uint8_t *seq);

#endif // framing_v2.h
