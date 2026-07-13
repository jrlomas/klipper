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

#include <string.h> // memcpy
#include "autoconf.h" // CONFIG_WANT_DATAGRAM_SESSION
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

// ---- optional DTLS-class session responder (session_sec.hpp) ----
// The board is the RESPONDER: it waits for the host's ClientHello and,
// once the 3-message handshake completes, all datagrams are session
// datagrams (auth with rotating per-session keys, epoch rotation, replay
// window). Static-PSK datagrams remain the permanent fallback: a host
// that never sends a ClientHello keeps using udpdg_encode/decode.
#if CONFIG_WANT_DATAGRAM_SESSION
#include "intentproto/session_sec.hpp"

static intentproto::SecureSession SessRx;
// Re-handshake support: a LIVE session is never reset by an
// unauthenticated packet. A ClientHello arriving while SessRx is
// established drives this separate pending session instead; it replaces
// SessRx only once its ClientFin proves PSK knowledge. Before
// establishment, each ClientHello re-inits SessRx fresh - that unwedges a
// half-open handshake (lost/spoofed hello) AND gives every handshake a
// unique responder nonce, so a replayed old handshake can never re-derive
// old session keys.
static intentproto::SecureSession SessPending;
static uint8_t SessPsk[64];
static uint32_t SessPskLen;
static uint8_t SessBoardId[intentproto::SEC_ID_MAX];
static uint32_t SessIdLen;
static uint8_t SessNonce[16];
static uint32_t SessHsCount;

static void
sess_fresh_init(intentproto::SecureSession *s)
{
    // Unique per-handshake nonce: the boot nonce XOR a counter.
    // Uniqueness (not secrecy) is the requirement; the PSK authenticates.
    uint8_t nonce[16];
    uint32_t c = ++SessHsCount;
    for (int i = 0; i < 16; i++)
        nonce[i] = SessNonce[i] ^ (uint8_t)(c >> ((i & 3) * 8));
    s->init(intentproto::SecRole::Responder, SessPsk, SessPskLen,
            SessBoardId, SessIdLen, nonce, intentproto::SEC_DEFAULT_REKEY);
}

extern "C" int
udpsess_msg_type(const uint8_t *data, uint32_t len)
{
    // Classify a raw datagram for the console router:
    //   1 = a ClientHello (handshake start - the rate-gated message)
    //   3 = a ClientFin (handshake completion - never gated, or a
    //       reconnect would livelock against the gate)
    //   2 = a session data datagram (DGF_SESSION set)
    //   0 = neither (route to the static path)
    if (len < 1)
        return 0;
    if (data[0] == intentproto::SEC_MSG_CLIENT_HELLO)
        return 1;
    if (data[0] == intentproto::SEC_MSG_CLIENT_FIN)
        return 3;
    if (data[0] & intentproto::DGF_SESSION)
        return 2;
    return 0;
}

extern "C" void
udpsess_init(const uint8_t *psk, uint32_t psk_len, const uint8_t *board_id,
             uint32_t id_len, const uint8_t *random16)
{
    // Own the key material: the session objects keep pointers, and the
    // pending/adopt dance re-inits them after the caller's stack frame
    // is long gone.
    if (psk_len > sizeof(SessPsk))
        psk_len = sizeof(SessPsk);
    memcpy(SessPsk, psk, psk_len);
    SessPskLen = psk_len;
    if (id_len > sizeof(SessBoardId))
        id_len = sizeof(SessBoardId);
    memcpy(SessBoardId, board_id, id_len);
    SessIdLen = id_len;
    memcpy(SessNonce, random16, sizeof(SessNonce));
    SessHsCount = 0;
    sess_fresh_init(&SessRx);
}

extern "C" int
udpsess_established(void)
{
    return SessRx.established() ? 1 : 0;
}

extern "C" uint32_t
udpsess_on_handshake(const uint8_t *msg, uint32_t len, uint8_t *out,
                     uint32_t cap)
{
    if (!SessRx.established()) {
        // Not yet live: every ClientHello restarts the handshake on a
        // fresh instance (unwedges half-open state; unique nonce).
        if (len >= 1 && msg[0] == intentproto::SEC_MSG_CLIENT_HELLO)
            sess_fresh_init(&SessRx);
        return (uint32_t)SessRx.on_handshake(msg, len, out, cap);
    }
    // Live session: drive the PENDING handshake; adopt it only when the
    // ClientFin proves the peer holds the PSK (a reconnecting host).
    // Unauthenticated hellos can therefore never reset live keys.
    if (len >= 1 && msg[0] == intentproto::SEC_MSG_CLIENT_HELLO)
        sess_fresh_init(&SessPending);
    uint32_t n = (uint32_t)SessPending.on_handshake(msg, len, out, cap);
    if (SessPending.established())
        SessRx = SessPending;
    return n;
}

extern "C" uint32_t
udpsess_encode(uint8_t *out, uint32_t cap, const uint8_t *frames,
               uint32_t len)
{
    return (uint32_t)SessRx.datagram_encode(
        out, cap, frames, len, intentproto::TrafficClass::Scheduled);
}

extern "C" int32_t
udpsess_decode(uint8_t *data, uint32_t len, const uint8_t **frames)
{
    intentproto::TrafficClass cls;
    return (int32_t)SessRx.datagram_decode(data, len, frames, &cls);
}
#endif // CONFIG_WANT_DATAGRAM_SESSION
