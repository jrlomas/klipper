// SHA-512 per FIPS 180-4.
// Freestanding profile: no heap, no exceptions, no RTTI; the only
// libc dependency is memcpy. See sha512.hpp for why the library needs
// a second hash (Ed25519 signed-image verification, FD-0001 doc 11).

#include "intentproto/sha512.hpp"

#include <string.h>

namespace intentproto {

namespace {

// FIPS 180-4 section 4.2.3: first 64 bits of the fractional parts of
// the cube roots of the first 80 primes.
const uint64_t K[80] = {
    0x428a2f98d728ae22ULL, 0x7137449123ef65cdULL, 0xb5c0fbcfec4d3b2fULL,
    0xe9b5dba58189dbbcULL, 0x3956c25bf348b538ULL, 0x59f111f1b605d019ULL,
    0x923f82a4af194f9bULL, 0xab1c5ed5da6d8118ULL, 0xd807aa98a3030242ULL,
    0x12835b0145706fbeULL, 0x243185be4ee4b28cULL, 0x550c7dc3d5ffb4e2ULL,
    0x72be5d74f27b896fULL, 0x80deb1fe3b1696b1ULL, 0x9bdc06a725c71235ULL,
    0xc19bf174cf692694ULL, 0xe49b69c19ef14ad2ULL, 0xefbe4786384f25e3ULL,
    0x0fc19dc68b8cd5b5ULL, 0x240ca1cc77ac9c65ULL, 0x2de92c6f592b0275ULL,
    0x4a7484aa6ea6e483ULL, 0x5cb0a9dcbd41fbd4ULL, 0x76f988da831153b5ULL,
    0x983e5152ee66dfabULL, 0xa831c66d2db43210ULL, 0xb00327c898fb213fULL,
    0xbf597fc7beef0ee4ULL, 0xc6e00bf33da88fc2ULL, 0xd5a79147930aa725ULL,
    0x06ca6351e003826fULL, 0x142929670a0e6e70ULL, 0x27b70a8546d22ffcULL,
    0x2e1b21385c26c926ULL, 0x4d2c6dfc5ac42aedULL, 0x53380d139d95b3dfULL,
    0x650a73548baf63deULL, 0x766a0abb3c77b2a8ULL, 0x81c2c92e47edaee6ULL,
    0x92722c851482353bULL, 0xa2bfe8a14cf10364ULL, 0xa81a664bbc423001ULL,
    0xc24b8b70d0f89791ULL, 0xc76c51a30654be30ULL, 0xd192e819d6ef5218ULL,
    0xd69906245565a910ULL, 0xf40e35855771202aULL, 0x106aa07032bbd1b8ULL,
    0x19a4c116b8d2d0c8ULL, 0x1e376c085141ab53ULL, 0x2748774cdf8eeb99ULL,
    0x34b0bcb5e19b48a8ULL, 0x391c0cb3c5c95a63ULL, 0x4ed8aa4ae3418acbULL,
    0x5b9cca4f7763e373ULL, 0x682e6ff3d6b2b8a3ULL, 0x748f82ee5defb2fcULL,
    0x78a5636f43172f60ULL, 0x84c87814a1f0ab72ULL, 0x8cc702081a6439ecULL,
    0x90befffa23631e28ULL, 0xa4506cebde82bde9ULL, 0xbef9a3f7b2c67915ULL,
    0xc67178f2e372532bULL, 0xca273eceea26619cULL, 0xd186b8c721c0c207ULL,
    0xeada7dd6cde0eb1eULL, 0xf57d4f7fee6ed178ULL, 0x06f067aa72176fbaULL,
    0x0a637dc5a2c898a6ULL, 0x113f9804bef90daeULL, 0x1b710b35131c471bULL,
    0x28db77f523047d84ULL, 0x32caab7b40c72493ULL, 0x3c9ebe0a15c9bebcULL,
    0x431d67c49c100d4cULL, 0x4cc5d4becb3e42b6ULL, 0x597f299cfc657e2aULL,
    0x5fcb6fab3ad6faecULL, 0x6c44198c4a475817ULL,
};

inline uint64_t rotr(uint64_t x, int n) {
    return (x >> n) | (x << (64 - n));
}

inline uint64_t load_be64(const uint8_t* p) {
    return ((uint64_t)p[0] << 56) | ((uint64_t)p[1] << 48)
         | ((uint64_t)p[2] << 40) | ((uint64_t)p[3] << 32)
         | ((uint64_t)p[4] << 24) | ((uint64_t)p[5] << 16)
         | ((uint64_t)p[6] << 8)  | (uint64_t)p[7];
}

inline void store_be64(uint8_t* p, uint64_t v) {
    p[0] = (uint8_t)(v >> 56);
    p[1] = (uint8_t)(v >> 48);
    p[2] = (uint8_t)(v >> 40);
    p[3] = (uint8_t)(v >> 32);
    p[4] = (uint8_t)(v >> 24);
    p[5] = (uint8_t)(v >> 16);
    p[6] = (uint8_t)(v >> 8);
    p[7] = (uint8_t)v;
}

// FIPS 180-4 section 6.4.2: process one 1024-bit block.
void sha512_block(uint64_t state[8], const uint8_t block[128]) {
    uint64_t w[80];
    for (int t = 0; t < 16; t++)
        w[t] = load_be64(block + 8 * t);
    for (int t = 16; t < 80; t++) {
        uint64_t s0 = rotr(w[t - 15], 1) ^ rotr(w[t - 15], 8)
                    ^ (w[t - 15] >> 7);
        uint64_t s1 = rotr(w[t - 2], 19) ^ rotr(w[t - 2], 61)
                    ^ (w[t - 2] >> 6);
        w[t] = w[t - 16] + s0 + w[t - 7] + s1;
    }

    uint64_t a = state[0], b = state[1], c = state[2], d = state[3];
    uint64_t e = state[4], f = state[5], g = state[6], h = state[7];

    for (int t = 0; t < 80; t++) {
        uint64_t S1 = rotr(e, 14) ^ rotr(e, 18) ^ rotr(e, 41);
        uint64_t ch = (e & f) ^ (~e & g);
        uint64_t t1 = h + S1 + ch + K[t] + w[t];
        uint64_t S0 = rotr(a, 28) ^ rotr(a, 34) ^ rotr(a, 39);
        uint64_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint64_t t2 = S0 + maj;
        h = g; g = f; f = e; e = d + t1;
        d = c; c = b; b = a; a = t1 + t2;
    }

    state[0] += a; state[1] += b; state[2] += c; state[3] += d;
    state[4] += e; state[5] += f; state[6] += g; state[7] += h;
}

} // namespace

void
Sha512::begin() {
    // FIPS 180-4 section 5.3.5: fractional parts of the square roots of
    // the first eight primes.
    state[0] = 0x6a09e667f3bcc908ULL;
    state[1] = 0xbb67ae8584caa73bULL;
    state[2] = 0x3c6ef372fe94f82bULL;
    state[3] = 0xa54ff53a5f1d36f1ULL;
    state[4] = 0x510e527fade682d1ULL;
    state[5] = 0x9b05688c2b3e6c1fULL;
    state[6] = 0x1f83d9abfb41bd6bULL;
    state[7] = 0x5be0cd19137e2179ULL;
    total_lo = 0;
    total_hi = 0;
    buflen = 0;
}

void
Sha512::update(const uint8_t* data, size_t len) {
    uint64_t old = total_lo;
    total_lo += len;
    if (total_lo < old)
        total_hi++;
    while (len) {
        size_t n = SHA512_BLOCK_SIZE - buflen;
        if (n > len)
            n = len;
        memcpy(buf + buflen, data, n);
        buflen += n;
        data += n;
        len -= n;
        if (buflen == SHA512_BLOCK_SIZE) {
            sha512_block(state, buf);
            buflen = 0;
        }
    }
}

void
Sha512::finish(uint8_t out[SHA512_DIGEST_SIZE]) {
    // Message length in bits as a 128-bit big-endian integer.
    uint64_t bits_lo = total_lo << 3;
    uint64_t bits_hi = (total_hi << 3) | (total_lo >> 61);

    uint8_t pad = 0x80;
    update(&pad, 1);
    uint8_t zero = 0x00;
    while (buflen != SHA512_BLOCK_SIZE - 16)
        update(&zero, 1);

    uint8_t lenblock[16];
    store_be64(lenblock, bits_hi);
    store_be64(lenblock + 8, bits_lo);
    update(lenblock, 16);
    // buflen is now 0: the length block completed a full block.

    for (int i = 0; i < 8; i++)
        store_be64(out + 8 * i, state[i]);

    begin(); // reinitialize; context is reusable
}

void
sha512(const uint8_t* data, size_t len, uint8_t out[SHA512_DIGEST_SIZE]) {
    Sha512 ctx;
    ctx.begin();
    ctx.update(data, len);
    ctx.finish(out);
}

} // namespace intentproto
