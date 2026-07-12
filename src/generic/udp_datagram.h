#ifndef __GENERIC_UDP_DATAGRAM_H
#define __GENERIC_UDP_DATAGRAM_H
// C bindings for the intentproto datagram link layer (RFC 0001 doc 07)

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
// valid for the life of the link (it is not copied).
void udpdg_init(const uint8_t *psk, uint32_t psk_len);
// Wrap whole klipper frames into a sealed datagram written to 'out'
// (which must hold len + UDPDG_OVERHEAD bytes).  Returns datagram
// size (0 on overflow).
uint32_t udpdg_encode(uint8_t *out, const uint8_t *frames, uint32_t len);
// Authenticate and sequence-check a received datagram (in place).
// Returns the frames' length and sets *frames pointing into 'data';
// 0 if the datagram was consumed internally (duplicate/parity);
// <0 if rejected (auth failure or malformed).
int32_t udpdg_decode(uint8_t *data, uint32_t len, const uint8_t **frames);
void udpdg_get_stats(struct udpdg_stats *st);

#endif // udp_datagram.h
