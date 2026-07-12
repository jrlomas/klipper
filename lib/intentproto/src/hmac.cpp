// SHA-256 per FIPS 180-4 and HMAC per RFC 2104.
// Freestanding profile: no heap, no exceptions, no RTTI; the only
// libc dependencies are memcpy-class functions.

#include "intentproto/hmac.hpp"

#include <string.h>

namespace intentproto {

// ---------------- SHA-256 ----------------

namespace {

// FIPS 180-4 section 4.2.2: first 32 bits of the fractional parts of
// the cube roots of the first 64 primes.
const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
    0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
    0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
    0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
    0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
    0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

inline uint32_t rotr(uint32_t x, int n) {
    return (x >> n) | (x << (32 - n));
}

inline uint32_t load_be32(const uint8_t* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16)
         | ((uint32_t)p[2] << 8) | (uint32_t)p[3];
}

inline void store_be32(uint8_t* p, uint32_t v) {
    p[0] = (uint8_t)(v >> 24);
    p[1] = (uint8_t)(v >> 16);
    p[2] = (uint8_t)(v >> 8);
    p[3] = (uint8_t)v;
}

// FIPS 180-4 section 6.2.2: process one 512-bit block.
void sha256_block(uint32_t state[8], const uint8_t block[64]) {
    uint32_t w[64];
    for (int t = 0; t < 16; t++)
        w[t] = load_be32(block + 4 * t);
    for (int t = 16; t < 64; t++) {
        uint32_t s0 = rotr(w[t - 15], 7) ^ rotr(w[t - 15], 18)
                    ^ (w[t - 15] >> 3);
        uint32_t s1 = rotr(w[t - 2], 17) ^ rotr(w[t - 2], 19)
                    ^ (w[t - 2] >> 10);
        w[t] = w[t - 16] + s0 + w[t - 7] + s1;
    }

    uint32_t a = state[0], b = state[1], c = state[2], d = state[3];
    uint32_t e = state[4], f = state[5], g = state[6], h = state[7];

    for (int t = 0; t < 64; t++) {
        uint32_t sig1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = h + sig1 + ch + K[t] + w[t];
        uint32_t sig0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = sig0 + maj;
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }

    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

} // namespace

void Sha256::begin() {
    // FIPS 180-4 section 5.3.3 initial hash value.
    state[0] = 0x6a09e667;
    state[1] = 0xbb67ae85;
    state[2] = 0x3c6ef372;
    state[3] = 0xa54ff53a;
    state[4] = 0x510e527f;
    state[5] = 0x9b05688c;
    state[6] = 0x1f83d9ab;
    state[7] = 0x5be0cd19;
    total = 0;
    buflen = 0;
}

void Sha256::update(const uint8_t* data, size_t len) {
    total += len;
    if (buflen) {
        size_t n = SHA256_BLOCK_SIZE - buflen;
        if (n > len)
            n = len;
        memcpy(buf + buflen, data, n);
        buflen += n;
        data += n;
        len -= n;
        if (buflen < SHA256_BLOCK_SIZE)
            return;
        sha256_block(state, buf);
        buflen = 0;
    }
    while (len >= SHA256_BLOCK_SIZE) {
        sha256_block(state, data);
        data += SHA256_BLOCK_SIZE;
        len -= SHA256_BLOCK_SIZE;
    }
    if (len) {
        memcpy(buf, data, len);
        buflen = len;
    }
}

void Sha256::finish(uint8_t out[SHA256_DIGEST_SIZE]) {
    // Pad: 0x80, zeros, 64-bit big-endian bit length (section 5.1.1).
    uint64_t bits = total << 3;
    buf[buflen++] = 0x80;
    if (buflen > SHA256_BLOCK_SIZE - 8) {
        memset(buf + buflen, 0, SHA256_BLOCK_SIZE - buflen);
        sha256_block(state, buf);
        buflen = 0;
    }
    memset(buf + buflen, 0, SHA256_BLOCK_SIZE - 8 - buflen);
    store_be32(buf + SHA256_BLOCK_SIZE - 8, (uint32_t)(bits >> 32));
    store_be32(buf + SHA256_BLOCK_SIZE - 4, (uint32_t)bits);
    sha256_block(state, buf);
    for (int i = 0; i < 8; i++)
        store_be32(out + 4 * i, state[i]);
    begin();
}

void sha256(const uint8_t* data, size_t len,
            uint8_t out[SHA256_DIGEST_SIZE]) {
    Sha256 ctx;
    ctx.begin();
    ctx.update(data, len);
    ctx.finish(out);
}

// ---------------- HMAC-SHA256 ----------------

void HmacSha256::begin(const uint8_t* key, size_t keylen) {
    if (keylen > SHA256_BLOCK_SIZE) {
        sha256(key, keylen, key_block);
        keylen = SHA256_DIGEST_SIZE;
    } else {
        memcpy(key_block, key, keylen);
    }
    memset(key_block + keylen, 0, SHA256_BLOCK_SIZE - keylen);

    uint8_t pad[SHA256_BLOCK_SIZE];
    for (size_t i = 0; i < SHA256_BLOCK_SIZE; i++)
        pad[i] = (uint8_t)(key_block[i] ^ 0x36);
    inner.begin();
    inner.update(pad, SHA256_BLOCK_SIZE);
    memset(pad, 0, sizeof(pad));
}

void HmacSha256::update(const uint8_t* data, size_t len) {
    inner.update(data, len);
}

void HmacSha256::finish(uint8_t out[SHA256_DIGEST_SIZE]) {
    uint8_t inner_digest[SHA256_DIGEST_SIZE];
    inner.finish(inner_digest);

    uint8_t pad[SHA256_BLOCK_SIZE];
    for (size_t i = 0; i < SHA256_BLOCK_SIZE; i++)
        pad[i] = (uint8_t)(key_block[i] ^ 0x5c);
    Sha256 outer;
    outer.begin();
    outer.update(pad, SHA256_BLOCK_SIZE);
    outer.update(inner_digest, SHA256_DIGEST_SIZE);
    outer.finish(out);

    memset(pad, 0, sizeof(pad));
    memset(inner_digest, 0, sizeof(inner_digest));
    memset(key_block, 0, sizeof(key_block));
}

void hmac_sha256(const uint8_t* key, size_t keylen,
                 const uint8_t* data, size_t len,
                 uint8_t out[SHA256_DIGEST_SIZE]) {
    HmacSha256 ctx;
    ctx.begin(key, keylen);
    ctx.update(data, len);
    ctx.finish(out);
}

void hmac_sha256_tag(const uint8_t* key, size_t keylen,
                     const uint8_t* data, size_t len,
                     uint8_t tag[HMAC_TAG_SIZE]) {
    uint8_t digest[SHA256_DIGEST_SIZE];
    hmac_sha256(key, keylen, data, len, digest);
    memcpy(tag, digest, HMAC_TAG_SIZE);
    memset(digest, 0, sizeof(digest));
}

bool hmac_tag_equal(const uint8_t a[HMAC_TAG_SIZE],
                    const uint8_t b[HMAC_TAG_SIZE]) {
    volatile uint8_t diff = 0;
    for (size_t i = 0; i < HMAC_TAG_SIZE; i++)
        diff |= (uint8_t)(a[i] ^ b[i]);
    return diff == 0;
}

// ---------------- HKDF-SHA256 (RFC 5869) ----------------

void hkdf_extract(const uint8_t* salt, size_t salt_len,
                  const uint8_t* ikm, size_t ikm_len,
                  uint8_t prk[SHA256_DIGEST_SIZE]) {
    // A null salt is defined as HashLen zero bytes (section 2.2). The
    // HMAC key path already zero-pads a short key, so passing the zero
    // buffer explicitly keeps that intent visible.
    uint8_t zero[SHA256_DIGEST_SIZE];
    if (!salt || !salt_len) {
        memset(zero, 0, sizeof(zero));
        salt = zero;
        salt_len = sizeof(zero);
    }
    hmac_sha256(salt, salt_len, ikm, ikm_len, prk);
    memset(zero, 0, sizeof(zero));
}

void hkdf_expand(const uint8_t prk[SHA256_DIGEST_SIZE],
                 const uint8_t* info, size_t info_len,
                 uint8_t* okm, size_t okm_len) {
    // T(0) = empty; T(i) = HMAC(PRK, T(i-1) || info || i); OKM is the
    // first okm_len bytes of T(1) || T(2) || ... (section 2.3).
    uint8_t t[SHA256_DIGEST_SIZE];
    size_t t_len = 0;
    uint8_t counter = 0;
    size_t done = 0;
    while (done < okm_len) {
        counter++;
        HmacSha256 h;
        h.begin(prk, SHA256_DIGEST_SIZE);
        h.update(t, t_len);            // T(i-1); empty on the first pass
        if (info_len)
            h.update(info, info_len);
        h.update(&counter, 1);
        h.finish(t);
        t_len = SHA256_DIGEST_SIZE;
        size_t n = okm_len - done;
        if (n > SHA256_DIGEST_SIZE)
            n = SHA256_DIGEST_SIZE;
        memcpy(okm + done, t, n);
        done += n;
    }
    memset(t, 0, sizeof(t));
}

void hkdf_sha256(const uint8_t* salt, size_t salt_len,
                 const uint8_t* ikm, size_t ikm_len,
                 const uint8_t* info, size_t info_len,
                 uint8_t* okm, size_t okm_len) {
    uint8_t prk[SHA256_DIGEST_SIZE];
    hkdf_extract(salt, salt_len, ikm, ikm_len, prk);
    hkdf_expand(prk, info, info_len, okm, okm_len);
    memset(prk, 0, sizeof(prk));
}

} // namespace intentproto
