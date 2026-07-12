// intentproto SHA-512 desktop tests.
//
// Vectors are the FIPS 180-4 examples: the empty string, "abc", and the
// two-block 896-bit message. Plus an incremental-chunking cross-check.

#include "intentproto/sha512.hpp"

#include <stdio.h>
#include <string.h>

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

static void from_hex(const char* hex, uint8_t* out, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned v = 0;
        sscanf(hex + 2 * i, "%2x", &v);
        out[i] = (uint8_t)v;
    }
}

static bool digest_is(const uint8_t digest[64], const char* hex) {
    uint8_t want[64];
    from_hex(hex, want, 64);
    return memcmp(digest, want, 64) == 0;
}

static void test_fips_vectors() {
    uint8_t d[64];

    intentproto::sha512((const uint8_t*)"", 0, d);
    CHECK(digest_is(d,
        "cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce"
        "47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e"));

    intentproto::sha512((const uint8_t*)"abc", 3, d);
    CHECK(digest_is(d,
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
        "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"));

    // The 896-bit (112-byte) two-block example.
    const char* m2 = "abcdefghbcdefghicdefghijdefghijkefghijklfghijklm"
                     "ghijklmnhijklmnoijklmnopjklmnopqklmnopqrlmnopqrs"
                     "mnopqrstnopqrstu";
    CHECK(strlen(m2) == 112);
    intentproto::sha512((const uint8_t*)m2, 112, d);
    CHECK(digest_is(d,
        "8e959b75dae313da8cf4f72814fc143f8f7779c6eb9f7fa17299aeadb6889018"
        "501d289e4900f7e4331b99dec4b5433ac7d329eeb6dd26545e96e55b874be909"));
}

static void test_incremental() {
    // A multi-block message fed in odd chunk sizes must match one-shot,
    // including chunks that straddle the 128-byte block boundary.
    uint8_t msg[517];
    for (size_t i = 0; i < sizeof(msg); i++)
        msg[i] = (uint8_t)(i * 11 + 7);

    uint8_t oneshot[64];
    intentproto::sha512(msg, sizeof(msg), oneshot);

    static const size_t chunks[] = {1, 2, 3, 5, 7, 13, 31, 63, 127, 128,
                                    129, 200, 400};
    for (size_t chunk : chunks) {
        intentproto::Sha512 ctx;
        ctx.begin();
        for (size_t off = 0; off < sizeof(msg); off += chunk) {
            size_t n = sizeof(msg) - off;
            if (n > chunk)
                n = chunk;
            ctx.update(msg + off, n);
        }
        uint8_t d[64];
        ctx.finish(d);
        CHECK(memcmp(d, oneshot, 64) == 0);
    }

    // finish() reinitializes: the context is reusable.
    intentproto::Sha512 ctx;
    ctx.begin();
    ctx.update((const uint8_t*)"abc", 3);
    uint8_t d[64];
    ctx.finish(d);
    ctx.update((const uint8_t*)"abc", 3);
    ctx.finish(d);
    CHECK(digest_is(d,
        "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
        "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f"));
}

int main() {
    test_fips_vectors();
    test_incremental();

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
