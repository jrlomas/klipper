// Serial console framing-v2 (BCH) de-frame.
//
// A framing transform (docs/Protocol_v2.md, docs/Upstream_Tracking.md): the
// host bridge re-frames klippy's stock v1 frames as intentproto v2 (BCH
// error-correcting) frames on the wire; this reconstructs the exact inner v1
// frame and hands it to the stock command dispatcher, so command.c and the
// v1 ARQ (seq/ack/retransmit) are untouched. Replies are re-framed to v2
// once a v2 frame has been seen (the link "latches"). This is NOT
// intentproto's HostSession (which replaces the ARQ and is only for the
// bootloader). frame_v2 comes from framing_v2.cpp over the already-linked
// BCH codec — nothing protocol is reimplemented here.
//
// Kept out of the hot path: only console_task calls in, and only when
// CONFIG_WANT_CONSOLE_FRAMING_V2 is set. The transform itself is LIVE-
// tested against linuxprocess firmware (test/console_v2_live_test.py:
// dual-accept, latch, 3-bit BCH correction); the serial_irq (silicon
// UART) call sites still await hardware bring-up.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include <string.h> // memcpy
#include "board/misc.h" // crc16_ccitt
#include "command.h" // command_find_block, MESSAGE_*
#include "console_v2.h" // console_v2_try_rx
#include "framing_v2.h" // fv2_decode

// Latches on the first valid v2 frame received; from then on replies are
// re-framed to v2. Reset only by a board restart (like the device-side
// latch in the library). A v2 host always probes with v2 frames, so this
// tracks the negotiated framing without a separate handshake.
static uint8_t v2_latched;

int_fast8_t
console_v2_try_rx(uint8_t *buf, uint_fast8_t len, uint_fast8_t *consumed)
{
    if (len < MESSAGE_HEADER_SIZE)
        return 0;
    uint_fast8_t flen = buf[0];
    if (flen < MESSAGE_MIN || flen > FV2_MAX)
        return 0; // not a plausible frame length (e.g. a resync sync byte)
    if (!(buf[1] & FV2_FLAG))
        return 0; // a legacy v1 frame -> stock path
    if (len < flen)
        return -1; // a v2 frame is mid-arrival; wait for the rest

    const uint8_t *payload;
    uint8_t seq;
    int32_t plen = fv2_decode(buf, flen, &payload, &seq);
    if (plen < 0) {
        // Uncorrectable: drop the frame. The inner v1 ARQ retransmits.
        *consumed = flen;
        return 1;
    }
    v2_latched = 1;

    uint_fast8_t v1len = (uint_fast8_t)(MESSAGE_HEADER_SIZE + plen
                                        + MESSAGE_TRAILER_SIZE);
    if (v1len > MESSAGE_MAX) {
        *consumed = flen;
        return 1; // oversize inner frame; drop
    }
    // Rebuild the exact stock v1 frame and dispatch it as the stock path
    // would, so command.c sees an ordinary v1 frame (CRC recomputed).
    uint8_t v1[MESSAGE_MAX];
    v1[0] = v1len;
    v1[1] = MESSAGE_DEST | (seq & MESSAGE_SEQ_MASK);
    memcpy(&v1[MESSAGE_HEADER_SIZE], payload, plen);
    uint16_t crc = crc16_ccitt(v1, v1len - MESSAGE_TRAILER_SIZE);
    v1[v1len - 3] = crc >> 8;
    v1[v1len - 2] = crc & 0xff;
    v1[v1len - 1] = MESSAGE_SYNC;

    uint_fast8_t pop_count;
    int_fast8_t ret = command_find_block(v1, v1len, &pop_count);
    if (ret > 0) {
        command_dispatch(v1, pop_count);
        command_send_ack();
    }
    *consumed = flen;
    return 1;
}

uint_fast8_t
console_v2_wrap_tx(uint8_t *buf, uint_fast8_t v1len, uint_fast8_t cap)
{
    if (!v2_latched || v1len < MESSAGE_MIN)
        return v1len;
    uint_fast8_t plen = (uint_fast8_t)(v1len - MESSAGE_HEADER_SIZE
                                       - MESSAGE_TRAILER_SIZE);
    uint8_t v2[FV2_MAX];
    uint32_t v2len = fv2_encode(v2, &buf[MESSAGE_HEADER_SIZE], plen,
                                buf[1] & MESSAGE_SEQ_MASK);
    if (!v2len || v2len > cap)
        return v1len; // no room; leave the v1 frame in place
    memcpy(buf, v2, v2len);
    return (uint_fast8_t)v2len;
}
