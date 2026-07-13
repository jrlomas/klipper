#ifndef __GENERIC_UDP_DATAGRAM_H
#define __GENERIC_UDP_DATAGRAM_H
// C bindings for the intentproto datagram link layer (FD-0001 doc 07)

#include <stdint.h> // uint32_t

// Wire geometry.  These mirror lib/intentproto/include/intentproto/
// datagram.hpp - static_asserts in udp_datagram.cpp keep them in sync.
#define UDPDG_DATAGRAM_MAX 1472
#define UDPDG_HEADER 3
#define UDPDG_TAG 8
#define UDPDG_OVERHEAD (UDPDG_HEADER + UDPDG_TAG)
#define UDPDG_FRAMES_MAX (UDPDG_DATAGRAM_MAX - UDPDG_OVERHEAD)

struct udpdg_stats {
    uint32_t rx_lost, rx_reordered, rx_auth_failures;
};

// Initialize the datagram codec.  psk_len==0 selects the explicitly
// unauthenticated trust_network mode; the psk buffer must remain
// valid for the life of the link (it is not copied).  fec_k selects
// the XOR erasure block size (a parity datagram every k data
// datagrams); 0 disables the erasure layer entirely.
void udpdg_init(const uint8_t *psk, uint32_t psk_len, uint8_t fec_k);
// Wrap whole klipper frames into a sealed datagram written to 'out'
// (which must hold len + UDPDG_OVERHEAD bytes).  Returns datagram
// size (0 on overflow).
uint32_t udpdg_encode(uint8_t *out, const uint8_t *frames, uint32_t len);
// When FEC is on and a block has just filled, emit its parity
// datagram into 'out' and return its size, else 0.  Call once after
// every udpdg_encode.
uint32_t udpdg_parity_flush(uint8_t *out);
// Authenticate and sequence-check a received datagram (in place).
// Returns the frames' length and sets *frames pointing into 'data';
// 0 if the datagram was consumed internally (duplicate/parity);
// <0 if rejected (auth failure or malformed).
int32_t udpdg_decode(uint8_t *data, uint32_t len, const uint8_t **frames);
// Non-mutating probe used by the session router. Returns 1 only when a
// PSK-authenticated static datagram validates under the configured key.
int udpdg_is_authenticated_static(uint8_t *data, uint32_t len);
// When the just-decoded datagram was a parity that reconstructed a
// single lost datagram of its block, copy the recovered datagram
// (whole: UDPDG_HEADER header + frames) into 'out' and return its
// length, else 0.  Call once after every udpdg_decode that returns 0.
// The recovered bytes are XOR-derived from already-authenticated
// survivors and parity, so they are post-auth trusted (there is no
// per-datagram tag to re-check on a reconstruction).
uint32_t udpdg_take_recovered(uint8_t *out, uint32_t cap);
void udpdg_get_stats(struct udpdg_stats *st);

// Optional DTLS-class session responder (CONFIG_WANT_DATAGRAM_SESSION)
// Classify a raw datagram: 1 = handshake message, 2 = session data
// (DGF_SESSION), 0 = static-path datagram.
int udpsess_msg_type(const uint8_t *data, uint32_t len);
// Initialize the responder session. random16 is 16 bytes of per-boot
// nonce entropy (uniqueness, not secrecy — the PSK authenticates).
void udpsess_init(const uint8_t *psk, uint32_t psk_len,
                  const uint8_t *board_id, uint32_t id_len,
                  const uint8_t *random16);
int udpsess_established(void);
// Feed one handshake message; any reply is written to out (>=256) and
// its length returned (0 if none).
uint32_t udpsess_on_handshake(const uint8_t *msg, uint32_t len,
                              uint8_t *out, uint32_t cap);
// Return 1 once after a ClientFin has authenticated and the responder
// adopted that candidate as the live session peer.
int udpsess_take_peer_adopted(void);
// Drop a half-open handshake after the router's bounded timeout. A live
// session is preserved; only its pending replacement is cleared.
void udpsess_reset_handshake(void);
// Seal frames into a session datagram (out >= len + 32). Returns size.
uint32_t udpsess_encode(uint8_t *out, uint32_t cap, const uint8_t *frames,
                        uint32_t len);
// Unwrap a session datagram in place: frames' length with *frames set;
// <0 on auth failure / malformed / replay.
int32_t udpsess_decode(uint8_t *data, uint32_t len, const uint8_t **frames);

#endif // udp_datagram.h
