#ifndef __GENERIC_CONSOLE_V2_H
#define __GENERIC_CONSOLE_V2_H
// Serial console framing-v2 (BCH) de-frame — a framing transform that lets
// an application board accept intentproto v2 frames on the UART console and
// reply in v2, while the inner stock v1 command block and its ARQ are
// preserved. Compiled only when CONFIG_WANT_CONSOLE_FRAMING_V2. UNPROVEN —
// needs hardware bring-up. See docs/Protocol_v2.md.

#include <stdint.h>

// RX: try to handle a v2 frame at buf[0]. Returns
//   > 0  a v2 frame was consumed (dispatched or dropped); *consumed set to
//        the number of bytes to pop from the receive buffer
//     0  no v2 frame here -> the caller runs the stock v1 path
//   < 0  an incomplete v2 frame is in progress -> the caller waits
int_fast8_t console_v2_try_rx(uint8_t *buf, uint_fast8_t len,
                              uint_fast8_t *consumed);

// TX: if the v2 link has latched (a valid v2 frame was received), re-frame
// the v1 frame already built in buf (v1len bytes) to a v2 frame in place and
// return the new length. Returns v1len unchanged if not latched or if the
// v2 frame would not fit in cap.
uint_fast8_t console_v2_wrap_tx(uint8_t *buf, uint_fast8_t v1len,
                                uint_fast8_t cap);

#endif // console_v2.h
