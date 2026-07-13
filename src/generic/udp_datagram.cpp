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
    return (int32_t)intentproto::datagram_decode(
        &DGRx, data, len, frames, &cls);
}

extern "C" int
udpdg_is_authenticated_static(uint8_t *data, uint32_t len)
{
    // Session mode requires a PSK. In trust_network mode a raw handshake
    // and a static datagram are not distinguishable by authentication, so
    // never claim a packet here.
    if (!DGRx.psk_len)
        return 0;
    return intentproto::datagram_authenticates(&DGRx, data, len) ? 1 : 0;
}

extern "C" uint32_t
udpdg_take_recovered(uint8_t *out, uint32_t cap)
{
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
// window). Static-PSK datagrams remain the pre-session fallback: a host
// that never sends a ClientHello keeps using udpdg_encode/decode, while a
// negotiated session pins subsequent data traffic to its stronger keys.
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
static bool SessPeerAdopted;
static uint8_t SessActiveHello[intentproto::SEC_MSG_MAX];
static uint32_t SessActiveHelloLen;
static uint8_t SessActiveReply[intentproto::SEC_MSG_MAX];
static uint32_t SessActiveReplyLen;

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

static void
sess_clear_active(void)
{
    SessActiveHelloLen = SessActiveReplyLen = 0;
}

static uint32_t
sess_on_hello(intentproto::SecureSession *s, const uint8_t *msg,
              uint32_t len, uint8_t *out, uint32_t cap)
{
    if (len > intentproto::SEC_MSG_MAX)
        return 0;
    if (s->state == intentproto::SecState::WaitClientFin) {
        // A retransmit of the same ClientHello is idempotent. A different
        // unauthenticated hello cannot replace an in-progress handshake.
        if (len == SessActiveHelloLen
            && !memcmp(msg, SessActiveHello, len)) {
            if (cap < SessActiveReplyLen)
                return 0;
            memcpy(out, SessActiveReply, SessActiveReplyLen);
            return SessActiveReplyLen;
        }
        return 0;
    }
    sess_fresh_init(s);
    uint32_t n = (uint32_t)s->on_handshake(msg, len, out, cap);
    if (n) {
        memcpy(SessActiveHello, msg, len);
        SessActiveHelloLen = len;
        memcpy(SessActiveReply, out, n);
        SessActiveReplyLen = n;
    }
    return n;
}

static bool
sess_try_fin(intentproto::SecureSession *s, const uint8_t *msg,
             uint32_t len, uint8_t *out, uint32_t cap)
{
    if (s->state != intentproto::SecState::WaitClientFin)
        return false;
    // Verify on a copy. A forged ClientFin must not fail or reset the
    // legitimate half-open handshake.
    intentproto::SecureSession trial = *s;
    trial.on_handshake(msg, len, out, cap);
    if (!trial.established())
        return false;
    *s = trial;
    sess_clear_active();
    return true;
}

extern "C" int
udpsess_msg_type(const uint8_t *data, uint32_t len)
{
    // Classify a raw datagram for the console router:
    //   1 = a ClientHello (handshake start, carrying its PSK proof)
    //   3 = a ClientFin (handshake completion)
    //   2 = a session data datagram (DGF_SESSION set)
    //   0 = neither (route to the static path)
    // Session negotiation requires a PSK. In explicit trust-network mode,
    // every packet belongs to the static path; interpreting sequence bytes
    // as handshake tags would only create collisions.
    if (!DGRx.psk_len || len < 1)
        return 0;
    if (data[0] == intentproto::SEC_MSG_CLIENT_HELLO) {
        if (len < 3 + intentproto::SEC_RANDOM_SIZE
            || data[1] != intentproto::SEC_PROTO_VERSION
            || data[2] > intentproto::SEC_ID_MAX
            || len != 3 + intentproto::SEC_RANDOM_SIZE + data[2]
                      + intentproto::SEC_HELLO_PROOF_SIZE)
            return 0;
        return 1;
    }
    if (data[0] == intentproto::SEC_MSG_CLIENT_FIN)
        return len == 1 + intentproto::SEC_FINISHED_SIZE ? 3 : 0;
    if ((data[0] & intentproto::DGF_SESSION)
        && len >= intentproto::SEC_DG_HEADER + intentproto::SEC_DG_TAG)
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
    SessPeerAdopted = false;
    sess_clear_active();
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
        uint32_t n = 0;
        if (len >= 1 && msg[0] == intentproto::SEC_MSG_CLIENT_HELLO)
            n = sess_on_hello(&SessRx, msg, len, out, cap);
        else if (len >= 1 && msg[0] == intentproto::SEC_MSG_CLIENT_FIN
                 && sess_try_fin(&SessRx, msg, len, out, cap)) {
            SessPeerAdopted = true;
        }
        return n;
    }
    // Live session: drive the PENDING handshake; adopt it only when the
    // ClientFin proves the peer holds the PSK (a reconnecting host).
    // Unauthenticated hellos can therefore never reset live keys.
    uint32_t n = 0;
    bool adopted = false;
    if (len >= 1 && msg[0] == intentproto::SEC_MSG_CLIENT_HELLO)
        n = sess_on_hello(&SessPending, msg, len, out, cap);
    else if (len >= 1 && msg[0] == intentproto::SEC_MSG_CLIENT_FIN)
        adopted = sess_try_fin(&SessPending, msg, len, out, cap);
    if (adopted) {
        SessRx = SessPending;
        SessPeerAdopted = true;
        // Consume the authenticated pending handshake. A later stray
        // ClientFin must not re-adopt an unrelated rx candidate.
        sess_fresh_init(&SessPending);
    }
    return n;
}

extern "C" void
udpsess_reset_handshake(void)
{
    sess_clear_active();
    if (SessRx.established())
        sess_fresh_init(&SessPending);
    else
        sess_fresh_init(&SessRx);
}

extern "C" int
udpsess_take_peer_adopted(void)
{
    bool adopted = SessPeerAdopted;
    SessPeerAdopted = false;
    return adopted ? 1 : 0;
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
