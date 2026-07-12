// C-callable shim over the intentproto datagram link layer
//
// This is the only translation unit in the firmware that speaks C++;
// it exists purely to expose lib/intentproto's datagram codec -
// 16-bit datagram sequencing, traffic-class tag, truncated
// HMAC-SHA256 authentication (FD-0001 doc 07) - to the C console
// glue in udp_console.c.  It matches lib/intentproto/tools/
// udp_bridge.py byte for byte.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
//
// This file may be distributed under the terms of the GNU GPLv3 license.

#include "intentproto/datagram.hpp"

extern "C" {
#include "udp_datagram.h"
}

static_assert(UDPDG_DATAGRAM_MAX == intentproto::DATAGRAM_MAX
              , "datagram max mismatch");
static_assert(UDPDG_HEADER == intentproto::DATAGRAM_HEADER
              , "datagram header mismatch");
static_assert(UDPDG_TAG == intentproto::DATAGRAM_TAG
              , "datagram tag mismatch");

static intentproto::DatagramTx DGTx;
static intentproto::DatagramRx DGRx;
// Set by udpdg_decode when the datagram it just consumed was a parity
// that reconstructed a single lost datagram into DGRx.held; cleared by
// udpdg_take_recovered.  This gates take_recovered so it only fires on
// a genuine reconstruction, never on the survivors accumulator that is
// otherwise sitting in DGRx.held mid-block.
static bool RecoveryPending;

extern "C" void
udpdg_init(const uint8_t *psk, uint32_t psk_len, uint8_t fec_k)
{
    // XOR erasure parity: on tx, datagram_encode folds each datagram
    // into a running parity block and udpdg_parity_flush emits the
    // parity every k; on rx, datagram_decode consumes parity and
    // reconstructs a single lost datagram of the block, which
    // udpdg_take_recovered then hands back for in-order reassembly.
    // fec_k==0 (default) leaves the whole erasure layer off, so the
    // legacy behaviour - loss handled purely by the frame layer's
    // ARQ - is preserved unchanged.
    intentproto::datagram_tx_init(&DGTx, psk, psk_len, fec_k);
    intentproto::datagram_rx_init(&DGRx, psk, psk_len);
    RecoveryPending = false;
}

extern "C" uint32_t
udpdg_encode(uint8_t *out, const uint8_t *frames, uint32_t len)
{
    return (uint32_t)intentproto::datagram_encode(
        &DGTx, out, frames, len, intentproto::TrafficClass::Scheduled);
}

extern "C" uint32_t
udpdg_parity_flush(uint8_t *out)
{
    return (uint32_t)intentproto::datagram_parity_flush(&DGTx, out);
}

extern "C" int32_t
udpdg_decode(uint8_t *data, uint32_t len, const uint8_t **frames)
{
    intentproto::TrafficClass cls;
    bool was_parity = len >= intentproto::DATAGRAM_HEADER
        && (data[2] & intentproto::DGF_PARITY);
    uint32_t lost_before = DGRx.lost;
    int32_t r = (int32_t)intentproto::datagram_decode(
        &DGRx, data, len, frames, &cls);
    // A single-loss reconstruction is exactly: a parity datagram was
    // consumed (r==0), it accounted the missing datagram this call
    // (lost advanced), and the survivors+parity XOR now sits in held.
    // That combination excludes the no-loss parity (held cleared) and
    // a stale/duplicate parity (lost unchanged), so we never mistake
    // the mid-block survivors accumulator for a reconstruction.
    RecoveryPending = (r == 0 && was_parity && DGRx.lost > lost_before
                       && DGRx.holding && DGRx.held_len);
    return r;
}

extern "C" uint32_t
udpdg_take_recovered(uint8_t *out, uint32_t cap)
{
    if (!RecoveryPending)
        return 0;
    RecoveryPending = false;
    return (uint32_t)intentproto::datagram_take_recovered(&DGRx, out, cap);
}

extern "C" void
udpdg_get_stats(struct udpdg_stats *st)
{
    st->rx_lost = DGRx.lost;
    st->rx_reordered = DGRx.reordered;
    st->rx_auth_failures = DGRx.auth_failures;
}
