#ifndef INTENTPROTO_HMAC_HPP
#define INTENTPROTO_HMAC_HPP
// SHA-256 (FIPS 180-4) and HMAC-SHA256 (RFC 2104) for the datagram
// transport (RFC 0001, doc 07): UDP frames carry a truncated 8-byte
// HMAC-SHA256 tag.
//
// Core profile: freestanding C++ — no heap, no exceptions, no RTTI,
// no virtual dispatch. All state lives in caller-owned contexts, so
// concurrent sessions and interrupt-context use are safe.

#include <stddef.h>
#include <stdint.h>

namespace intentproto {

constexpr size_t SHA256_DIGEST_SIZE = 32;
constexpr size_t SHA256_BLOCK_SIZE = 64;
constexpr size_t HMAC_TAG_SIZE = 8;

// Incremental SHA-256. begin(); update() any number of times in any
// chunking; finish() writes the digest and reinitializes the context.
struct Sha256 {
    void begin();
    void update(const uint8_t* data, size_t len);
    void finish(uint8_t out[SHA256_DIGEST_SIZE]);

    uint32_t state[8];
    uint64_t total;                     // bytes hashed so far
    uint8_t buf[SHA256_BLOCK_SIZE];     // partial block
    size_t buflen;
};

// One-shot convenience.
void sha256(const uint8_t* data, size_t len,
            uint8_t out[SHA256_DIGEST_SIZE]);

// Incremental HMAC-SHA256. begin() accepts any key length (keys
// longer than the block size are hashed first, per RFC 2104).
// finish() wipes the key material from the context.
struct HmacSha256 {
    void begin(const uint8_t* key, size_t keylen);
    void update(const uint8_t* data, size_t len);
    void finish(uint8_t out[SHA256_DIGEST_SIZE]);

    Sha256 inner;
    uint8_t key_block[SHA256_BLOCK_SIZE];
};

// One-shot convenience.
void hmac_sha256(const uint8_t* key, size_t keylen,
                 const uint8_t* data, size_t len,
                 uint8_t out[SHA256_DIGEST_SIZE]);

// Truncated tag (leftmost 8 bytes, RFC 2104 section 5) as carried by
// datagram frames.
void hmac_sha256_tag(const uint8_t* key, size_t keylen,
                     const uint8_t* data, size_t len,
                     uint8_t tag[HMAC_TAG_SIZE]);

// Constant-time comparison — timing must not leak how many leading
// tag bytes matched.
bool hmac_tag_equal(const uint8_t a[HMAC_TAG_SIZE],
                    const uint8_t b[HMAC_TAG_SIZE]);

} // namespace intentproto

#endif // INTENTPROTO_HMAC_HPP
