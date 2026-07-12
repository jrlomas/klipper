// intentproto SHA-256 / HMAC-SHA256 desktop tests.
//
// SHA-256 vectors are the FIPS 180-4 examples; HMAC vectors are from
// RFC 4231.

#include "intentproto/hmac.hpp"

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

// Parse a lowercase hex string into bytes; n is the byte count.
static void from_hex(const char* hex, uint8_t* out, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned v = 0;
        sscanf(hex + 2 * i, "%2x", &v);
        out[i] = (uint8_t)v;
    }
}

static bool digest_is(const uint8_t digest[32], const char* hex) {
    uint8_t want[32];
    from_hex(hex, want, 32);
    return memcmp(digest, want, 32) == 0;
}

static void test_sha256_vectors() {
    uint8_t d[32];

    intentproto::sha256((const uint8_t*)"", 0, d);
    CHECK(digest_is(d, "e3b0c44298fc1c149afbf4c8996fb924"
                       "27ae41e4649b934ca495991b7852b855"));

    intentproto::sha256((const uint8_t*)"abc", 3, d);
    CHECK(digest_is(d, "ba7816bf8f01cfea414140de5dae2223"
                       "b00361a396177a9cb410ff61f20015ad"));

    const char* m2 = "abcdbcdecdefdefgefghfghighijhijk"
                     "ijkljklmklmnlmnomnopnopq";
    CHECK(strlen(m2) == 56);
    intentproto::sha256((const uint8_t*)m2, 56, d);
    CHECK(digest_is(d, "248d6a61d20638b8e5c026930c3e6039"
                       "a33ce45964ff2167f6ecedd419db06c1"));
}

static void test_sha256_incremental() {
    // A message spanning several blocks, fed in odd-sized chunks,
    // must match the one-shot digest.
    uint8_t msg[219];
    for (size_t i = 0; i < sizeof(msg); i++)
        msg[i] = (uint8_t)(i * 7 + 3);

    uint8_t oneshot[32];
    intentproto::sha256(msg, sizeof(msg), oneshot);

    static const size_t chunks[] = {1, 2, 3, 5, 7, 11, 13, 63, 65, 200};
    for (size_t chunk : chunks) {
        intentproto::Sha256 ctx;
        ctx.begin();
        for (size_t off = 0; off < sizeof(msg); off += chunk) {
            size_t n = sizeof(msg) - off;
            if (n > chunk)
                n = chunk;
            ctx.update(msg + off, n);
        }
        uint8_t d[32];
        ctx.finish(d);
        CHECK(memcmp(d, oneshot, 32) == 0);
    }

    // finish() reinitializes: the context is reusable.
    intentproto::Sha256 ctx;
    ctx.begin();
    ctx.update((const uint8_t*)"abc", 3);
    uint8_t d[32];
    ctx.finish(d);
    ctx.update((const uint8_t*)"abc", 3);
    ctx.finish(d);
    CHECK(digest_is(d, "ba7816bf8f01cfea414140de5dae2223"
                       "b00361a396177a9cb410ff61f20015ad"));
}

static void test_hmac_rfc4231() {
    uint8_t d[32];

    // Test case 1.
    uint8_t key1[20];
    memset(key1, 0x0b, sizeof(key1));
    intentproto::hmac_sha256(key1, sizeof(key1),
                             (const uint8_t*)"Hi There", 8, d);
    CHECK(digest_is(d, "b0344c61d8db38535ca8afceaf0bf12b"
                       "881dc200c9833da726e9376c2e32cff7"));

    // Test case 2.
    intentproto::hmac_sha256(
        (const uint8_t*)"Jefe", 4,
        (const uint8_t*)"what do ya want for nothing?", 28, d);
    CHECK(digest_is(d, "5bdcc146bf60754e6a042426089575c7"
                       "5a003f089d2739839dec58b964ec3843"));

    // Test case 6: key larger than the block size.
    uint8_t key6[131];
    memset(key6, 0xaa, sizeof(key6));
    const char* msg6 = "Test Using Larger Than Block-Size Key "
                       "- Hash Key First";
    intentproto::hmac_sha256(key6, sizeof(key6),
                             (const uint8_t*)msg6, strlen(msg6), d);
    CHECK(digest_is(d, "60e431591ee0b67f0d8a26aacbf5b77f"
                       "8e0bc6213728c5140546040f0ee37f54"));

    // Incremental HMAC matches the one-shot.
    intentproto::HmacSha256 ctx;
    ctx.begin((const uint8_t*)"Jefe", 4);
    ctx.update((const uint8_t*)"what do ya ", 11);
    ctx.update((const uint8_t*)"want for nothing?", 17);
    uint8_t d2[32];
    ctx.finish(d2);
    CHECK(memcmp(d, d2, 32) != 0);   // d is still test case 6
    CHECK(digest_is(d2, "5bdcc146bf60754e6a042426089575c7"
                        "5a003f089d2739839dec58b964ec3843"));
}

static void test_tags() {
    uint8_t key[20];
    memset(key, 0x0b, sizeof(key));
    uint8_t tag[8];
    intentproto::hmac_sha256_tag(key, sizeof(key),
                                 (const uint8_t*)"Hi There", 8, tag);
    // Leftmost 8 bytes of the founding document 4231 case 1 digest.
    uint8_t want[8];
    from_hex("b0344c61d8db3853", want, 8);
    CHECK(intentproto::hmac_tag_equal(tag, want));

    // Unequal in each byte position must compare unequal.
    for (int i = 0; i < 8; i++) {
        uint8_t bad[8];
        memcpy(bad, want, 8);
        bad[i] ^= 0x01;
        CHECK(!intentproto::hmac_tag_equal(tag, bad));
    }
}

int main() {
    test_sha256_vectors();
    test_sha256_incremental();
    test_hmac_rfc4231();
    test_tags();

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
