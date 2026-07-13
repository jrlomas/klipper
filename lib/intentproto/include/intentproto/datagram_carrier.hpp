#ifndef INTENTPROTO_DATAGRAM_CARRIER_HPP
#define INTENTPROTO_DATAGRAM_CARRIER_HPP
// Host-side glue that binds the UDP datagram link layer (datagram.hpp)
// to a HostSession's framed byte stream (host.hpp) - the symmetric
// complement of the device-side src/generic/udp_console.c, and the
// counterpart to the CanCarrier binding for CAN.
//
// A HostSession produces and consumes whole v1/v2 frames through its
// WriteFn / on_rx; datagram_encode/decode wrap and unwrap exactly those
// whole frames with authentication (truncated HMAC-SHA256) and optional
// XOR erasure FEC. This carrier wires the two together so a host can run
// a full ARQ session over UDP without the caller re-implementing the
// datagram accounting - the piece the intentproto README tracked as
// "not yet implemented" (datagram transport bound to the session's
// framed byte stream).
//
// Freestanding profile: no heap, no exceptions; caller-owned buffers.
//
// Copyright (C) 2026  JR Lomas <lomas.jr@gmail.com>
// MIT licensed (see LICENSE).

#include <stddef.h>
#include <stdint.h>

#include "intentproto/datagram.hpp"

namespace intentproto {

struct HostSession;  // host.hpp

struct DatagramCarrier {
    DatagramTx tx;
    DatagramRx rx;
    HostSession* session;
    // Emit one UDP datagram; return >= 0 on success (mirrors the CAN
    // carrier's send contract). Called from write_frame().
    int (*send)(const uint8_t* dgram, size_t len, void* user);
    void* user;
    // tx scratch: a datagram is the wrapped frame plus header+tag.
    uint8_t txbuf[DATAGRAM_MAX];

    // Bind a HostSession to a UDP datagram transport. psk_len == 0 is
    // the explicit trust_network (unauthenticated) mode, matching the
    // datagram layer. fec_k == 0 leaves the erasure layer off.
    void init(HostSession* s, const uint8_t* psk, size_t psk_len,
              uint8_t fec_k,
              int (*send_fn)(const uint8_t*, size_t, void*), void* user_in);

    // HostSession::WriteFn-compatible: wrap the whole frame the session
    // just produced in an authenticated datagram and send it, then emit
    // a parity datagram when the FEC block is due. Returns the session's
    // frame length on success (so it satisfies the WriteFn contract) or
    // a negative send error.
    int write_frame(const uint8_t* frames, size_t len);

    // Feed one received UDP datagram: authenticate + sequence-check,
    // forward the inner frames to session->on_rx(), and replay any
    // datagram a parity reconstructed. A rejected datagram (bad tag,
    // malformed, stale) is dropped - the session's ARQ recovers it.
    void on_datagram(const uint8_t* data, size_t len);
};

// HostSession::WriteFn thunk: set HostSession::write = datagram_write_thunk
// and write_user = &carrier. Frames go out as Scheduled-class datagrams
// (the write hook does not carry the per-frame class; the class tag is
// best-effort QoS and the session already accounts classes internally).
int datagram_write_thunk(const uint8_t* data, size_t len, void* user);

} // namespace intentproto

#endif // INTENTPROTO_DATAGRAM_CARRIER_HPP
