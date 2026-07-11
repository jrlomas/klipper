// intentproto framing v2 BCH codec tests.
//
// Bit positions in these tests are codeword-stream indices: index
// 0..8*len-1 walks the data bytes MSB-first, index 8*len..8*len+29
// walks the 30 packed parity bits MSB-first (the two spare low bits
// of parity[3] carry no information and are never flipped).

#include "intentproto/bch.hpp"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

// Deterministic PRNG (xorshift32, fixed seed).
static uint32_t g_rng = 0x2545f491;
static uint32_t prng() {
    g_rng ^= g_rng << 13;
    g_rng ^= g_rng >> 17;
    g_rng ^= g_rng << 5;
    return g_rng;
}

// Flip codeword bit `pos` (see the numbering above).
static void flip_bit(uint8_t* data, uint8_t* parity, size_t len,
                     unsigned pos) {
    if (pos < 8 * len) {
        data[pos >> 3] ^= (uint8_t)(0x80u >> (pos & 7));
    } else {
        unsigned q = pos - (unsigned)(8 * len);     // 0..29
        parity[q >> 3] ^= (uint8_t)(0x80u >> (q & 7));
    }
}

static unsigned codeword_bits(size_t len) {
    return (unsigned)(8 * len) + 30;
}

// Pick n distinct positions in [0, codeword_bits(len)).
static void pick_positions(size_t len, unsigned* pos, int n) {
    unsigned nbits = codeword_bits(len);
    for (int i = 0; i < n; i++) {
        for (;;) {
            unsigned p = prng() % nbits;
            bool dup = false;
            for (int j = 0; j < i; j++)
                if (pos[j] == p)
                    dup = true;
            if (!dup) {
                pos[i] = p;
                break;
            }
        }
    }
}

static int bitdiff(const uint8_t* a, const uint8_t* b, size_t len) {
    int n = 0;
    for (size_t i = 0; i < len; i++) {
        uint8_t d = a[i] ^ b[i];
        while (d) {
            n += d & 1;
            d >>= 1;
        }
    }
    return n;
}

// ---------------- tests ----------------

static void test_clean_roundtrip() {
    uint8_t data[61], work[61], parity[4];
    for (int i = 0; i < 61; i++)
        data[i] = (uint8_t)(i * 7 + 3);
    intentproto::bch_encode(data, 61, parity);
    memcpy(work, data, 61);
    CHECK(intentproto::bch_decode(work, 61, parity) == 0);
    CHECK(memcmp(work, data, 61) == 0);
    // Encoding is deterministic.
    uint8_t parity2[4];
    intentproto::bch_encode(data, 61, parity2);
    CHECK(memcmp(parity, parity2, 4) == 0);
    // Spare bits are transmitted as zero.
    CHECK((parity[3] & 0x03) == 0);
}

static void test_extremes() {
    static const size_t lens[] = {1, 5, 61};
    for (size_t len : lens) {
        uint8_t data[61], work[61], parity[4];
        memset(data, 0x00, len);
        intentproto::bch_encode(data, len, parity);
        // All-zero data is a codeword of the cyclic code: parity 0.
        CHECK((parity[0] | parity[1] | parity[2] | parity[3]) == 0);
        memcpy(work, data, len);
        CHECK(intentproto::bch_decode(work, len, parity) == 0);
        CHECK(memcmp(work, data, len) == 0);

        memset(data, 0xff, len);
        intentproto::bch_encode(data, len, parity);
        memcpy(work, data, len);
        CHECK(intentproto::bch_decode(work, len, parity) == 0);
        CHECK(memcmp(work, data, len) == 0);
    }
}

static void test_random_flips() {
    static const size_t lens[] = {1, 5, 61};
    for (size_t len : lens) {
        for (int nerr = 1; nerr <= 3; nerr++) {
            for (int trial = 0; trial < 400; trial++) {
                uint8_t data[61], work[61], parity[4], pwork[4];
                for (size_t i = 0; i < len; i++)
                    data[i] = (uint8_t)prng();
                intentproto::bch_encode(data, len, parity);
                memcpy(work, data, len);
                memcpy(pwork, parity, 4);
                unsigned pos[3];
                pick_positions(len, pos, nerr);
                for (int i = 0; i < nerr; i++)
                    flip_bit(work, pwork, len, pos[i]);
                int rc = intentproto::bch_decode(work, len, pwork);
                CHECK(rc == nerr);
                CHECK(memcmp(work, data, len) == 0);
                if (rc != nerr || memcmp(work, data, len) != 0)
                    return;             // stop the flood on failure
            }
        }
    }
}

// 4 flips exceed t=3: the decoder must never silently hand back a
// non-codeword. Either it detects (-1) or it "corrects" to some
// OTHER valid codeword (aliasing is unavoidable beyond the design
// distance). Verify the codeword property whenever rc >= 0 by
// re-encoding, and check the decoder's honesty: the returned count
// equals the Hamming distance between the received word and the
// codeword it settled on.
static void test_four_flips() {
    static const size_t lens[] = {5, 61};
    int detected = 0, miscorrected = 0, total = 0;
    for (size_t len : lens) {
        for (int trial = 0; trial < 1500; trial++) {
            uint8_t data[61], work[61], corrupt[61];
            uint8_t parity[4], pwork[4];
            for (size_t i = 0; i < len; i++)
                data[i] = (uint8_t)prng();
            intentproto::bch_encode(data, len, parity);
            memcpy(work, data, len);
            memcpy(pwork, parity, 4);
            unsigned pos[4];
            pick_positions(len, pos, 4);
            for (int i = 0; i < 4; i++)
                flip_bit(work, pwork, len, pos[i]);
            memcpy(corrupt, work, len);
            int rc = intentproto::bch_decode(work, len, pwork);
            total++;
            if (rc < 0) {
                detected++;
                continue;
            }
            miscorrected++;
            CHECK(rc >= 1 && rc <= intentproto::BCH_T);
            // The settled-on word must be a valid codeword: its data
            // part re-encodes to the parity the decoder implies
            // (received parity minus the parity-bit corrections).
            uint8_t reparity[4];
            intentproto::bch_encode(work, len, reparity);
            int dist = bitdiff(corrupt, work, len)
                + bitdiff(pwork, reparity, 4);
            CHECK(dist == rc);
            // And decoding the settled-on codeword is clean.
            uint8_t again[61];
            memcpy(again, work, len);
            CHECK(intentproto::bch_decode(again, len, reparity) == 0);
        }
    }
    printf("4-bit errors: %d/%d detected, %d miscorrected to a valid"
           " codeword\n", detected, total, miscorrected);
    CHECK(detected + miscorrected == total);
    CHECK(detected > 0);
}

// Hand-picked patterns: adjacent bits, byte boundaries, and the
// data/parity seam.
static void test_handpicked() {
    static const size_t len = 61;
    static const unsigned cases[][3] = {
        {0, 1, 2},          // first bits of data[0]
        {7, 8, 15},         // data byte boundaries
        {6, 7, 8},          // straddling data[0]/data[1]
        {486, 487, 488},    // last data bits + first parity bit
        {487, 488, 489},    // the data/parity seam
        {515, 516, 517},    // last three parity bits
        {0, 487, 517},      // extremes of the codeword
    };
    for (const unsigned* c : cases) {
        uint8_t data[len], work[len], parity[4], pwork[4];
        for (size_t i = 0; i < len; i++)
            data[i] = (uint8_t)(0xa5 ^ i);
        intentproto::bch_encode(data, len, parity);
        // All prefixes of the pattern: 1, 2 and 3 flips.
        for (int n = 1; n <= 3; n++) {
            memcpy(work, data, len);
            memcpy(pwork, parity, 4);
            for (int i = 0; i < n; i++)
                flip_bit(work, pwork, len, c[i]);
            CHECK(intentproto::bch_decode(work, len, pwork) == n);
            CHECK(memcmp(work, data, len) == 0);
        }
    }
}

// Single-bit errors at every position of a short frame.
static void test_exhaustive_single() {
    static const size_t len = 5;
    uint8_t data[len], work[len], parity[4], pwork[4];
    for (size_t i = 0; i < len; i++)
        data[i] = (uint8_t)(0x3c + 11 * i);
    intentproto::bch_encode(data, len, parity);
    for (unsigned p = 0; p < codeword_bits(len); p++) {
        memcpy(work, data, len);
        memcpy(pwork, parity, 4);
        flip_bit(work, pwork, len, p);
        CHECK(intentproto::bch_decode(work, len, pwork) == 1);
        CHECK(memcmp(work, data, len) == 0);
    }
}

int main() {
    test_clean_roundtrip();
    test_extremes();
    test_random_flips();
    test_four_flips();
    test_handpicked();
    test_exhaustive_single();

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
