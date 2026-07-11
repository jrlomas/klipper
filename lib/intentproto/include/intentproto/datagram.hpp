#ifndef INTENTPROTO_DATAGRAM_HPP
#define INTENTPROTO_DATAGRAM_HPP
// intentproto v2 link layer (RFC 0001 doc 07):
//  - framing v2: BCH(t=3) error-correcting trailer replacing CRC16,
//    negotiated via the reserved seq-byte bits legacy firmware
//    provably rejects
//  - UDP datagram binding: 16-bit datagram sequencing, traffic-class
//    tag, optional XOR erasure parity, mandatory truncated
//    HMAC-SHA256 authentication (an unauthenticated mode exists only
//    behind an explicit trust_network confession)
//
// Freestanding profile: no heap, no exceptions; caller-owned buffers.

#include <stddef.h>
#include <stdint.h>

namespace intentproto {

// ---- framing v2 ----
// <len> <seq | 0x80> <payload...> <BCH parity: 4 bytes> <sync 0x7e>
constexpr uint8_t FRAME_V2_FLAG = 0x80;
constexpr size_t FRAME_V2_OVERHEAD = 7; // len, seq, parity[4], sync

// Encode payload (<= 57 bytes) into out (>= payload_len +
// FRAME_V2_OVERHEAD); returns the frame length.
size_t frame_v2_encode(uint8_t* out, const uint8_t* payload,
                       size_t payload_len, uint8_t seq);
// Decode (and correct) a frame in place. Returns payload length and
// sets *payload / *seq, or -1 if uncorrectable (caller naks; ARQ is
// retained, FEC only reduces its use).
int frame_v2_decode(uint8_t* frame, size_t frame_len,
                    const uint8_t** payload, uint8_t* seq);

// ---- traffic classes (RFC 0001 doc 03) ----
enum class TrafficClass : uint8_t { Scheduled = 0, Prompt = 1,
                                    Telemetry = 2 };

struct ClassStats {
    uint32_t tx_msgs, tx_bytes;
    uint32_t rx_msgs, rx_bytes;
    uint32_t dropped;         // producer-side drops (Class 2 only)
};

// ---- datagram binding ----
// [u16 seq][u8 flags][payload: whole frames][8-byte HMAC tag]
// flags: bits 0-1 traffic class, bit 2 = XOR parity datagram,
//        bit 3 = authenticated
constexpr size_t DATAGRAM_HEADER = 3;
constexpr size_t DATAGRAM_TAG = 8;
constexpr size_t DATAGRAM_MAX = 1472; // typical UDP payload MTU
constexpr uint8_t DGF_CLASS_MASK = 0x03;
constexpr uint8_t DGF_PARITY = 0x04;
constexpr uint8_t DGF_AUTH = 0x08;

struct DatagramTx {
    const uint8_t* psk;
    size_t psk_len;           // 0 => trust_network mode (unauthenticated)
    uint16_t next_seq;
    // XOR erasure accumulator: parity over the last k datagrams
    uint8_t parity[DATAGRAM_MAX];
    size_t parity_len;
    uint8_t k;                // parity every k datagrams; 0 = off
    uint8_t sent_since_parity;
    ClassStats stats[3];
};

struct DatagramRx {
    const uint8_t* psk;
    size_t psk_len;
    uint16_t expect_seq;
    bool synced;
    uint32_t lost, reordered, auth_failures;
    ClassStats stats[3];
    // Single-loss recovery buffer
    uint8_t held[DATAGRAM_MAX];
    size_t held_len;
    uint16_t held_seq;
    bool holding;
};

void datagram_tx_init(DatagramTx* tx, const uint8_t* psk, size_t psk_len,
                      uint8_t fec_k);
void datagram_rx_init(DatagramRx* rx, const uint8_t* psk, size_t psk_len);

// Wrap frames (already framed payload bytes) into a datagram in out
// (>= len + DATAGRAM_HEADER + DATAGRAM_TAG). Returns datagram size.
size_t datagram_encode(DatagramTx* tx, uint8_t* out, const uint8_t* frames,
                       size_t len, TrafficClass cls);
// If FEC is on and due, emits a parity datagram into out and returns
// its size, else 0. Call after each datagram_encode.
size_t datagram_parity_flush(DatagramTx* tx, uint8_t* out);

// Authenticate + sequence-check a received datagram. On success
// returns the frames' length and sets *frames (pointing into data)
// and *cls; parity datagrams are consumed internally (may recover a
// lost datagram into rx->held). Returns -1 on auth failure, -2 on
// malformed, 0 for consumed-internally.
int datagram_decode(DatagramRx* rx, uint8_t* data, size_t len,
                    const uint8_t** frames, TrafficClass* cls);
// After a decode that detected a single loss recovered by parity,
// fetch the reconstructed datagram (returns length or 0).
size_t datagram_take_recovered(DatagramRx* rx, uint8_t* out, size_t cap);

} // namespace intentproto

#endif // INTENTPROTO_DATAGRAM_HPP
