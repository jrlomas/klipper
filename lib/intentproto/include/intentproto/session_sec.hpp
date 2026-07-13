#ifndef INTENTPROTO_SESSION_SEC_HPP
#define INTENTPROTO_SESSION_SEC_HPP
// Optional session-security layer for the datagram transport
// (FD-0001 doc 07, "Security" section — the deferred "heavier
// machinery: DTLS, key rotation, per-board identities").
//
// SCOPE DECISION. Full IETF DTLS 1.3 in a freestanding, no-heap,
// no-STL library is out of proportion to this codebase and could not
// be honestly verified here: it would drag in X.509/ASN.1, an AEAD
// suite, record-replay/epoch machinery, cookie exchange and a
// certificate or full PSK-with-(EC)DHE key schedule — thousands of
// lines whose correctness this fork cannot audit. Instead this file
// implements a PURPOSE-BUILT lightweight authenticated session that
// delivers exactly the four properties FD-0001 doc 07 names, using ONLY the
// primitives already in the library:
//
//   * session keys, not the raw PSK on every packet  — HKDF-SHA256
//     (built on the existing HMAC) derives per-session traffic keys
//     from PSK + exchanged nonces;
//   * key rotation                                   — an epoch byte
//     re-derives the traffic key on a datagram-count threshold or an
//     explicit rekey();
//   * per-board identity                             — a board id is
//     carried in the handshake and exposed to the caller;
//   * replay protection                              — a 64-entry
//     sliding window over the per-epoch sequence number.
//
// AUTH-ONLY, NOT CONFIDENTIALITY. Like the static-PSK path, this layer
// AUTHENTICATES datagrams (truncated HMAC-SHA256 over the whole
// datagram, now keyed by a rotating session key) but does NOT encrypt
// the payload. That is deliberate: the threat FD-0001 doc 07 states is forgery
// and blind replay of motion/heater commands by anything on the
// network segment — not secrecy of the commands themselves. A stream
// cipher (an HKDF-derived keystream XORed over the payload) could be
// added later without re-framing, but it buys nothing against the
// stated threat model while adding a keystream generator and its
// nonce-reuse footgun to unverifiable freestanding crypto. Auth with
// rotated per-session keys is the right floor here; if confidentiality
// is ever required it is a clean addition on top of this schedule.
//
// Pure state machine, freestanding profile: no heap, no exceptions,
// no RTTI, no virtual dispatch, no I/O, and no time or RNG reads. Like
// host.hpp, entropy enters only as an argument: the caller supplies
// this peer's random nonce to init() from its own RNG. Feed it bytes,
// it emits bytes and reaches Established.
//
// NEGOTIATION / DOWNGRADE. Session security is an OFFER layered over
// the untouched static-PSK datagram path (datagram.hpp), which stays
// the default bootstrap/fallback when no session is established. The
// board integration pins data traffic to the session after negotiation.
// The initiator emits a
// ClientHello; a peer that does not support the session layer never
// answers with a ServerHello, so the initiator never reaches
// Established and the caller keeps using datagram_encode/decode with
// the static PSK. downgrade() records that decision explicitly.
// Session-protected datagrams are marked with DGF_SESSION (flags bit
// 4); the static path neither sets nor inspects that bit.

#include <stddef.h>
#include <stdint.h>

#include "datagram.hpp"
#include "hmac.hpp"

namespace intentproto {

// Nonce exchanged by each side; 128 bits is ample for uniqueness
// across a link's lifetime given the keys are PSK-authenticated.
constexpr size_t SEC_RANDOM_SIZE = 16;
// Board identity: a short id/name carried in the handshake.
constexpr size_t SEC_ID_MAX = 24;
// Traffic and finished keys are full SHA-256 width.
constexpr size_t SEC_KEY_SIZE = SHA256_DIGEST_SIZE;
// Finished-MAC truncation (handshake authentication proof).
constexpr size_t SEC_FINISHED_SIZE = 16;
// Session datagram header: flags, epoch, u32 per-epoch sequence.
constexpr size_t SEC_DG_HEADER = 6;
constexpr size_t SEC_DG_TAG = DATAGRAM_TAG; // 8-byte truncated HMAC
// Default auto-rekey threshold (datagrams per epoch). Chosen well
// under the u32 sequence space; the caller may override per link.
constexpr uint32_t SEC_DEFAULT_REKEY = 1u << 20;

// Handshake message type tags (first byte of each message).
constexpr uint8_t SEC_MSG_CLIENT_HELLO = 0x51;
constexpr uint8_t SEC_MSG_SERVER_HELLO = 0x52;
constexpr uint8_t SEC_MSG_CLIENT_FIN = 0x53;
constexpr uint8_t SEC_PROTO_VERSION = 1;

// Largest handshake message (ServerHello: type,ver,id_len, random,
// id, finished MAC).
constexpr size_t SEC_MSG_MAX =
    3 + SEC_RANDOM_SIZE + SEC_ID_MAX + SEC_FINISHED_SIZE;

enum class SecRole : uint8_t { Initiator, Responder };

enum class SecState : uint8_t {
    Idle,               // before start()/first message
    WaitServerHello,    // initiator sent ClientHello
    WaitClientFin,      // responder sent ServerHello
    Established,         // keys live, data path open
    Failed,             // auth/parse failure or explicit downgrade
};

struct SecureSession {
    // ---- state (read-only outside; mutate via the methods) ----
    SecRole role;
    SecState state;

    const uint8_t* psk;
    size_t psk_len;

    uint8_t prk[SHA256_DIGEST_SIZE];        // HKDF-Extract output
    uint8_t client_random[SEC_RANDOM_SIZE];
    uint8_t server_random[SEC_RANDOM_SIZE];

    uint8_t my_id[SEC_ID_MAX];
    size_t my_id_len;
    uint8_t peer_id_buf[SEC_ID_MAX];
    size_t peer_id_length;

    // Transmit direction: current epoch, per-epoch sequence, key.
    uint32_t tx_epoch;
    uint32_t tx_seq;
    uint8_t tx_key[SEC_KEY_SIZE];
    uint32_t rekey_threshold;

    // Receive direction: current epoch, key, sliding replay window.
    uint32_t rx_epoch;
    uint8_t rx_key[SEC_KEY_SIZE];
    uint32_t rx_window_top;     // highest accepted seq in rx_epoch
    uint64_t rx_window_bits;    // bit d set => (top - d) already seen

    // Diagnostics.
    uint32_t auth_failures;
    uint32_t replays_rejected;
    uint32_t old_epoch_rejected;

    // ---- handshake API ----
    // Reset state and install parameters. board_id (<= SEC_ID_MAX) is
    // this peer's identity; my_random (SEC_RANDOM_SIZE bytes) is a
    // fresh nonce the caller draws from its own RNG. rekey_threshold
    // is the per-epoch datagram count that triggers an automatic key
    // rotation (0 selects SEC_DEFAULT_REKEY).
    void init(SecRole role_, const uint8_t* psk_, size_t psk_len_,
              const uint8_t* board_id, size_t id_len,
              const uint8_t* my_random,
              uint32_t rekey = SEC_DEFAULT_REKEY);

    // Initiator only: write the ClientHello into out (>= SEC_MSG_MAX).
    // Returns its length, or 0 on misuse.
    size_t start(uint8_t* out, size_t cap);

    // Feed one received handshake message. Any reply is written into
    // out (>= SEC_MSG_MAX) and its length returned (0 if none). Check
    // established()/failed() afterwards.
    size_t on_handshake(const uint8_t* msg, size_t len,
                        uint8_t* out, size_t cap);

    bool established() const { return state == SecState::Established; }
    bool failed() const { return state == SecState::Failed; }

    // Record that the peer did not accept the session offer (no
    // ServerHello); the caller falls back to the static-PSK path.
    void downgrade() { state = SecState::Failed; }

    // Per-board identity of the peer, valid once the handshake has
    // exchanged hellos.
    const uint8_t* peer_id() const { return peer_id_buf; }
    size_t peer_id_len() const { return peer_id_length; }

    // ---- data path (only meaningful once Established) ----
    // Seal frames into a session-protected datagram in out (>= len +
    // SEC_DG_HEADER + SEC_DG_TAG). Returns the datagram size, or 0 on
    // misuse. May trigger an automatic epoch rekey when the per-epoch
    // sequence crosses rekey_threshold.
    size_t datagram_encode(uint8_t* out, size_t cap,
                           const uint8_t* frames, size_t len,
                           TrafficClass cls);

    // Authenticate, de-replay, and unwrap a session-protected
    // datagram. On success returns the frames' length and sets
    // *frames (into data) and *cls. Returns -1 on auth failure, -2 on
    // malformed/not-a-session-datagram, -3 on replay or stale epoch.
    int datagram_decode(uint8_t* data, size_t len,
                        const uint8_t** frames, TrafficClass* cls);

    // Explicit key rotation: bump the tx epoch, re-derive the tx key,
    // and restart the per-epoch sequence. The peer follows on the
    // epoch byte of the next datagram.
    void rekey();

    // ---- internal helpers (public struct, library use) ----
    void derive_prk();
    void derive_traffic_key(bool is_tx, uint32_t epoch, uint8_t* out);
    void finished_mac(bool server, uint8_t out[SEC_FINISHED_SIZE]);
};

} // namespace intentproto

#endif // INTENTPROTO_SESSION_SEC_HPP
