// intentproto Ed25519 verification tests.
//
// Vectors are RFC 8032 section 7.1 TEST 1, 2, 3 (the canonical
// public-key / message / signature triples), plus a 1023-byte
// multi-block message vector that drives SHA-512 across many blocks.
// Each vector is checked to ACCEPT the valid signature and to REJECT a
// tampered signature, message, and public key. The RFC 8032 triples were
// independently confirmed valid with an external Ed25519 library; the
// 1023-byte vector is deterministic (fixed seed 00..1f, message byte
// i = (i*7+3) & 0xff) and produced by scripts/sign_image.py's signer.

#include "intentproto/ed25519.hpp"

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

using intentproto::ed25519_verify;

// Run accept + reject-on-tamper for one vector.
static void run_vector(const char* name, const char* pub_hex,
                       const uint8_t* msg, size_t mlen, const char* sig_hex) {
    uint8_t pub[32], sig[64];
    from_hex(pub_hex, pub, 32);
    from_hex(sig_hex, sig, 64);

    // Accept the genuine signature.
    if (!ed25519_verify(sig, msg, mlen, pub)) {
        printf("FAIL %s: valid signature rejected\n", name);
        g_failures++;
    }

    // Reject a tampered signature (flip one bit in each of R and S).
    static const int sig_bytes[] = {0, 40, 63};
    for (int i : sig_bytes) {
        uint8_t bad[64];
        memcpy(bad, sig, 64);
        bad[i] ^= 0x01;
        if (ed25519_verify(bad, msg, mlen, pub)) {
            printf("FAIL %s: tampered sig byte %d accepted\n", name, i);
            g_failures++;
        }
    }

    // Reject a tampered public key.
    {
        uint8_t bad[32];
        memcpy(bad, pub, 32);
        bad[0] ^= 0x01;
        if (ed25519_verify(sig, msg, mlen, bad)) {
            printf("FAIL %s: tampered pubkey accepted\n", name);
            g_failures++;
        }
    }

    // Reject a tampered message (only meaningful when non-empty). The
    // largest vector here is 1023 bytes, so a fixed buffer suffices.
    if (mlen) {
        static uint8_t bad[1023];
        memcpy(bad, msg, mlen);
        bad[mlen / 2] ^= 0x01;
        if (ed25519_verify(sig, bad, mlen, pub)) {
            printf("FAIL %s: tampered message accepted\n", name);
            g_failures++;
        }
    }
}

int main() {
    // RFC 8032 TEST 1: empty message.
    run_vector("rfc8032-test1",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
        (const uint8_t*)"", 0,
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015"
        "55fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b");

    // RFC 8032 TEST 2: one-byte message 0x72.
    {
        uint8_t m[1] = {0x72};
        run_vector("rfc8032-test2",
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
            m, 1,
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69"
            "da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00");
    }

    // RFC 8032 TEST 3: two-byte message 0xaf82.
    {
        uint8_t m[2] = {0xaf, 0x82};
        run_vector("rfc8032-test3",
            "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
            m, 2,
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3"
            "ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a");
    }

    // 1023-byte multi-block message (SHA-512 spans several blocks).
    {
        uint8_t m[1023];
        for (size_t i = 0; i < sizeof(m); i++)
            m[i] = (uint8_t)(i * 7 + 3);
        run_vector("large-1023",
            "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8",
            m, sizeof(m),
            "2a9b2aaf45fc9eabf91b1f9abbb2736dd9ebb91f29d788ec6f38b4f9bf1e28"
            "3b585f4dd6511d2684a0cd87e25da95d6c834e7c2ac121896199181467debd740e");
    }

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
