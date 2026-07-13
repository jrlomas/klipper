// intentproto v2 link layer tests: framing v2 + datagrams.

#include "intentproto/datagram.hpp"
#include "intentproto/proto.hpp"

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

using namespace intentproto;

static void test_frame_v2() {
    uint8_t payload[32];
    for (int i = 0; i < 32; i++)
        payload[i] = (uint8_t)(i * 7 + 1);
    uint8_t frame[64];
    size_t n = frame_v2_encode(frame, payload, 32, 5);
    CHECK(n == 32 + FRAME_V2_OVERHEAD);
    CHECK(frame[1] == (0x80 | 5));

    const uint8_t* out;
    uint8_t seq = 0;
    CHECK(frame_v2_decode(frame, n, &out, &seq) == 32);
    CHECK(seq == 5);
    CHECK(!memcmp(out, payload, 32));

    // Three bit errors are corrected in place
    uint8_t dam[64];
    memcpy(dam, frame, n);
    dam[3] ^= 0x10;
    dam[10] ^= 0x02;
    dam[33] ^= 0x80;
    CHECK(frame_v2_decode(dam, n, &out, &seq) == 32);
    CHECK(!memcmp(out, payload, 32));

    // Legacy rejection property: the v2 seq byte sets reserved bits
    CHECK((frame[1] & ~(MESSAGE_SEQ_MASK | 0x10)) != 0);
}

static const uint8_t PSK[16] = {1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16};

static void test_datagram_auth_roundtrip() {
    DatagramTx tx;
    DatagramRx rx;
    datagram_tx_init(&tx, PSK, sizeof(PSK), 0);
    datagram_rx_init(&rx, PSK, sizeof(PSK));

    uint8_t frames[40];
    memset(frames, 0xab, sizeof(frames));
    uint8_t dg[DATAGRAM_MAX];
    size_t n = datagram_encode(&tx, dg, frames, sizeof(frames),
                               TrafficClass::Scheduled);
    CHECK(n == DATAGRAM_HEADER + 40 + DATAGRAM_TAG);

    const uint8_t* out;
    TrafficClass cls;
    int r = datagram_decode(&rx, dg, n, &out, &cls);
    CHECK(r == 40);
    CHECK(cls == TrafficClass::Scheduled);
    CHECK(!memcmp(out, frames, 40));
    CHECK(rx.auth_failures == 0);

    // Forged content is rejected
    size_t n2 = datagram_encode(&tx, dg, frames, sizeof(frames),
                                TrafficClass::Prompt);
    dg[5] ^= 1;
    r = datagram_decode(&rx, dg, n2, &out, &cls);
    CHECK(r == -1);
    CHECK(rx.auth_failures == 1);

    // Unauthenticated datagram rejected when a PSK is configured
    uint8_t plain[16] = {0, 9, 0, 1, 2, 3};
    r = datagram_decode(&rx, plain, sizeof(plain), &out, &cls);
    CHECK(r == -1);
}

static void check_datagram_loss_recovery(size_t survivor_len,
                                         size_t lost_len) {
    DatagramTx tx;
    DatagramRx rx;
    datagram_tx_init(&tx, PSK, sizeof(PSK), 2); // parity every 2
    datagram_rx_init(&rx, PSK, sizeof(PSK));

    uint8_t f1[32], f2[32];
    memset(f1, 0x11, sizeof(f1));
    memset(f2, 0x22, sizeof(f2));
    uint8_t d1[DATAGRAM_MAX], d2[DATAGRAM_MAX], dp[DATAGRAM_MAX];
    size_t n1 = datagram_encode(&tx, d1, f1, survivor_len,
                                TrafficClass::Scheduled);
    CHECK(datagram_parity_flush(&tx, dp) == 0);
    size_t n2 = datagram_encode(&tx, d2, f2, lost_len,
                                TrafficClass::Scheduled);
    size_t np = datagram_parity_flush(&tx, dp);
    CHECK(np > 0);

    // Deliver d1, LOSE d2, deliver parity: d2 must be reconstructed
    const uint8_t* out;
    TrafficClass cls;
    CHECK(datagram_decode(&rx, d1, n1, &out, &cls)
          == (int)survivor_len);
    CHECK(datagram_decode(&rx, dp, np, &out, &cls) == 0);
    CHECK(rx.lost == 1);
    uint8_t rec[DATAGRAM_MAX];
    size_t rn = datagram_take_recovered(&rx, rec, sizeof(rec));
    CHECK(rn == DATAGRAM_HEADER + lost_len);
    CHECK(!memcmp(rec + DATAGRAM_HEADER, f2, lost_len));
    (void)n2;
}

static void test_datagram_first_loss_recovery_in_order() {
    DatagramTx tx;
    DatagramRx rx;
    datagram_tx_init(&tx, PSK, sizeof(PSK), 2);
    datagram_rx_init(&rx, PSK, sizeof(PSK));
    uint8_t first[9], second[13];
    memset(first, 0x31, sizeof(first));
    memset(second, 0x32, sizeof(second));
    uint8_t d1[DATAGRAM_MAX], d2[DATAGRAM_MAX], parity[DATAGRAM_MAX];
    size_t n1 = datagram_encode(&tx, d1, first, sizeof(first),
                                TrafficClass::Scheduled);
    CHECK(n1 > 0);
    CHECK(datagram_parity_flush(&tx, parity) == 0);
    size_t n2 = datagram_encode(&tx, d2, second, sizeof(second),
                                TrafficClass::Scheduled);
    size_t np = datagram_parity_flush(&tx, parity);
    CHECK(n2 > 0 && np > 0);

    // Lose the block-start packet. The second packet must be deferred,
    // then parity releases recovered-first followed by the survivor.
    const uint8_t* out;
    TrafficClass cls;
    CHECK(datagram_decode(&rx, d2, n2, &out, &cls) == 0);
    CHECK(datagram_decode(&rx, parity, np, &out, &cls) == 0);
    uint8_t ready[DATAGRAM_MAX];
    size_t rn = datagram_take_recovered(&rx, ready, sizeof(ready));
    CHECK(rn == DATAGRAM_HEADER + sizeof(first));
    CHECK(!memcmp(ready + DATAGRAM_HEADER, first, sizeof(first)));
    rn = datagram_take_recovered(&rx, ready, sizeof(ready));
    CHECK(rn == DATAGRAM_HEADER + sizeof(second));
    CHECK(!memcmp(ready + DATAGRAM_HEADER, second, sizeof(second)));
    CHECK(datagram_take_recovered(&rx, ready, sizeof(ready)) == 0);
}

static void test_datagram_loss_recovery() {
    // Recover byte-exact length/content whether the missing datagram is
    // larger or smaller than the surviving one.
    check_datagram_loss_recovery(8, 20);
    check_datagram_loss_recovery(20, 8);
}

static void test_datagram_fec_bounds_and_version() {
    DatagramTx tx;
    uint8_t frames[DATAGRAM_FEC_MAX_BODY - DATAGRAM_HEADER + 1];
    memset(frames, 0x5a, sizeof(frames));
    uint8_t dg[DATAGRAM_MAX];
    datagram_tx_init(&tx, PSK, sizeof(PSK), 2);
    size_t max_frames = DATAGRAM_FEC_MAX_BODY - DATAGRAM_HEADER;
    CHECK(datagram_encode(&tx, dg, frames, max_frames,
                          TrafficClass::Scheduled)
          == DATAGRAM_FEC_MAX_BODY + DATAGRAM_TAG);

    datagram_tx_init(&tx, PSK, sizeof(PSK), 2);
    CHECK(datagram_encode(&tx, dg, frames, max_frames + 1,
                          TrafficClass::Scheduled) == 0);
    CHECK(tx.next_seq == 0);
    CHECK(tx.sent_since_parity == 0);

    // Pair blocks are the bounded embedded format; other k values fail
    // closed instead of making a recovery promise they cannot keep.
    datagram_tx_init(&tx, PSK, sizeof(PSK), 3);
    CHECK(datagram_encode(&tx, dg, frames, 8,
                          TrafficClass::Scheduled) == 0);
    CHECK(tx.next_seq == 0);

    // An older parity body without the explicit length-format bit is
    // consumed without recovery; v1 ARQ remains the compatibility path.
    DatagramRx rx;
    datagram_tx_init(&tx, nullptr, 0, 2);
    datagram_rx_init(&rx, nullptr, 0);
    uint8_t one[8] = {1}, two[8] = {2};
    uint8_t d1[DATAGRAM_MAX], d2[DATAGRAM_MAX], dp[DATAGRAM_MAX];
    size_t n1 = datagram_encode(&tx, d1, one, sizeof(one),
                                TrafficClass::Scheduled);
    datagram_encode(&tx, d2, two, sizeof(two), TrafficClass::Scheduled);
    size_t np = datagram_parity_flush(&tx, dp);
    const uint8_t* out;
    TrafficClass cls;
    CHECK(datagram_decode(&rx, d1, n1, &out, &cls) == (int)sizeof(one));
    dp[2] &= (uint8_t)~DGF_PARITY_LENGTHS;
    CHECK(datagram_decode(&rx, dp, np, &out, &cls) == 0);
    uint8_t recovered[DATAGRAM_MAX];
    CHECK(datagram_take_recovered(&rx, recovered, sizeof(recovered)) == 0);
}

static void test_datagram_trust_network() {
    DatagramTx tx;
    DatagramRx rx;
    datagram_tx_init(&tx, nullptr, 0, 0); // explicit trust_network
    datagram_rx_init(&rx, nullptr, 0);
    uint8_t f[4] = {9, 8, 7, 6};
    uint8_t dg[DATAGRAM_MAX];
    size_t n = datagram_encode(&tx, dg, f, 4, TrafficClass::Telemetry);
    CHECK(n == DATAGRAM_HEADER + 4); // no tag
    const uint8_t* out;
    TrafficClass cls;
    CHECK(datagram_decode(&rx, dg, n, &out, &cls) == 4);
    CHECK(cls == TrafficClass::Telemetry);
}

int main() {
    test_frame_v2();
    test_datagram_auth_roundtrip();
    test_datagram_loss_recovery();
    test_datagram_first_loss_recovery_in_order();
    test_datagram_fec_bounds_and_version();
    test_datagram_trust_network();
    if (g_failures) {
        printf("%d FAILURE(S)\n", g_failures);
        return 1;
    }
    printf("all tests passed\n");
    return 0;
}
