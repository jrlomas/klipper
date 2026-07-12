#ifndef INTENTPROTO_SHA512_HPP
#define INTENTPROTO_SHA512_HPP
// SHA-512 (FIPS 180-4). Added for Ed25519 signed-image verification
// (FD-0001 doc 11, "Signed images"): RFC 8032 Ed25519 hashes with
// SHA-512, which the SHA-256 in hmac.hpp does not provide.
//
// Core profile: freestanding C++ — no heap, no exceptions, no RTTI,
// no virtual dispatch. All state lives in caller-owned contexts.

#include <stddef.h>
#include <stdint.h>

namespace intentproto {

constexpr size_t SHA512_DIGEST_SIZE = 64;
constexpr size_t SHA512_BLOCK_SIZE = 128;

// Incremental SHA-512. begin(); update() any number of times in any
// chunking; finish() writes the digest and reinitializes the context.
struct Sha512 {
    void begin();
    void update(const uint8_t* data, size_t len);
    void finish(uint8_t out[SHA512_DIGEST_SIZE]);

    uint64_t state[8];
    uint64_t total_lo;                  // byte count, low 64 bits
    uint64_t total_hi;                  // byte count, high 64 bits
    uint8_t buf[SHA512_BLOCK_SIZE];     // partial block
    size_t buflen;
};

// One-shot convenience.
void sha512(const uint8_t* data, size_t len,
            uint8_t out[SHA512_DIGEST_SIZE]);

} // namespace intentproto

#endif // INTENTPROTO_SHA512_HPP
