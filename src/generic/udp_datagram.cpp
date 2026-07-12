// C-callable shim over the intentproto datagram link layer
//
// This is the only translation unit in the firmware that speaks C++;
// it exists purely to expose lib/intentproto's datagram codec -
// 16-bit datagram sequencing, traffic-class tag, truncated
// HMAC-SHA256 authentication (RFC 0001 doc 07) - to the C console
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

extern "C" void
udpdg_init(const uint8_t *psk, uint32_t psk_len)
{
    // XOR erasure parity (fec_k) stays off until the receive path
    // grows in-order block reassembly; loss recovery is the frame
    // layer's ARQ for now.
    intentproto::datagram_tx_init(&DGTx, psk, psk_len, 0);
    intentproto::datagram_rx_init(&DGRx, psk, psk_len);
}

extern "C" uint32_t
udpdg_encode(uint8_t *out, const uint8_t *frames, uint32_t len)
{
    return (uint32_t)intentproto::datagram_encode(
        &DGTx, out, frames, len, intentproto::TrafficClass::Scheduled);
}

extern "C" int32_t
udpdg_decode(uint8_t *data, uint32_t len, const uint8_t **frames)
{
    intentproto::TrafficClass cls;
    return (int32_t)intentproto::datagram_decode(
        &DGRx, data, len, frames, &cls);
}

extern "C" void
udpdg_get_stats(struct udpdg_stats *st)
{
    st->rx_lost = DGRx.lost;
    st->rx_reordered = DGRx.reordered;
    st->rx_auth_failures = DGRx.auth_failures;
}
