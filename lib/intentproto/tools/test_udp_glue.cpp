// Focused test for the firmware datagram glue (src/generic/
// udp_datagram.cpp) - the extern-C shim the MCU actually runs.  It
// exercises the erasure receive path end to end at the glue layer:
// tx encode + parity_flush, rx decode + take_recovered, with exactly
// one data datagram of a protected block dropped, and asserts the lost
// datagram's frames are reconstructed and gated correctly (no phantom
// recovery from the survivors accumulator).
//
// It also writes wire fixtures (build/udp_glue_*.bin) so the sibling
// Python test (test_udp_bridge_fec.py) can decode the very bytes this
// C path produced, proving host bridge <-> firmware byte identity.
//
// Build + run (from lib/intentproto):
//   g++ -std=c++17 -Iinclude -iquote ../../src/generic \
//       ../../src/generic/udp_datagram.cpp \
//       src/datagram.cpp src/hmac.cpp src/bch.cpp \
//       tools/test_udp_glue.cpp -o build/test_udp_glue
//   (cd tools && ../build/test_udp_glue)

#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

extern "C" {
#include "udp_datagram.h"
}

static int g_failures = 0;
#define CHECK(cond)                                                     \
    do {                                                                \
        if (!(cond)) {                                                  \
            printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);      \
            g_failures++;                                               \
        }                                                               \
    } while (0)

static const uint8_t PSK[16] = {1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16};

static void
dump(const char *dir, const char *name, const uint8_t *p, uint32_t n)
{
    char path[256];
    snprintf(path, sizeof(path), "%s/%s", dir, name);
    FILE *f = fopen(path, "wb");
    if (f) {
        fwrite(p, 1, n, f);
        fclose(f);
    }
}

int
main(int argc, char **argv)
{
    const char *dir = argc > 1 ? argv[1] : "build";
    mkdir(dir, 0755);

    // One tx and one rx over the shared static codecs, fec_k = 2.
    udpdg_init(PSK, sizeof(PSK), 2);

    uint8_t f1[8], f2[8];
    memset(f1, 0x11, sizeof(f1));
    memset(f2, 0x22, sizeof(f2));

    uint8_t d1[UDPDG_DATAGRAM_MAX], d2[UDPDG_DATAGRAM_MAX];
    uint8_t dp[UDPDG_DATAGRAM_MAX];
    uint32_t n1 = udpdg_encode(d1, f1, sizeof(f1));
    uint32_t p_early = udpdg_parity_flush(dp);
    CHECK(p_early == 0);              // no parity until the block fills
    uint32_t n2 = udpdg_encode(d2, f2, sizeof(f2));
    uint32_t np = udpdg_parity_flush(dp);
    CHECK(np > 0);                    // parity emitted after k=2
    (void)n2;

    // Receive d1, DROP d2, receive the parity: d2 must be rebuilt.
    const uint8_t *frames;
    uint8_t rec[UDPDG_DATAGRAM_MAX];

    int32_t r1 = udpdg_decode(d1, n1, &frames);
    CHECK(r1 == (int32_t)sizeof(f1));
    CHECK(!memcmp(frames, f1, sizeof(f1)));
    // Gating: the survivors accumulator must NOT masquerade as recovery
    CHECK(udpdg_take_recovered(rec, sizeof(rec)) == 0);

    int32_t rp = udpdg_decode(dp, np, &frames);
    CHECK(rp == 0);                  // parity consumed internally
    uint32_t rn = udpdg_take_recovered(rec, sizeof(rec));
    CHECK(rn >= UDPDG_HEADER + sizeof(f2));
    CHECK(!memcmp(rec + UDPDG_HEADER, f2, sizeof(f2)));

    // A second take must not re-emit the same reconstruction
    CHECK(udpdg_take_recovered(rec, sizeof(rec)) == 0);

    // Reset and lose the FIRST packet. The second must be deferred until
    // parity, then the glue exposes recovered-first followed by survivor.
    udpdg_init(PSK, sizeof(PSK), 2);
    n1 = udpdg_encode(d1, f1, sizeof(f1));
    CHECK(udpdg_parity_flush(dp) == 0);
    n2 = udpdg_encode(d2, f2, sizeof(f2));
    np = udpdg_parity_flush(dp);
    CHECK(n1 > 0 && n2 > 0 && np > 0);
    CHECK(udpdg_decode(d2, n2, &frames) == 0);
    CHECK(udpdg_decode(dp, np, &frames) == 0);
    rn = udpdg_take_recovered(rec, sizeof(rec));
    CHECK(rn == UDPDG_HEADER + sizeof(f1));
    CHECK(!memcmp(rec + UDPDG_HEADER, f1, sizeof(f1)));
    rn = udpdg_take_recovered(rec, sizeof(rec));
    CHECK(rn == UDPDG_HEADER + sizeof(f2));
    CHECK(!memcmp(rec + UDPDG_HEADER, f2, sizeof(f2)));
    CHECK(udpdg_take_recovered(rec, sizeof(rec)) == 0);

    // Fixtures for the Python wire-identity cross check: the survivor
    // datagram, the parity datagram, and the dropped datagram's frames.
    dump(dir, "udp_glue_survivor.bin", d1, n1);
    dump(dir, "udp_glue_parity.bin", dp, np);
    dump(dir, "udp_glue_lost_frames.bin", f2, sizeof(f2));

    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
