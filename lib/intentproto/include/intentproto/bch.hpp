#ifndef INTENTPROTO_BCH_HPP
#define INTENTPROTO_BCH_HPP
// Framing v2 forward error correction (RFC 0001, doc 07): shortened
// binary BCH over GF(2^10), natural length n = 1023 bits, shortened
// to the frame length (<= 61 data bytes = 488 bits), t = 3 — any
// three bit errors per frame are corrected in place; heavier damage
// is detected and reported so the caller can nak (ARQ is retained,
// FEC merely reduces its use).
//
// 30 parity bits, transmitted packed MSB-first into 4 bytes; the two
// low bits of the last byte are spare and sent as zero.
//
// Core profile: freestanding C++ — no heap, no exceptions, no RTTI,
// no virtual dispatch, no STL containers. All lookup tables are
// constexpr-generated const arrays (flash-resident on MCU).

#include <stddef.h>
#include <stdint.h>

namespace intentproto {

// Trailer size on the wire (30 parity bits + 2 spare zero bits).
constexpr size_t BCH_PARITY_BYTES = 4;
// Number of bit errors corrected per frame.
constexpr int BCH_T = 3;
// Largest protected data length (frame minus trailer, per doc 07).
constexpr size_t BCH_DATA_MAX = 61;

// Compute the 30 parity bits over data[0..len) and pack them into
// parity[0..3]. len must be <= BCH_DATA_MAX.
void bch_encode(const uint8_t* data, size_t len, uint8_t parity[4]);

// Check and correct a received frame. The codeword covers data and
// parity, so errors in the parity bytes themselves are handled too.
// Returns the number of bit errors corrected (0..BCH_T) — data
// corrections are applied in place, parity corrections to a local
// copy — or -1 if the frame is uncorrectable (caller naks).
// The no-error case is a single table-driven pass, comparable in
// cost to a table-driven CRC; the correction machinery runs only on
// damaged frames.
int bch_decode(uint8_t* data, size_t len, const uint8_t* parity);

} // namespace intentproto

#endif // INTENTPROTO_BCH_HPP
